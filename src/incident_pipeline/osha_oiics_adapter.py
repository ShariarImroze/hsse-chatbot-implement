"""OSHA annual ITA OIICS adapters for disjoint safety and health corpora.

The source narratives are employer-submitted OSHA Form 300/301 fields.  OSHA
and BLS added the ``*_pred`` OIICS fields with an automated coder, so these
labels are useful for deterministic corpus construction but are not expert
adjudications.  Current annual files that do not yet contain OIICS fields use
the employer-selected ``type_of_incident`` only for the disjoint Health codes
2--6; OIICS Nature always takes precedence when present.
"""

from __future__ import annotations

import csv
import heapq
import io
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import UNITED_STATES_COUNTRY, IncidentRecord
from .taxonomy import (
    classify_hazard,
    occupational_potential_severity,
    osha_actual_severity,
)
from .text import (
    clean_text,
    combine_distinct,
    is_probably_english,
    sampling_rank,
    text_digest,
    word_count,
)


OSHA_ITA_SOURCE_URL = "https://www.osha.gov/itadata"
OSHA_ITA_SOURCE_LICENSE = (
    "Public United States government data; OSHA does not state a specific "
    "dataset license on the download page; employer-submitted text and "
    "source-specific terms may apply. OIICS fields are machine-coded rather "
    "than expert-adjudicated; absent OIICS, Health uses the source-reported "
    "type_of_incident code"
)

_SAFETY_GROUPS = frozenset({"1"})
_HEALTH_GROUPS = frozenset({"2", "3", "4", "5", "6"})
_HEALTH_FALLBACK_TYPES = frozenset({"2", "3", "4", "5", "6"})
_TYPE_LABELS = {
    "1": "Injury",
    "2": "Skin disorder",
    "3": "Respiratory condition",
    "4": "Poisoning",
    "5": "Hearing loss",
    "6": "Other illness",
}
_YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _event_date(value: object) -> str:
    cleaned = clean_text(value)
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%d%b%Y"):
        try:
            return datetime.strptime(cleaned, date_format).date().isoformat()
        except ValueError:
            pass
    return cleaned or "Date not reported"


def _discover_input_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.casefold() not in {".csv", ".zip"}:
            raise ValueError(f"Unsupported OSHA input file: {path}")
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)

    files = sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.casefold() in {".csv", ".zip"}
    )
    if not files:
        raise ValueError(f"No OSHA ZIP or CSV files found under {path}")
    return files


def _source_year(row: dict[str, str], source_path: Path) -> str:
    """Return the filing year, preferring a source field over filename/date hints."""
    for value in (
        row.get("year_of_filing"),
        row.get("year_filing_for"),
        source_path.name,
        row.get("date_of_incident"),
    ):
        match = _YEAR_PATTERN.search(clean_text(value))
        if match:
            return match.group(1)
    return "unknown"


def _iter_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    def normalized_rows(text_stream: io.TextIOBase) -> Iterator[dict[str, str]]:
        reader = csv.reader(text_stream)
        try:
            headers = [clean_text(value).casefold() for value in next(reader)]
        except StopIteration:
            return
        for values in reader:
            yield {
                header: values[index] if index < len(values) else ""
                for index, header in enumerate(headers)
                if header
            }

    if path.suffix.casefold() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [
                member
                for member in archive.namelist()
                if not member.endswith("/") and member.casefold().endswith(".csv")
            ]
            if len(members) != 1:
                raise ValueError(
                    f"Expected one CSV member in OSHA archive {path}, found {len(members)}"
                )
            with archive.open(members[0]) as binary_stream:
                text_stream = io.TextIOWrapper(
                    binary_stream,
                    encoding="utf-8-sig",
                    errors="replace",
                    newline="",
                )
                yield from normalized_rows(text_stream)
        return

    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        yield from normalized_rows(stream)


def _iter_input_rows(path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    for source_path in _discover_input_files(path):
        for row in _iter_csv_rows(source_path):
            yield source_path, row


def _push_bounded(
    heap: list[tuple[int, str, IncidentRecord]],
    capacity: int,
    record: IncidentRecord,
) -> None:
    if capacity <= 0:
        return
    item = (-record.sampling_rank, record.case_no, record)
    if len(heap) < capacity:
        heapq.heappush(heap, item)
        return
    if record.sampling_rank < -heap[0][0]:
        heapq.heapreplace(heap, item)


def _build_osha_oiics_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    allowed_groups: frozenset[str],
    allowed_fallback_types: frozenset[str],
    case_type: str,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    source = "OSHA_ITA_CASE_DETAIL"
    heap: list[tuple[int, str, IncidentRecord]] = []
    seen_source_ids: set[tuple[str, str]] = set()
    statistics = {
        "source_files_read": len(_discover_input_files(Path(path))),
        "rows_read": 0,
        "wrong_oiics_group": 0,
        "matched_by_oiics": 0,
        "matched_by_type_fallback": 0,
        "missing_source_id": 0,
        "empty_or_short": 0,
        "non_english": 0,
        "duplicate_source_ids": 0,
        "eligible_unique": 0,
    }

    for source_path, row in _iter_input_rows(Path(path)):
        if maximum_rows is not None and statistics["rows_read"] >= maximum_rows:
            break
        statistics["rows_read"] += 1

        nature_code = clean_text(row.get("nature_code_pred"))
        nature_group = nature_code[:1]
        source_incident_type = clean_text(row.get("type_of_incident"))
        if nature_code:
            if nature_group not in allowed_groups:
                statistics["wrong_oiics_group"] += 1
                continue
            classification_rule = "OIICS Nature major group"
            statistics["matched_by_oiics"] += 1
        else:
            if source_incident_type not in allowed_fallback_types:
                statistics["wrong_oiics_group"] += 1
                continue
            classification_rule = "source type_of_incident fallback (OIICS absent)"
            statistics["matched_by_type_fallback"] += 1

        native_source_id = clean_text(row.get("id"))
        if not native_source_id:
            statistics["missing_source_id"] += 1
            continue
        source_year = _source_year(row, source_path)
        source_key = (source_year, native_source_id)
        if source_key in seen_source_ids:
            statistics["duplicate_source_ids"] += 1
            continue
        seen_source_ids.add(source_key)
        source_id = f"{source_year}:{native_source_id}"

        description = combine_distinct(
            [
                row.get("new_incident_description"),
                row.get("new_nar_what_happened"),
                row.get("new_nar_before_incident"),
                row.get("new_nar_injury_illness"),
                row.get("new_nar_object_substance"),
                row.get("new_incident_location"),
            ]
        )
        if word_count(description) < minimum_words:
            statistics["empty_or_short"] += 1
            continue
        if not is_probably_english(description, minimum_source_words=5):
            statistics["non_english"] += 1
            continue

        event = clean_text(row.get("event_title_pred"))
        raw_nature = clean_text(row.get("nature_title_pred"))
        nature = raw_nature or _TYPE_LABELS.get(source_incident_type, "")
        source_object = clean_text(row.get("source_title_pred"))
        body_part = clean_text(row.get("part_title_pred"))
        title = ": ".join(value for value in (event, nature) if value)
        if not title:
            title = f"OSHA {case_type.lower()} incident"

        hazard_type = classify_hazard(event, source_object, nature, description)
        hazard = " — ".join(value for value in (event, source_object) if value)
        if not hazard:
            hazard = nature or "Unspecified occupational hazard"
        raw_outcome = clean_text(row.get("incident_outcome"))
        actual_severity = osha_actual_severity(raw_outcome)
        description_hash = text_digest(description)

        record = IncidentRecord(
            case_no=f"OSHA-ITA:{source_id}",
            country=UNITED_STATES_COUNTRY,
            title=title,
            description=description,
            case_type=case_type,
            hazard=hazard,
            hazard_type=hazard_type,
            actual_severity=actual_severity,
            potential_severity=occupational_potential_severity(
                actual_severity,
                hazard_type,
            ),
            source=source,
            source_record_id=source_id,
            source_url=OSHA_ITA_SOURCE_URL,
            source_license=OSHA_ITA_SOURCE_LICENSE,
            event_date=_event_date(row.get("date_of_incident")),
            raw_case_type=(
                f"osha_id={native_source_id}; filing_year={source_year}; "
                f"classification_rule={classification_rule}; "
                f"type_of_incident={source_incident_type or 'Unknown'}; "
                f"nature_code_pred={nature_code or 'Absent'}; "
                f"nature_title_pred={raw_nature or 'Absent'}"
            ),
            raw_hazard=" | ".join(
                value for value in (event, source_object, nature, body_part) if value
            ),
            raw_severity=raw_outcome,
            title_provenance="deterministic_from_source_classification_fields",
            hazard_label_method="rule_from_source_classification_fields_and_text",
            actual_severity_label_method="source_incident_outcome_code_rule",
            potential_severity_label_method="documented_hazard_matrix_rule",
            text_hash=description_hash,
            sampling_rank=sampling_rank(seed, source, source_id),
        )
        statistics["eligible_unique"] += 1
        _push_bounded(heap, capacity, record)

    records = sorted(
        (item[2] for item in heap),
        key=lambda record: (record.sampling_rank, record.case_no),
    )
    statistics["candidates_returned"] = len(records)
    return records, statistics


def build_osha_safety_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    """Build Occupational Safety records from OIICS Nature major group 1."""
    return _build_osha_oiics_candidates(
        path,
        capacity,
        seed,
        minimum_words,
        _SAFETY_GROUPS,
        frozenset(),
        "Occupational Safety",
        maximum_rows,
    )


def build_osha_health_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    """Build Occupational Health records from OIICS Nature major groups 2-6."""
    return _build_osha_oiics_candidates(
        path,
        capacity,
        seed,
        minimum_words,
        _HEALTH_GROUPS,
        _HEALTH_FALLBACK_TYPES,
        "Occupational Health",
        maximum_rows,
    )
