"""Source-specific adapters for OSHA, MSHA, and VCDB public incident data."""

from __future__ import annotations

import csv
import glob
import heapq
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .models import UNITED_STATES_COUNTRY, IncidentRecord, canonicalize_country
from .taxonomy import (
    classify_hazard,
    msha_actual_severity,
    occupational_case_type,
    occupational_potential_severity,
    osha_actual_severity,
    vcdb_action_details,
    vcdb_actual_severity,
    vcdb_hazard_type,
    vcdb_potential_severity,
)
from .text import (
    clean_text,
    combine_distinct,
    is_probably_english,
    sampling_rank,
    text_digest,
    word_count,
)


OSHA_SOURCE_URL = "https://www.osha.gov/Establishment-Specific-Injury-and-Illness-Data"
MSHA_SOURCE_URL = "https://arlweb.msha.gov/OpenGovernmentData/OGIMSHA.asp"
VCDB_SOURCE_URL = "https://github.com/vz-risk/VCDB"

OSHA_INCIDENT_TYPES = {
    "1": "Injury",
    "2": "Skin disorder",
    "3": "Respiratory condition",
    "4": "Poisoning",
    "5": "Hearing loss",
    "6": "All other illness",
}

SOURCE_SENTINELS = {"", "NO VALUE FOUND", "NOT REPORTED", "NOT APPLICABLE"}


def _source_label(value: object) -> str:
    """Remove administrative sentinel values from reader-facing labels."""
    cleaned = clean_text(value)
    return "" if cleaned.upper() in SOURCE_SENTINELS else cleaned


def _iter_zipped_csv(path: Path, delimiter: str) -> Iterator[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        members = [member for member in archive.namelist() if not member.endswith("/")]
        if len(members) != 1:
            raise ValueError(f"Expected one tabular file in {path}, found {len(members)}")
        with archive.open(members[0]) as binary_stream:
            text_stream = io.TextIOWrapper(binary_stream, encoding="utf-8-sig", errors="replace", newline="")
            yield from csv.DictReader(text_stream, delimiter=delimiter)


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
    return sorted((item[2] for item in heap), key=lambda record: (record.sampling_rank, record.case_no))


def build_osha_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    source = "OSHA_ITA"
    candidate_heap: list[tuple[int, str, IncidentRecord]] = []
    seen_descriptions: set[str] = set()
    stats = {"rows_read": 0, "empty_or_short": 0, "exact_duplicates": 0, "eligible_unique": 0}

    for row in _iter_zipped_csv(Path(path), delimiter=","):
        if maximum_rows is not None and stats["rows_read"] >= maximum_rows:
            break
        stats["rows_read"] += 1
        description = combine_distinct(
            [
                row.get("NEW_INCIDENT_DESCRIPTION"),
                row.get("NEW_NAR_WHAT_HAPPENED"),
                row.get("NEW_NAR_BEFORE_INCIDENT"),
                row.get("NEW_NAR_INJURY_ILLNESS"),
                row.get("NEW_NAR_OBJECT_SUBSTANCE"),
                row.get("NEW_INCIDENT_LOCATION"),
            ]
        )
        if word_count(description) < minimum_words:
            stats["empty_or_short"] += 1
            continue
        description_hash = text_digest(description)
        if description_hash in seen_descriptions:
            stats["exact_duplicates"] += 1
            continue
        seen_descriptions.add(description_hash)

        source_id = clean_text(row.get("id"))
        if not source_id:
            continue
        event = clean_text(row.get("event_title_pred"))
        nature = clean_text(row.get("nature_title_pred"))
        source_object = clean_text(row.get("source_title_pred"))
        part = clean_text(row.get("part_title_pred"))
        title_parts = [value for value in (event, nature) if value]
        title = ": ".join(title_parts) if title_parts else "Occupational injury or illness incident"
        hazard_type = classify_hazard(event, source_object, nature, description)
        raw_type_code = clean_text(row.get("type_of_incident"))
        raw_type = OSHA_INCIDENT_TYPES.get(raw_type_code, "Unknown")
        actual = osha_actual_severity(clean_text(row.get("incident_outcome")))
        potential = occupational_potential_severity(actual, hazard_type)
        hazard = " — ".join(value for value in (event, source_object) if value) or "Unspecified occupational hazard"

        record = IncidentRecord(
            case_no=f"OSHA-ITA:{source_id}",
            country=UNITED_STATES_COUNTRY,
            title=title,
            description=description,
            case_type=occupational_case_type(
                raw_type_code,
                raw_type,
                hazard_type,
            ),
            hazard=hazard,
            hazard_type=hazard_type,
            actual_severity=actual,
            potential_severity=potential,
            source=source,
            source_record_id=source_id,
            source_url=OSHA_SOURCE_URL,
            source_license="United States government public data; source-specific terms apply",
            event_date=clean_text(row.get("date_of_incident")),
            raw_case_type=raw_type,
            raw_hazard=" | ".join(value for value in (event, source_object, nature, part) if value),
            raw_severity=clean_text(row.get("incident_outcome")),
            title_provenance="deterministic_from_source_event_and_nature",
            hazard_label_method="rule_from_OIICS_source_fields",
            actual_severity_label_method="source_code_rule",
            potential_severity_label_method="documented_hazard_matrix_rule",
            text_hash=description_hash,
            sampling_rank=sampling_rank(seed, source, source_id),
        )
        stats["eligible_unique"] += 1
        _push_bounded(candidate_heap, capacity, record)

    stats["candidates_returned"] = len(candidate_heap)
    return _finalize_heap(candidate_heap), stats


def build_msha_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    source = "MSHA"
    candidate_heap: list[tuple[int, str, IncidentRecord]] = []
    seen_descriptions: set[str] = set()
    stats = {"rows_read": 0, "empty_or_short": 0, "exact_duplicates": 0, "eligible_unique": 0}

    for row in _iter_zipped_csv(Path(path), delimiter="|"):
        if maximum_rows is not None and stats["rows_read"] >= maximum_rows:
            break
        stats["rows_read"] += 1
        narrative = clean_text(row.get("NARRATIVE"))
        description = combine_distinct(
            [
                narrative,
                f"Activity: {clean_text(row.get('ACTIVITY'))}" if clean_text(row.get("ACTIVITY")) not in {"", "NO VALUE FOUND"} else "",
                f"Equipment or source: {clean_text(row.get('INJURY_SOURCE'))}" if clean_text(row.get("INJURY_SOURCE")) not in {"", "NO VALUE FOUND"} else "",
                f"Nature of injury: {clean_text(row.get('NATURE_INJURY'))}" if clean_text(row.get("NATURE_INJURY")) not in {"", "NO VALUE FOUND"} else "",
                f"Affected body part: {clean_text(row.get('INJ_BODY_PART'))}" if clean_text(row.get("INJ_BODY_PART")) not in {"", "NO VALUE FOUND"} else "",
            ]
        )
        if word_count(narrative) < 5 or word_count(description) < minimum_words:
            stats["empty_or_short"] += 1
            continue
        description_hash = text_digest(narrative)
        if description_hash in seen_descriptions:
            stats["exact_duplicates"] += 1
            continue
        seen_descriptions.add(description_hash)

        source_id = clean_text(row.get("DOCUMENT_NO"))
        if not source_id:
            continue
        classification = _source_label(row.get("CLASSIFICATION")).title()
        accident_type = _source_label(row.get("ACCIDENT_TYPE"))
        injury_source = _source_label(row.get("INJURY_SOURCE")).title()
        degree = clean_text(row.get("DEGREE_INJURY"))
        title = " — ".join(value for value in (classification, accident_type) if value) or "Mining accident or occupational illness"
        hazard_type = classify_hazard(classification, accident_type, injury_source, narrative)
        actual = msha_actual_severity(degree, clean_text(row.get("DAYS_LOST")), clean_text(row.get("DAYS_RESTRICT")))
        potential = occupational_potential_severity(actual, hazard_type)
        raw_type = classification or "Mining accident or illness"
        hazard = " — ".join(value for value in (classification, accident_type, injury_source) if value) or "Unspecified mining hazard"

        record = IncidentRecord(
            case_no=f"MSHA:{source_id}",
            country=UNITED_STATES_COUNTRY,
            title=title,
            description=description,
            case_type=occupational_case_type(
                "",
                f"{classification} {degree}",
                hazard_type,
            ),
            hazard=hazard,
            hazard_type=hazard_type,
            actual_severity=actual,
            potential_severity=potential,
            source=source,
            source_record_id=source_id,
            source_url=MSHA_SOURCE_URL,
            source_license="United States government public data; source-specific terms apply",
            event_date=clean_text(row.get("ACCIDENT_DT")),
            raw_case_type=raw_type,
            raw_hazard=" | ".join(value for value in (classification, accident_type, injury_source) if value),
            raw_severity=degree,
            title_provenance="deterministic_from_source_classification_and_accident_type",
            hazard_label_method="rule_from_MSHA_classification_fields",
            actual_severity_label_method="source_degree_and_lost_days_rule",
            potential_severity_label_method="documented_hazard_matrix_rule",
            text_hash=text_digest(description),
            sampling_rank=sampling_rank(seed, source, source_id),
        )
        stats["eligible_unique"] += 1
        _push_bounded(candidate_heap, capacity, record)

    stats["candidates_returned"] = len(candidate_heap)
    return _finalize_heap(candidate_heap), stats


def _discover_vcdb_json_files(root: Path) -> list[Path]:
    patterns = (
        str(root / "data" / "json" / "validated" / "*.json"),
        str(root / "**" / "data" / "json" / "validated" / "*.json"),
    )
    discovered: set[str] = set()
    for pattern in patterns:
        discovered.update(glob.glob(pattern, recursive=True))
    return [Path(path) for path in sorted(discovered)]


def _vcdb_country_labels(root: Path) -> dict[str, str]:
    candidates = list(root.glob("vcdb-labels.json")) + list(root.glob("**/vcdb-labels.json"))
    if not candidates:
        return {}
    with candidates[0].open(encoding="utf-8") as stream:
        labels = json.load(stream)
    return labels.get("victim", {}).get("country", {})


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def build_vcdb_candidates(
    root: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    source = "VCDB"
    root_path = Path(root)
    candidate_heap: list[tuple[int, str, IncidentRecord]] = []
    seen_narratives: set[str] = set()
    country_labels = _vcdb_country_labels(root_path)
    paths = _discover_vcdb_json_files(root_path)
    stats = {"rows_read": 0, "empty_or_short": 0, "non_english": 0, "exact_duplicates": 0, "eligible_unique": 0}

    for path in paths:
        if maximum_rows is not None and stats["rows_read"] >= maximum_rows:
            break
        stats["rows_read"] += 1
        try:
            with path.open(encoding="utf-8") as stream:
                row = json.load(stream)
        except (OSError, json.JSONDecodeError):
            stats["empty_or_short"] += 1
            continue

        base_narrative = clean_text(row.get("summary") or row.get("notes"))
        if word_count(base_narrative) < 5:
            stats["empty_or_short"] += 1
            continue
        if not is_probably_english(base_narrative, minimum_source_words=5):
            stats["non_english"] += 1
            continue
        narrative_hash = text_digest(base_narrative)
        if narrative_hash in seen_narratives:
            stats["exact_duplicates"] += 1
            continue
        seen_narratives.add(narrative_hash)

        # The validated filename/master identifier is the stable VCDB record key.
        # Some recent source files reuse malformed incident_id values across
        # otherwise distinct incidents, so incident_id cannot serve as a primary key.
        source_id = clean_text(path.stem)
        if not source_id:
            continue
        action = row.get("action", {}) if isinstance(row.get("action"), dict) else {}
        action_categories, action_varieties = vcdb_action_details(action)
        attributes = row.get("attribute", {}) if isinstance(row.get("attribute"), dict) else {}
        confidentiality = attributes.get("confidentiality", {}) if isinstance(attributes.get("confidentiality"), dict) else {}
        availability_block = attributes.get("availability", {}) if isinstance(attributes.get("availability"), dict) else {}
        availability = [clean_text(value) for value in _as_list(availability_block.get("variety")) if clean_text(value)]
        data_disclosure = clean_text(confidentiality.get("data_disclosure")) or "Unknown"
        data_total_value = confidentiality.get("data_total")
        try:
            data_total = int(data_total_value) if data_total_value is not None else None
        except (TypeError, ValueError):
            data_total = None
        impact = row.get("impact", {}) if isinstance(row.get("impact"), dict) else {}
        overall_rating = clean_text(impact.get("overall_rating")) or "Unknown"
        security_status = clean_text(row.get("security_incident")) or "Unknown"
        countries = [clean_text(value) for value in _as_list((row.get("victim") or {}).get("country")) if clean_text(value)] if isinstance(row.get("victim"), dict) else []
        country = canonicalize_country(
            "; ".join(country_labels.get(code, code) for code in countries)
        ) or "Unknown"

        category_text = ", ".join(action_categories) or "Unknown"
        variety_text = ", ".join(action_varieties) or "Unknown"
        availability_text = ", ".join(availability) or "not specified"
        structured_context = (
            f"VERIS records the action categories as {category_text}, with the varieties {variety_text}. "
            f"The incident status is {security_status}; data disclosure is {data_disclosure}; "
            f"availability impact is {availability_text}; and the source impact rating is {overall_rating}."
        )
        description = combine_distinct([base_narrative, structured_context])
        if word_count(description) < minimum_words:
            stats["empty_or_short"] += 1
            continue

        hazard_type = vcdb_hazard_type(action_categories, action_varieties, availability)
        actual = vcdb_actual_severity(overall_rating)
        potential = vcdb_potential_severity(
            actual,
            hazard_type,
            action_varieties,
            availability,
            data_total,
            data_disclosure,
        )
        primary_action = action_categories[0] if action_categories else "Unspecified"
        title = f"{primary_action} information security incident"
        hazard = f"{category_text} — {variety_text}"
        timeline = row.get("timeline", {}) if isinstance(row.get("timeline"), dict) else {}
        incident_time = timeline.get("incident", {}) if isinstance(timeline.get("incident"), dict) else {}
        event_date = "-".join(str(incident_time[key]) for key in ("year", "month", "day") if key in incident_time)

        record = IncidentRecord(
            case_no=f"VCDB:{source_id}",
            country=country,
            title=title,
            description=description,
            case_type="Information Security",
            hazard=hazard,
            hazard_type=hazard_type,
            actual_severity=actual,
            potential_severity=potential,
            source=source,
            source_record_id=source_id,
            source_url=VCDB_SOURCE_URL,
            source_license="Creative Commons Attribution-ShareAlike 4.0 International",
            event_date=event_date,
            raw_case_type=security_status,
            raw_hazard=f"actions={category_text}; varieties={variety_text}; availability={availability_text}",
            raw_severity=overall_rating,
            title_provenance="deterministic_from_VERIS_action_category",
            hazard_label_method="rule_from_VERIS_action_and_attribute_fields",
            actual_severity_label_method="source_impact_rating_rule",
            potential_severity_label_method="documented_VERIS_consequence_rule",
            text_hash=text_digest(description),
            sampling_rank=sampling_rank(seed, source, source_id),
        )
        stats["eligible_unique"] += 1
        _push_bounded(candidate_heap, capacity, record)

    stats["candidates_returned"] = len(candidate_heap)
    return _finalize_heap(candidate_heap), stats
