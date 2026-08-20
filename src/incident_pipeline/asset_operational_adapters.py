"""Standalone adapters for authentic asset- and operational-loss records.

The FAA adapter consumes the agency's annual Service Difficulty Report (SDR)
CSV files.  An SDR is a processed report of an aircraft malfunction, failure,
or defect.  ``Discrepancy`` is retained as the report description; no incident
narrative is generated.

The FDA adapter consumes openFDA Recall Enterprise System (RES) bulk JSON
downloads.  It keeps ``reason_for_recall`` first and only appends other
source-authored recall fields without truncation.  The native ``recall_number``
is the record grain: a single RES event can legitimately contain several
distinct product recalls.

Both builders are deliberately independent of the shared pipeline and
taxonomy modules.  They accept individual files or directories of official
bulk-download files and use bounded, deterministic sampling.
"""

from __future__ import annotations

import csv
import heapq
import io
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .models import UNITED_STATES_COUNTRY, IncidentRecord, canonicalize_country
from .text import (
    clean_text,
    combine_distinct,
    is_probably_english,
    sampling_rank,
    text_digest,
    word_count,
)


FAA_SDR_SOURCE = "FAA_SDR"
FAA_SDR_SOURCE_URL = "https://www.faa.gov/av-info/download_SDR"
FAA_SDR_SOURCE_LICENSE = (
    "United States government public data; source-specific terms apply"
)

FDA_RES_SOURCE = "FDA_RES"
FDA_RES_SOURCE_URL = "https://open.fda.gov/apis/"
FDA_RES_SOURCE_LICENSE = (
    "Public domain under Creative Commons CC0 1.0 Universal unless otherwise noted"
)
FDA_ENFORCEMENT_URLS = {
    "food": "https://open.fda.gov/apis/food/enforcement/",
    "drug": "https://open.fda.gov/apis/drug/enforcement/",
    "device": "https://open.fda.gov/apis/device/enforcement/",
}

_JSON_ARRAY_PATTERN = re.compile(r'"(?:results|data|records)"\s*:\s*\[')
_US_ALIASES = {
    "u s",
    "u s a",
    "us",
    "usa",
    "united states",
    "united states of america",
}


def _validate_builder_arguments(
    capacity: int,
    minimum_words: int,
    maximum_rows: int | None,
) -> None:
    if capacity <= 0:
        raise ValueError("capacity must be greater than zero")
    if minimum_words < 0:
        raise ValueError("minimum_words cannot be negative")
    if maximum_rows is not None and maximum_rows < 0:
        raise ValueError("maximum_rows cannot be negative")


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


def _finalize_heap(
    heap: list[tuple[int, str, IncidentRecord]],
) -> list[IncidentRecord]:
    return sorted(
        (item[2] for item in heap),
        key=lambda record: (record.sampling_rank, record.case_no),
    )


def _files_with_suffixes(path: Path, suffixes: set[str]) -> list[Path]:
    if path.is_file():
        if path.suffix.casefold() not in suffixes:
            expected = ", ".join(sorted(suffixes))
            raise ValueError(f"Expected one of {expected}: {path}")
        return [path]
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_dir():
        raise ValueError(f"Expected a file or directory: {path}")
    files = [
        candidate
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file() and candidate.suffix.casefold() in suffixes
    ]
    if not files:
        expected = ", ".join(sorted(suffixes))
        raise ValueError(f"No {expected} inputs found under {path}")
    return files


def _iter_faa_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    files = _files_with_suffixes(path, {".csv", ".zip"})
    matched_sources = 0
    for source_path in files:
        if source_path.suffix.casefold() == ".csv":
            matched_sources += 1
            with source_path.open(
                encoding="utf-8-sig",
                errors="replace",
                newline="",
            ) as stream:
                yield from csv.DictReader(stream)
            continue

        with zipfile.ZipFile(source_path) as archive:
            members = sorted(
                name
                for name in archive.namelist()
                if not name.endswith("/") and name.casefold().endswith(".csv")
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
        raise ValueError(f"No CSV inputs found under {path}")


def _fill_buffer(stream: TextIO, buffer: str, minimum: int = 1) -> tuple[str, bool]:
    eof = False
    while len(buffer) < minimum:
        chunk = stream.read(64 * 1024)
        if not chunk:
            eof = True
            break
        buffer += chunk
    return buffer, eof


def _iter_json_array(stream: TextIO) -> Iterator[dict[str, Any]]:
    """Incrementally yield objects from a bare or wrapped JSON array.

    Official device-enforcement JSON expands to hundreds of megabytes.  The
    standard library's ``json.load`` would retain that entire document, so this
    small streaming decoder searches for the top-level results array and then
    decodes one object at a time.
    """

    decoder = json.JSONDecoder()
    buffer = ""
    array_start: int | None = None
    eof = False

    while array_start is None:
        chunk = stream.read(64 * 1024)
        if not chunk:
            eof = True
            break
        buffer += chunk
        stripped = buffer.lstrip("\ufeff \t\r\n")
        if stripped.startswith("["):
            array_start = len(buffer) - len(stripped) + 1
            break
        match = _JSON_ARRAY_PATTERN.search(buffer)
        if match:
            array_start = match.end()
            break
        # Keep enough overlap for a field name split across read boundaries.
        if len(buffer) > 256 * 1024:
            buffer = buffer[-256:]

    if array_start is None:
        if eof:
            raise ValueError("JSON input does not contain a results array")
        raise ValueError("Could not locate JSON results array")

    position = array_start
    while True:
        while True:
            if position >= len(buffer):
                if eof:
                    raise ValueError("Unexpected end of JSON results array")
                buffer = ""
                position = 0
                buffer, eof = _fill_buffer(stream, buffer)
                continue
            character = buffer[position]
            if character in " \t\r\n,":
                position += 1
                continue
            if character == "]":
                return
            break

        try:
            value, end = decoder.raw_decode(buffer, position)
        except json.JSONDecodeError:
            if eof:
                raise ValueError("Malformed JSON object in results array") from None
            buffer = buffer[position:]
            position = 0
            chunk = stream.read(64 * 1024)
            if not chunk:
                eof = True
            else:
                buffer += chunk
            continue

        position = end
        if isinstance(value, dict):
            yield value
        if position > 256 * 1024:
            buffer = buffer[position:]
            position = 0


def _iter_json_lines(stream: TextIO) -> Iterator[dict[str, Any]]:
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Malformed JSONL record on line {line_number}") from error
        if isinstance(value, dict):
            yield value


def _iter_fda_json_rows(path: Path) -> Iterator[dict[str, Any]]:
    files = _files_with_suffixes(path, {".json", ".jsonl", ".zip"})
    matched_sources = 0
    for source_path in files:
        suffix = source_path.suffix.casefold()
        if suffix in {".json", ".jsonl"}:
            matched_sources += 1
            with source_path.open(encoding="utf-8-sig", errors="replace") as stream:
                if suffix == ".jsonl":
                    yield from _iter_json_lines(stream)
                else:
                    yield from _iter_json_array(stream)
            continue

        with zipfile.ZipFile(source_path) as archive:
            members = sorted(
                name
                for name in archive.namelist()
                if not name.endswith("/")
                and name.casefold().endswith((".json", ".jsonl"))
            )
            for member in members:
                matched_sources += 1
                with archive.open(member) as binary_stream:
                    text_stream = io.TextIOWrapper(
                        binary_stream,
                        encoding="utf-8-sig",
                        errors="replace",
                    )
                    if member.casefold().endswith(".jsonl"):
                        yield from _iter_json_lines(text_stream)
                    else:
                        yield from _iter_json_array(text_stream)
    if matched_sources == 0:
        raise ValueError(f"No JSON inputs found under {path}")


def _normalised_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).casefold())


def _row_value(row: dict[str, Any], *field_names: str) -> Any:
    exact_field_present = False
    for field_name in field_names:
        if field_name in row:
            exact_field_present = True
            if clean_text(row[field_name]):
                return row[field_name]
    if exact_field_present:
        return ""
    wanted = {_normalised_key(field_name) for field_name in field_names}
    for key, value in row.items():
        if _normalised_key(key) in wanted and clean_text(value):
            return value
    return ""


def _country(value: object, default: str = "Unknown") -> str:
    label = clean_text(value)
    if not label:
        return default
    if re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip() in _US_ALIASES:
        return UNITED_STATES_COUNTRY
    return canonicalize_country(label)


def _json_source_fields(row: dict[str, Any], field_names: tuple[str, ...]) -> str:
    fields = {
        field_name: clean_text(row.get(field_name))
        for field_name in field_names
        if clean_text(row.get(field_name))
    }
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _faa_title(row: dict[str, Any], source_id: str) -> str:
    make = clean_text(_row_value(row, "AircraftMake"))
    model = clean_text(_row_value(row, "AircraftModel"))
    aircraft = " ".join(value for value in (make, model) if value)
    part = clean_text(_row_value(row, "PartName", "ComponentName"))
    condition = clean_text(_row_value(row, "PartCondition"))
    subject = " — ".join(value for value in (aircraft, part, condition) if value)
    return subject or f"FAA service difficulty report {source_id}"


def build_faa_sdr_asset_reputation_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    """Build Asset and Reputation Damage/Loss records from FAA SDR CSVs.

    This builder uses the asset-loss side of the combined case-type definition;
    it makes no claim that an SDR documents reputational damage.
    """

    _validate_builder_arguments(capacity, minimum_words, maximum_rows)
    candidate_heap: list[tuple[int, str, IncidentRecord]] = []
    seen_source_ids: set[str] = set()
    seen_descriptions: set[str] = set()
    stats = {
        "rows_read": 0,
        "missing_source_id": 0,
        "empty_or_short": 0,
        "non_english": 0,
        "duplicate_source_ids": 0,
        "exact_duplicates": 0,
        "eligible_unique": 0,
    }

    for row in _iter_faa_csv_rows(Path(path)):
        if maximum_rows is not None and stats["rows_read"] >= maximum_rows:
            break
        stats["rows_read"] += 1

        source_id = clean_text(
            _row_value(row, "OperatorControlNumber", "UniqueControlNumber")
        )
        if not source_id:
            stats["missing_source_id"] += 1
            continue
        if source_id in seen_source_ids:
            stats["duplicate_source_ids"] += 1
            continue
        seen_source_ids.add(source_id)

        description = clean_text(_row_value(row, "Discrepancy", "ProblemDescription"))
        if word_count(description) < minimum_words:
            stats["empty_or_short"] += 1
            continue
        if not is_probably_english(description, minimum_source_words=5):
            stats["non_english"] += 1
            continue
        description_hash = text_digest(description)
        if description_hash in seen_descriptions:
            stats["exact_duplicates"] += 1
            continue
        seen_descriptions.add(description_hash)

        nature = " | ".join(
            value
            for value in (
                clean_text(_row_value(row, "NatureOfConditionA")),
                clean_text(_row_value(row, "NatureOfConditionB")),
                clean_text(_row_value(row, "NatureOfConditionC")),
            )
            if value
        )
        part_name = clean_text(_row_value(row, "PartName", "ComponentName"))
        part_condition = clean_text(_row_value(row, "PartCondition"))
        hazard_detail = " — ".join(
            value for value in (part_name, part_condition, nature) if value
        )
        hazard = hazard_detail or "Aircraft malfunction, failure, or defect"

        record = IncidentRecord(
            case_no=f"FAA-SDR:{source_id}",
            country=UNITED_STATES_COUNTRY,
            title=_faa_title(row, source_id),
            description=description,
            case_type="Asset and Reputation Damage/Loss",
            hazard=hazard,
            hazard_type="Asset Damage/Loss",
            actual_severity="Unknown",
            potential_severity="Unknown",
            source=FAA_SDR_SOURCE,
            source_record_id=source_id,
            source_url=FAA_SDR_SOURCE_URL,
            source_license=FAA_SDR_SOURCE_LICENSE,
            event_date=clean_text(_row_value(row, "DifficultyDate")),
            raw_case_type=clean_text(_row_value(row, "SDRType")),
            raw_hazard=_json_source_fields(
                row,
                (
                    "JASCCode",
                    "NatureOfConditionA",
                    "NatureOfConditionB",
                    "NatureOfConditionC",
                    "PartName",
                    "PartCondition",
                    "StageOfOperationCode",
                    "HowDiscoveredCode",
                ),
            ),
            raw_severity=_json_source_fields(
                row,
                (
                    "PrecautionaryProcedureA",
                    "PrecautionaryProcedureB",
                    "PrecautionaryProcedureC",
                    "PrecautionaryProcedureD",
                ),
            ),
            title_provenance="deterministic_from_FAA_aircraft_part_and_condition_fields",
            hazard_label_method="fixed_asset_loss_mapping_from_FAA_SDR_record",
            actual_severity_label_method="not_available_as_normalized_source_field",
            potential_severity_label_method="not_available_as_normalized_source_field",
            text_hash=description_hash,
            sampling_rank=sampling_rank(seed, FAA_SDR_SOURCE, source_id),
        )
        stats["eligible_unique"] += 1
        _push_bounded(candidate_heap, capacity, record)

    stats["candidates_returned"] = len(candidate_heap)
    return _finalize_heap(candidate_heap), stats


def _fda_severity(classification: str) -> str:
    normalised = _normalised_key(classification)
    return {
        "classi": "Severe",
        "classii": "Medium",
        "classiii": "Low",
    }.get(normalised, "Unknown")


def _fda_event_date(row: dict[str, Any]) -> str:
    """Prefer initiation date, but fall back when the source value is implausible."""

    for field in (
        "recall_initiation_date",
        "report_date",
        "center_classification_date",
    ):
        value = clean_text(_row_value(row, field))
        try:
            parsed = datetime.strptime(value, "%Y%m%d")
        except ValueError:
            continue
        if 1970 <= parsed.year <= 2030:
            return value
    return "Date not reported"


def build_fda_operational_loss_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    """Build Operational Loss records from openFDA RES enforcement JSON.

    Descriptions contain only complete source-authored values, in this order:
    recall reason, product description, lot/code information, and any additional
    code information.  Distinct recall numbers under one event remain distinct.
    """

    _validate_builder_arguments(capacity, minimum_words, maximum_rows)
    candidate_heap: list[tuple[int, str, IncidentRecord]] = []
    seen_source_ids: set[str] = set()
    seen_descriptions: set[str] = set()
    stats = {
        "rows_read": 0,
        "missing_source_id": 0,
        "empty_or_short": 0,
        "non_english": 0,
        "duplicate_source_ids": 0,
        "exact_duplicates": 0,
        "eligible_unique": 0,
    }

    for row in _iter_fda_json_rows(Path(path)):
        if maximum_rows is not None and stats["rows_read"] >= maximum_rows:
            break
        stats["rows_read"] += 1

        source_id = clean_text(_row_value(row, "recall_number"))
        if not source_id:
            stats["missing_source_id"] += 1
            continue
        if source_id in seen_source_ids:
            stats["duplicate_source_ids"] += 1
            continue
        seen_source_ids.add(source_id)

        reason = clean_text(_row_value(row, "reason_for_recall"))
        description = combine_distinct(
            [
                reason,
                _row_value(row, "product_description"),
                _row_value(row, "code_info"),
                _row_value(row, "more_code_info"),
            ]
        )
        if not reason or word_count(description) < minimum_words:
            stats["empty_or_short"] += 1
            continue
        if not is_probably_english(description, minimum_source_words=5):
            stats["non_english"] += 1
            continue
        description_hash = text_digest(description)
        if description_hash in seen_descriptions:
            stats["exact_duplicates"] += 1
            continue
        seen_descriptions.add(description_hash)

        product_description = clean_text(_row_value(row, "product_description"))
        classification = clean_text(_row_value(row, "classification"))
        product_type = clean_text(_row_value(row, "product_type"))
        severity = _fda_severity(classification)
        endpoint_url = FDA_ENFORCEMENT_URLS.get(
            product_type.casefold(),
            FDA_RES_SOURCE_URL,
        )
        hazard = " — ".join(
            value
            for value in (
                "FDA product recall",
                product_type,
                classification,
            )
            if value
        )

        record = IncidentRecord(
            case_no=f"FDA-RES:{source_id}",
            country=_country(_row_value(row, "country")),
            title=product_description or f"FDA recall {source_id}",
            description=description,
            case_type="Operational Loss",
            hazard=hazard,
            hazard_type="Operational Disruption/Loss",
            actual_severity=severity,
            potential_severity=severity,
            source=FDA_RES_SOURCE,
            source_record_id=source_id,
            source_url=endpoint_url,
            source_license=FDA_RES_SOURCE_LICENSE,
            event_date=_fda_event_date(row),
            raw_case_type=_json_source_fields(
                row,
                (
                    "event_id",
                    "product_type",
                    "voluntary_mandated",
                    "initial_firm_notification",
                    "status",
                    "recall_initiation_date",
                    "report_date",
                    "center_classification_date",
                ),
            ),
            raw_hazard=_json_source_fields(
                row,
                (
                    "classification",
                    "recalling_firm",
                    "product_quantity",
                    "distribution_pattern",
                ),
            ),
            raw_severity=classification or "Unknown",
            title_provenance="source_product_description_or_deterministic_recall_number_fallback",
            hazard_label_method="fixed_operational_loss_mapping_from_FDA_recall_record",
            actual_severity_label_method="rule_from_FDA_recall_classification",
            potential_severity_label_method="same_as_FDA_recall_classification_rule",
            text_hash=description_hash,
            sampling_rank=sampling_rank(seed, FDA_RES_SOURCE, source_id),
        )
        stats["eligible_unique"] += 1
        _push_bounded(candidate_heap, capacity, record)

    stats["candidates_returned"] = len(candidate_heap)
    return _finalize_heap(candidate_heap), stats


__all__ = [
    "FAA_SDR_SOURCE_LICENSE",
    "FAA_SDR_SOURCE_URL",
    "FDA_RES_SOURCE_LICENSE",
    "FDA_RES_SOURCE_URL",
    "build_faa_sdr_asset_reputation_candidates",
    "build_fda_operational_loss_candidates",
]
