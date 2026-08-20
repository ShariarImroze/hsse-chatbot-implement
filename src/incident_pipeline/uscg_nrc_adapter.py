"""Adapter for USCG National Response Center process-safety notifications.

NRC publishes relational Excel workbooks.  This adapter keeps only source
incident types that describe fixed process equipment or containment systems:
``FIXED``, ``STORAGE TANK``, ``PIPELINE``, and ``PLATFORM``.  NRC warns that
the records are initial caller reports and have not been validated or
investigated by a response agency.
"""

from __future__ import annotations

import csv
import heapq
import io
import re
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree

from .models import UNITED_STATES_COUNTRY, IncidentRecord
from .taxonomy import classify_hazard, occupational_potential_severity
from .text import (
    clean_text,
    combine_distinct,
    is_explicit_non_release,
    is_probably_english,
    sampling_rank,
    text_digest,
    word_count,
)


NRC_SOURCE_URL = "https://nrc.uscg.mil/"
NRC_SOURCE_LICENSE = (
    "Public USCG FOIA incident data; no explicit dataset license is stated; "
    "caller-authored text and source-specific terms may apply. NRC states "
    "that these initial reports are not validated or investigated"
)
NRC_PROCESS_INCIDENT_TYPES = frozenset(
    {"FIXED", "STORAGE TANK", "PIPELINE", "PLATFORM"}
)

_MAIN_SHEET = "INCIDENT_COMMONS"
_DETAIL_SHEET = "INCIDENT_DETAILS"
_EQUIPMENT_SHEET = "INCIDENTS"
_MATERIAL_SHEET = "MATERIAL_INVOLVED"
_XML_NAMESPACE = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATIONSHIP_NAMESPACE = (
    "{http://schemas.openxmlformats.org/package/2006/relationships}"
)
_OFFICE_RELATIONSHIP_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)
_COLUMN_PATTERN = re.compile(r"[A-Z]+")


def _cell_column(reference: str) -> str:
    match = _COLUMN_PATTERN.match(reference)
    return match.group(0) if match else ""


class _StreamingXlsx:
    """Small dependency-free reader for the row-oriented NRC workbooks."""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self._sheet_paths = self._read_sheet_paths()
        self._shared_strings = self._read_shared_strings()

    def _read_sheet_paths(self) -> dict[str, str]:
        workbook = ElementTree.fromstring(self.archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            self.archive.read("xl/_rels/workbook.xml.rels")
        )
        target_by_id = {
            relationship.get("Id", ""): relationship.get("Target", "")
            for relationship in relationships.iter(
                f"{_RELATIONSHIP_NAMESPACE}Relationship"
            )
        }
        result: dict[str, str] = {}
        for sheet in workbook.iter(f"{_XML_NAMESPACE}sheet"):
            relationship_id = sheet.get(_OFFICE_RELATIONSHIP_ID, "")
            target = target_by_id.get(relationship_id, "")
            if not target:
                continue
            if target.startswith("/"):
                normalized = target.lstrip("/")
            elif target.startswith("xl/"):
                normalized = target
            else:
                normalized = f"xl/{target}"
            result[sheet.get("name", "")] = normalized
        return result

    def _read_shared_strings(self) -> list[str]:
        member = "xl/sharedStrings.xml"
        if member not in self.archive.namelist():
            return []
        root = ElementTree.fromstring(self.archive.read(member))
        return [
            "".join(node.text or "" for node in item.iter(f"{_XML_NAMESPACE}t"))
            for item in root.iter(f"{_XML_NAMESPACE}si")
        ]

    def has_sheet(self, sheet_name: str) -> bool:
        return sheet_name in self._sheet_paths

    def _cell_value(self, cell: ElementTree.Element) -> str:
        cell_type = cell.get("t", "")
        if cell_type == "inlineStr":
            return "".join(
                node.text or "" for node in cell.iter(f"{_XML_NAMESPACE}t")
            )
        value_node = cell.find(f"{_XML_NAMESPACE}v")
        value = "" if value_node is None else value_node.text or ""
        if cell_type == "s" and value:
            return self._shared_strings[int(value)]
        return value

    def rows(self, sheet_name: str) -> Iterator[dict[str, str]]:
        member = self._sheet_paths[sheet_name]
        headers: dict[str, str] = {}
        with self.archive.open(member) as stream:
            for _, row in ElementTree.iterparse(stream, events=("end",)):
                if row.tag != f"{_XML_NAMESPACE}row":
                    continue
                values = {
                    _cell_column(cell.get("r", "")): self._cell_value(cell)
                    for cell in row.findall(f"{_XML_NAMESPACE}c")
                }
                if not headers:
                    headers = {
                        column: clean_text(value)
                        for column, value in values.items()
                        if clean_text(value)
                    }
                else:
                    yield {
                        header: values.get(column, "")
                        for column, header in headers.items()
                    }
                row.clear()


@contextmanager
def _open_xlsx(path: Path) -> Iterator[_StreamingXlsx]:
    if path.suffix.casefold() == ".xlsx":
        with zipfile.ZipFile(path) as workbook_archive:
            yield _StreamingXlsx(workbook_archive)
        return

    with zipfile.ZipFile(path) as outer_archive:
        members = [
            member
            for member in outer_archive.namelist()
            if not member.endswith("/") and member.casefold().endswith(".xlsx")
        ]
        if len(members) != 1:
            raise ValueError(
                f"Expected one XLSX workbook in NRC archive {path}, found {len(members)}"
            )
        workbook_bytes = io.BytesIO(outer_archive.read(members[0]))
        with zipfile.ZipFile(workbook_bytes) as workbook_archive:
            yield _StreamingXlsx(workbook_archive)


def _discover_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    supported = {".csv", ".xlsx", ".zip"}
    return sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.casefold() in supported
    )


def _iter_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        yield from csv.DictReader(stream)


def _truthy(value: object) -> bool:
    return clean_text(value).casefold() in {"yes", "y", "true", "1"}


def _number(value: object) -> float:
    cleaned = clean_text(value).replace(",", "").replace("$", "")
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def _actual_severity(details: dict[str, str]) -> str:
    fatalities = max(
        _number(details.get("NUMBER_FATALITIES")),
        _number(details.get("EMPL_FATALITY")),
        _number(details.get("PASS_FATALITY")),
        _number(details.get("OCCUPANT_FATALITY")),
    )
    if fatalities > 0 or _truthy(details.get("ANY_FATALITIES")):
        return "Severe"

    injuries = max(
        _number(details.get("NUMBER_INJURED")),
        _number(details.get("NUMBER_HOSPITALIZED")),
        _number(details.get("EMPLOYEE_INJURIES")),
        _number(details.get("PASSENGER_INJURIES")),
    )
    evacuations = _number(details.get("NUMBER_EVACUATED"))
    if (
        injuries > 0
        or evacuations > 0
        or _truthy(details.get("ANY_INJURIES"))
        or _truthy(details.get("ANY_EVACUATIONS"))
    ):
        return "Medium"

    known_flags = [
        clean_text(details.get(field)).casefold()
        for field in (
            "ANY_FATALITIES",
            "ANY_INJURIES",
            "ANY_EVACUATIONS",
            "ANY_DAMAGES",
        )
        if clean_text(details.get(field))
    ]
    if known_flags and all(value in {"no", "n", "false", "0"} for value in known_flags):
        return "Low"
    return "Unknown"


def _severity_evidence(details: dict[str, str]) -> str:
    fields = (
        "ANY_FATALITIES",
        "NUMBER_FATALITIES",
        "ANY_INJURIES",
        "NUMBER_INJURED",
        "NUMBER_HOSPITALIZED",
        "ANY_EVACUATIONS",
        "NUMBER_EVACUATED",
        "ANY_DAMAGES",
        "DAMAGE_AMOUNT",
    )
    return " | ".join(
        f"{field}={clean_text(details.get(field))}"
        for field in fields
        if clean_text(details.get(field))
    )


def _material_evidence(rows: list[dict[str, str]]) -> tuple[list[str], str]:
    names: list[str] = []
    evidence: list[str] = []
    for row in rows:
        name = clean_text(row.get("NAME_OF_MATERIAL"))
        if name and name not in names:
            names.append(name)
        parts = [
            f"material={name}" if name else "",
            (
                f"amount={clean_text(row.get('AMOUNT_OF_MATERIAL'))} "
                f"{clean_text(row.get('UNIT_OF_MEASURE'))}"
            ).strip()
            if clean_text(row.get("AMOUNT_OF_MATERIAL"))
            else "",
            f"reached_water={clean_text(row.get('IF_REACHED_WATER'))}"
            if clean_text(row.get("IF_REACHED_WATER"))
            else "",
        ]
        item = "; ".join(part for part in parts if part)
        if item:
            evidence.append(item)
    return names, " | ".join(evidence[:5])


def _load_one_to_one(
    workbook: _StreamingXlsx,
    sheet_name: str,
    wanted_ids: set[str],
) -> dict[str, dict[str, str]]:
    if not workbook.has_sheet(sheet_name):
        return {}
    result: dict[str, dict[str, str]] = {}
    for row in workbook.rows(sheet_name):
        source_id = clean_text(row.get("SEQNOS"))
        if source_id in wanted_ids and source_id not in result:
            result[source_id] = row
    return result


def _load_materials(
    workbook: _StreamingXlsx,
    wanted_ids: set[str],
) -> dict[str, list[dict[str, str]]]:
    if not workbook.has_sheet(_MATERIAL_SHEET):
        return {}
    result: dict[str, list[dict[str, str]]] = {}
    for row in workbook.rows(_MATERIAL_SHEET):
        source_id = clean_text(row.get("SEQNOS"))
        if source_id in wanted_ids:
            result.setdefault(source_id, []).append(row)
    return result


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
    elif record.sampling_rank < -heap[0][0]:
        heapq.heapreplace(heap, item)


def build_nrc_process_safety_candidates(
    path: str,
    capacity: int,
    seed: int,
    minimum_words: int,
    maximum_rows: int | None = None,
) -> tuple[list[IncidentRecord], dict[str, int]]:
    """Build deterministic Process Safety candidates from NRC bulk files."""
    source = "USCG_NRC"
    heap: list[tuple[int, str, IncidentRecord]] = []
    seen_source_ids: set[str] = set()
    statistics = {
        "source_files_read": 0,
        "rows_read": 0,
        "wrong_incident_type": 0,
        "missing_source_id": 0,
        "empty_or_short": 0,
        "non_english": 0,
        "explicit_non_release": 0,
        "duplicate_source_ids": 0,
        "eligible_unique": 0,
    }
    stop = False

    for source_path in _discover_files(Path(path)):
        if stop:
            break
        statistics["source_files_read"] += 1

        if source_path.suffix.casefold() == ".csv":
            row_iterator = _iter_csv(source_path)
            workbook_context = None
            workbook = None
        else:
            workbook_context = _open_xlsx(source_path)
            workbook = workbook_context.__enter__()
            if not workbook.has_sheet(_MAIN_SHEET):
                workbook_context.__exit__(None, None, None)
                continue
            row_iterator = workbook.rows(_MAIN_SHEET)

        try:
            base_rows: dict[str, dict[str, str]] = {}
            for row in row_iterator:
                if maximum_rows is not None and statistics["rows_read"] >= maximum_rows:
                    stop = True
                    break
                statistics["rows_read"] += 1
                incident_type = clean_text(row.get("TYPE_OF_INCIDENT")).upper()
                if incident_type not in NRC_PROCESS_INCIDENT_TYPES:
                    statistics["wrong_incident_type"] += 1
                    continue
                source_id = clean_text(row.get("SEQNOS"))
                if not source_id:
                    statistics["missing_source_id"] += 1
                    continue
                if source_id in seen_source_ids or source_id in base_rows:
                    statistics["duplicate_source_ids"] += 1
                    continue
                base_rows[source_id] = row

            wanted_ids = set(base_rows)
            if workbook is None:
                details_by_id: dict[str, dict[str, str]] = {}
                equipment_by_id: dict[str, dict[str, str]] = {}
                materials_by_id: dict[str, list[dict[str, str]]] = {}
            else:
                details_by_id = _load_one_to_one(
                    workbook,
                    _DETAIL_SHEET,
                    wanted_ids,
                )
                equipment_by_id = _load_one_to_one(
                    workbook,
                    _EQUIPMENT_SHEET,
                    wanted_ids,
                )
                materials_by_id = _load_materials(workbook, wanted_ids)

            for source_id, row in base_rows.items():
                details = details_by_id.get(source_id, {})
                equipment = equipment_by_id.get(source_id, {})
                description = combine_distinct(
                    [
                        row.get("DESCRIPTION_OF_INCIDENT"),
                        details.get("ADDITIONAL_INFO"),
                        details.get("DESC_REMEDIAL_ACTION"),
                    ]
                )
                if word_count(description) < minimum_words:
                    statistics["empty_or_short"] += 1
                    continue
                if not is_probably_english(description, minimum_source_words=5):
                    statistics["non_english"] += 1
                    continue
                if is_explicit_non_release(description):
                    statistics["explicit_non_release"] += 1
                    continue

                seen_source_ids.add(source_id)
                incident_type = clean_text(row.get("TYPE_OF_INCIDENT")).upper()
                incident_cause = clean_text(row.get("INCIDENT_CAUSE"))
                material_names, material_raw = _material_evidence(
                    materials_by_id.get(source_id, [])
                )
                equipment_values = [
                    clean_text(equipment.get(field))
                    for field in (
                        "TYPE_OF_FIXED_OBJECT",
                        "PIPELINE_TYPE",
                        "DESCRIPTION_OF_TANK",
                        "PLATFORM_RIG_NAME",
                    )
                    if clean_text(equipment.get(field))
                ]
                hazard_type = classify_hazard(
                    incident_cause,
                    *material_names,
                    *equipment_values,
                    description,
                )
                if hazard_type == "Other/Unknown":
                    hazard_type = "Loss of Containment/Release"
                hazard_parts = material_names[:3] + equipment_values[:2]
                if incident_cause and incident_cause.upper() != "UNKNOWN":
                    hazard_parts.append(incident_cause)
                hazard = " — ".join(hazard_parts) or f"{incident_type.title()} event"
                actual_severity = _actual_severity(details)
                title = incident_type.title()
                if incident_cause and incident_cause.upper() != "UNKNOWN":
                    title += f" — {incident_cause.title()}"
                else:
                    title += " process-safety notification"

                location_raw = " | ".join(
                    value
                    for value in (
                        clean_text(row.get("INCIDENT_LOCATION")),
                        clean_text(row.get("LOCATION_NEAREST_CITY")),
                        clean_text(row.get("LOCATION_STATE")),
                        clean_text(row.get("LOCATION_COUNTY")),
                    )
                    if value
                )
                raw_hazard = " | ".join(
                    value
                    for value in (
                        f"incident_type={incident_type}",
                        f"cause={incident_cause}" if incident_cause else "",
                        f"location={location_raw}" if location_raw else "",
                        material_raw,
                        "equipment=" + "; ".join(equipment_values)
                        if equipment_values
                        else "",
                    )
                    if value
                )
                record = IncidentRecord(
                    case_no=f"USCG-NRC:{source_id}",
                    country=UNITED_STATES_COUNTRY,
                    title=title,
                    description=description,
                    case_type="Process Safety",
                    hazard=hazard,
                    hazard_type=hazard_type,
                    actual_severity=actual_severity,
                    potential_severity=occupational_potential_severity(
                        actual_severity,
                        hazard_type,
                    ),
                    source=source,
                    source_record_id=source_id,
                    source_url=NRC_SOURCE_URL,
                    source_license=NRC_SOURCE_LICENSE,
                    event_date=clean_text(row.get("INCIDENT_DATE_TIME")),
                    raw_case_type=incident_type,
                    raw_hazard=raw_hazard,
                    raw_severity=_severity_evidence(details),
                    title_provenance="deterministic_from_NRC_incident_type_and_cause",
                    hazard_label_method="rule_from_NRC_type_cause_material_and_equipment",
                    actual_severity_label_method="source_consequence_fields_rule",
                    potential_severity_label_method="documented_hazard_matrix_rule",
                    text_hash=text_digest(description),
                    sampling_rank=sampling_rank(seed, source, source_id),
                )
                statistics["eligible_unique"] += 1
                _push_bounded(heap, capacity, record)
        finally:
            if workbook_context is not None:
                workbook_context.__exit__(None, None, None)

    records = sorted(
        (item[2] for item in heap),
        key=lambda record: (record.sampling_rank, record.case_no),
    )
    statistics["candidates_returned"] = len(records)
    return records, statistics
