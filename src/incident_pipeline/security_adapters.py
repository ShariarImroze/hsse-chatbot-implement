"""Standalone adapters for public information- and physical-security records.

The CFPB adapter keeps the published, consumer-supplied narrative.  It does not
turn every credit complaint into a security incident: a row must both mention
identity theft in its narrative and have a CFPB classification that explicitly
or contextually supports that interpretation.

Police.uk does not publish incident narratives.  Its adapter therefore renders
the published structured fields with :func:`format_police_uk_description`, a
fixed and auditable template.  Those descriptions must not be represented as
police-authored narratives.
"""

from __future__ import annotations

import csv
import hashlib
import heapq
import io
import json
import re
import zipfile
from collections.abc import Callable, Hashable, Iterator, Set as AbstractSet
from pathlib import Path
from typing import Optional

from .models import UNITED_STATES_COUNTRY, IncidentRecord
from .text import (
    clean_text,
    is_probably_english,
    normalized_for_hash,
    sampling_rank,
    text_digest,
    word_count,
)


CFPB_SOURCE = "CFPB_CONSUMER_COMPLAINT"
CFPB_SOURCE_URL = "https://www.consumerfinance.gov/data-research/consumer-complaints/"
CFPB_DETAIL_URL = (
    "https://www.consumerfinance.gov/data-research/consumer-complaints/search/detail/{complaint_id}"
)
CFPB_SOURCE_LICENSE = (
    "CC0 1.0 Universal; narratives are optional consumer allegations, scrubbed of "
    "personal information, and are not verified by CFPB"
)

POLICE_UK_SOURCE = "POLICE_UK"
POLICE_UK_SOURCE_URL = "https://data.police.uk/data/"
POLICE_UK_SOURCE_LICENSE = (
    "Open Government Licence v3.0; street locations are anonymised/approximate, "
    "Crime IDs can change when source details change, and the source provides no "
    "incident narrative (Description is a deterministic rendering of source fields)"
)

# The contextual pair is the narrow CFPB credit-report cohort shown by the
# official fields to concern records belonging to somebody else.  We still
# require the consumer narrative itself to say "identity theft".
CFPB_CONTEXTUAL_IDENTITY_THEFT_CLASSIFICATIONS = frozenset(
    {
        (
            "incorrect information on your report",
            "information belongs to someone else",
        ),
    }
)

# Historical complaint forms sometimes used an issue-level identity-theft
# category without a more specific sub-issue.  Service complaints about an
# identity-theft monitoring product are deliberately not included here.
CFPB_EXPLICIT_IDENTITY_THEFT_ISSUES = frozenset(
    {
        "identity theft / fraud / embezzlement",
    }
)

# Restricted to property intrusion/damage, theft, robbery, and weapons.  Broad
# public-order and personal-safety buckets (for example, "Violence and sexual
# offences") are intentionally excluded.
POLICE_UK_PHYSICAL_SECURITY_CATEGORIES = frozenset(
    {
        "bicycle theft",
        "burglary",
        "criminal damage and arson",
        "possession of weapons",
        "robbery",
        "theft from the person",
        "vehicle crime",
    }
)

POLICE_UK_HAZARD_LABELS = {
    "bicycle theft": "Bicycle theft",
    "burglary": "Property intrusion / burglary",
    "criminal damage and arson": "Property damage / arson",
    "possession of weapons": "Weapons threat",
    "robbery": "Robbery / violent theft",
    "theft from the person": "Theft from person",
    "vehicle crime": "Vehicle theft / tampering",
}

DuplicateKey = Callable[[str], Optional[Hashable]]

_IDENTITY_THEFT_PATTERN = re.compile(r"\bidentity[\s-]+theft\b", re.IGNORECASE)
_VALUE_TOKEN_PATTERN = re.compile(r"^(?:x{2,}|.*\d.*)$", re.IGNORECASE)


def cfpb_template_fingerprint(narrative: str) -> str:
    """Return a stable key that collapses common masked/numbered templates.

    CFPB narratives frequently replace account numbers with runs of ``X``.
    Exact hashing treats otherwise identical form text as different when only a
    masked account number or date changes.  This fingerprint canonicalises such
    variable tokens before hashing.  Callers may pass a different
    ``near_duplicate_key`` to the builder, or ``None`` to disable this layer.
    """

    tokens = normalized_for_hash(narrative).split()
    canonical: list[str] = []
    for token in tokens:
        replacement = "<value>" if _VALUE_TOKEN_PATTERN.fullmatch(token) else token
        if canonical and replacement == "<value>" == canonical[-1]:
            continue
        canonical.append(replacement)
    return hashlib.sha256(" ".join(canonical).encode("utf-8")).hexdigest()


def _input_files(path: Path, directory_csv_suffix: str | None) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_dir():
        raise ValueError(f"Expected a CSV, ZIP, or directory: {path}")

    files: list[Path] = []
    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file():
            continue
        suffix = candidate.suffix.casefold()
        if suffix == ".zip":
            files.append(candidate)
        elif suffix == ".csv" and (
            directory_csv_suffix is None
            or candidate.name.casefold().endswith(directory_csv_suffix.casefold())
        ):
            files.append(candidate)
    return files


def _iter_csv_rows(
    path: Path,
    *,
    zip_member_predicate: Callable[[str], bool],
    directory_csv_suffix: str | None = None,
) -> Iterator[dict[str, str]]:
    files = _input_files(path, directory_csv_suffix)
    if not files:
        raise ValueError(f"No matching CSV or ZIP inputs found under {path}")

    matched_sources = 0
    for source_path in files:
        if source_path.suffix.casefold() == ".csv":
            matched_sources += 1
            with source_path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
                yield from csv.DictReader(stream)
            continue

        if source_path.suffix.casefold() != ".zip":
            continue
        with zipfile.ZipFile(source_path) as archive:
            members = sorted(
                member
                for member in archive.namelist()
                if not member.endswith("/")
                and not member.startswith("__MACOSX/")
                and zip_member_predicate(member)
            )
            for member in members:
                matched_sources += 1
                with archive.open(member) as binary_stream:
                    text_stream = io.TextIOWrapper(
                        binary_stream,
                        encoding="utf-8-sig",
                        errors="replace",
                        newline="",
                    )
                    yield from csv.DictReader(text_stream)

    if matched_sources == 0:
        raise ValueError(f"No matching CSV members found in inputs under {path}")


def _push_bounded(
    heap: list[tuple[int, str, IncidentRecord]],
    capacity: int,
    record: IncidentRecord,
) -> None:
    item = (-record.sampling_rank, record.case_no, record)
    if len(heap) < capacity:
        heapq.heappush(heap, item)
        return
    worst_rank = -heap[0][0]
    if record.sampling_rank < worst_rank:
        heapq.heapreplace(heap, item)


def _finalize_heap(heap: list[tuple[int, str, IncidentRecord]]) -> list[IncidentRecord]:
    return sorted(
        (item[2] for item in heap),
        key=lambda record: (record.sampling_rank, record.case_no),
    )


def _validate_builder_arguments(capacity: int, minimum_words: int) -> None:
    if capacity <= 0:
        raise ValueError("capacity must be greater than zero")
    if minimum_words < 0:
        raise ValueError("minimum_words cannot be negative")


def _normalised_classification(row: dict[str, str]) -> tuple[str, str]:
    return (
        clean_text(row.get("Issue")).casefold(),
        clean_text(row.get("Sub-issue")).casefold(),
    )


def _is_cfpb_identity_theft(row: dict[str, str], narrative: str) -> bool:
    issue, sub_issue = _normalised_classification(row)
    explicitly_classified = (
        issue in CFPB_EXPLICIT_IDENTITY_THEFT_ISSUES
        or "identity theft" in sub_issue
    )
    contextual_with_narrative_evidence = (
        (issue, sub_issue) in CFPB_CONTEXTUAL_IDENTITY_THEFT_CLASSIFICATIONS
        and _IDENTITY_THEFT_PATTERN.search(narrative) is not None
    )
    return explicitly_classified or contextual_with_narrative_evidence


def _json_fields(row: dict[str, str], field_names: tuple[str, ...]) -> str:
    values = {
        field_name: clean_text(row.get(field_name))
        for field_name in field_names
        if clean_text(row.get(field_name))
    }
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_cfpb_identity_theft_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
    *,
    exact_duplicate_key: DuplicateKey = text_digest,
    near_duplicate_key: DuplicateKey | None = cfpb_template_fingerprint,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    """Build Information Security records from authentic CFPB narratives.

    ``exact_duplicate_key`` and ``near_duplicate_key`` are explicit hooks for
    corpus-wide deduplication policy.  The default near-duplicate hook catches
    common form narratives that differ only in redacted or numeric tokens.
    """

    _validate_builder_arguments(capacity, minimum_words)
    candidate_heap: list[tuple[int, str, IncidentRecord]] = []
    seen_source_ids: set[str] = set()
    seen_exact: set[Hashable] = set()
    seen_near: set[Hashable] = set()
    stats = {
        "rows_read": 0,
        "missing_id": 0,
        "not_identity_theft": 0,
        "empty_or_short": 0,
        "non_english": 0,
        "duplicate_source_id": 0,
        "exact_duplicates": 0,
        "near_duplicates": 0,
        "eligible_unique": 0,
    }

    rows = _iter_csv_rows(
        Path(path),
        zip_member_predicate=lambda name: name.casefold().endswith(".csv"),
    )
    for row in rows:
        if maximum_rows is not None and stats["rows_read"] >= maximum_rows:
            break
        stats["rows_read"] += 1

        source_id = clean_text(row.get("Complaint ID"))
        if not source_id:
            stats["missing_id"] += 1
            continue
        if source_id in seen_source_ids:
            stats["duplicate_source_id"] += 1
            continue
        seen_source_ids.add(source_id)

        narrative = clean_text(row.get("Consumer complaint narrative"))
        if not _is_cfpb_identity_theft(row, narrative):
            stats["not_identity_theft"] += 1
            continue
        if word_count(narrative) < minimum_words:
            stats["empty_or_short"] += 1
            continue
        if not is_probably_english(narrative, minimum_source_words=5):
            stats["non_english"] += 1
            continue

        exact_key = exact_duplicate_key(narrative)
        if exact_key is not None and exact_key in seen_exact:
            stats["exact_duplicates"] += 1
            continue
        if exact_key is not None:
            seen_exact.add(exact_key)

        near_key = near_duplicate_key(narrative) if near_duplicate_key is not None else None
        if near_key is not None and near_key in seen_near:
            stats["near_duplicates"] += 1
            continue
        if near_key is not None:
            seen_near.add(near_key)

        product = clean_text(row.get("Product"))
        sub_product = clean_text(row.get("Sub-product"))
        issue = clean_text(row.get("Issue"))
        sub_issue = clean_text(row.get("Sub-issue"))
        classification = sub_issue or issue or "Identity theft"
        title = f"Identity theft complaint — {classification}"
        hazard_parts = ["Identity theft / personal data misuse"]
        if product:
            hazard_parts.append(product)
        if sub_product:
            hazard_parts.append(sub_product)

        record = IncidentRecord(
            case_no=f"CFPB:{source_id}",
            country=UNITED_STATES_COUNTRY,
            title=title,
            description=narrative,
            case_type="Information Security",
            hazard=" — ".join(hazard_parts),
            hazard_type="Cyber—Identity Theft/Fraud",
            actual_severity="Unknown",
            potential_severity="Unknown",
            source=CFPB_SOURCE,
            source_record_id=source_id,
            source_url=CFPB_DETAIL_URL.format(complaint_id=source_id),
            source_license=CFPB_SOURCE_LICENSE,
            event_date=clean_text(row.get("Date received")),
            raw_case_type=_json_fields(
                row,
                ("Product", "Sub-product", "Issue", "Sub-issue"),
            ),
            raw_hazard=_json_fields(
                row,
                ("Company", "State", "ZIP code", "Tags"),
            ),
            raw_severity=_json_fields(
                row,
                (
                    "Company response to consumer",
                    "Timely response?",
                    "Consumer disputed?",
                ),
            ),
            title_provenance="deterministic_from_CFPB_issue_and_sub_issue",
            hazard_label_method="fixed_identity_theft_mapping_from_CFPB_classification_and_narrative",
            actual_severity_label_method="not_available_in_source",
            potential_severity_label_method="not_available_in_source",
            text_hash=text_digest(narrative),
            sampling_rank=sampling_rank(seed, CFPB_SOURCE, source_id),
        )
        stats["eligible_unique"] += 1
        _push_bounded(candidate_heap, capacity, record)

    stats["candidates_returned"] = len(candidate_heap)
    return _finalize_heap(candidate_heap), stats


def format_police_uk_description(row: dict[str, str]) -> str:
    """Render Police.uk fields with a fixed, non-AI description template."""

    source_id = clean_text(row.get("Crime ID")) or "not published"
    crime_type = clean_text(row.get("Crime type")) or "unspecified crime"
    month = clean_text(row.get("Month")) or "an unspecified month"
    reported_by = clean_text(row.get("Reported by")) or "not published"
    falls_within = clean_text(row.get("Falls within")) or "not published"
    location = clean_text(row.get("Location")) or "not published"
    outcome = clean_text(row.get("Last outcome category")) or "not available"
    context = clean_text(row.get("Context"))
    description = (
        f"Police.uk records a {crime_type.lower()} crime for {month}. "
        f"The reporting force was {reported_by}, within {falls_within}. "
        f"The source publishes the approximate location as {location}. "
        f"The latest available outcome category is {outcome}. "
        f"The published source Crime ID is {source_id}."
    )
    if context:
        description += f" The source context field states: {context}."
    return clean_text(description)


def build_police_uk_physical_security_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
    *,
    allowed_categories: AbstractSet[str] | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    """Build Physical Security records from Police.uk street-crime CSVs.

    ZIP inputs may be a complete Police.uk bulk archive; only ``*-street.csv``
    members are read.  Directory inputs likewise ignore outcomes and stop/search
    CSVs.  A directly supplied CSV is accepted regardless of its filename.
    """

    _validate_builder_arguments(capacity, minimum_words)
    allowed = {
        clean_text(value).casefold()
        for value in (allowed_categories or POLICE_UK_PHYSICAL_SECURITY_CATEGORIES)
    }
    candidate_heap: list[tuple[int, str, IncidentRecord]] = []
    seen_source_ids: set[str] = set()
    stats = {
        "rows_read": 0,
        "missing_id": 0,
        "not_allowed_category": 0,
        "duplicate_source_id": 0,
        "empty_or_short": 0,
        "eligible_unique": 0,
    }

    source_path = Path(path)
    directory_suffix = "-street.csv" if source_path.is_dir() else None
    rows = _iter_csv_rows(
        source_path,
        zip_member_predicate=lambda name: name.casefold().endswith("-street.csv"),
        directory_csv_suffix=directory_suffix,
    )
    for row in rows:
        if maximum_rows is not None and stats["rows_read"] >= maximum_rows:
            break
        stats["rows_read"] += 1

        raw_category = clean_text(row.get("Crime type"))
        category = raw_category.casefold()
        if category not in allowed:
            stats["not_allowed_category"] += 1
            continue
        source_id = clean_text(row.get("Crime ID"))
        if not source_id:
            stats["missing_id"] += 1
            continue
        if source_id in seen_source_ids:
            stats["duplicate_source_id"] += 1
            continue
        seen_source_ids.add(source_id)

        description = format_police_uk_description(row)
        if word_count(description) < minimum_words:
            stats["empty_or_short"] += 1
            continue

        location = clean_text(row.get("Location")) or "Approximate location not published"
        hazard_label = POLICE_UK_HAZARD_LABELS.get(category, raw_category)
        record = IncidentRecord(
            case_no=f"POLICE-UK:{source_id}",
            country="United Kingdom",
            title=f"{raw_category} — {location}",
            description=description,
            case_type="Physical Security",
            hazard=f"{hazard_label} — {location}",
            hazard_type="Physical Security",
            actual_severity="Unknown",
            potential_severity="Unknown",
            source=POLICE_UK_SOURCE,
            source_record_id=source_id,
            source_url=POLICE_UK_SOURCE_URL,
            source_license=POLICE_UK_SOURCE_LICENSE,
            event_date=clean_text(row.get("Month")),
            raw_case_type=raw_category,
            raw_hazard=_json_fields(
                row,
                (
                    "Reported by",
                    "Falls within",
                    "Longitude",
                    "Latitude",
                    "Location",
                    "LSOA code",
                    "LSOA name",
                    "Context",
                ),
            ),
            raw_severity=clean_text(row.get("Last outcome category")),
            title_provenance="deterministic_from_PoliceUK_crime_type_and_approximate_location",
            hazard_label_method="fixed_mapping_from_PoliceUK_crime_type",
            actual_severity_label_method="not_available_in_source",
            potential_severity_label_method="not_available_in_source",
            # The fixed Description includes the source Crime ID, so distinct
            # source records remain distinct while the standard text hash stays
            # reproducible from Description alone for release QA.
            text_hash=text_digest(description),
            sampling_rank=sampling_rank(seed, POLICE_UK_SOURCE, source_id),
        )
        stats["eligible_unique"] += 1
        _push_bounded(candidate_heap, capacity, record)

    stats["candidates_returned"] = len(candidate_heap)
    return _finalize_heap(candidate_heap), stats


__all__ = [
    "CFPB_SOURCE_LICENSE",
    "CFPB_SOURCE_URL",
    "POLICE_UK_PHYSICAL_SECURITY_CATEGORIES",
    "POLICE_UK_SOURCE_LICENSE",
    "POLICE_UK_SOURCE_URL",
    "build_cfpb_identity_theft_candidates",
    "build_police_uk_physical_security_candidates",
    "cfpb_template_fingerprint",
    "format_police_uk_description",
]
