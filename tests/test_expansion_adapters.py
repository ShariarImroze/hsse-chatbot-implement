import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from incident_pipeline.caloes_adapter import build_caloes_environment_candidates
from incident_pipeline.osha_oiics_adapter import (
    build_osha_health_candidates,
    build_osha_safety_candidates,
)
from incident_pipeline.uscg_nrc_adapter import build_nrc_process_safety_candidates
from incident_pipeline.text import clean_text


def _write_osha_zip(
    path: Path,
    filing_year: str = "2023",
    include_fallback_rows: bool = False,
) -> None:
    fieldnames = [
        "id",
        "year_filing_for",
        "date_of_incident",
        "incident_outcome",
        "type_of_incident",
        "NEW_INCIDENT_DESCRIPTION",
        "NEW_NAR_WHAT_HAPPENED",
        "NEW_NAR_BEFORE_INCIDENT",
        "NEW_NAR_INJURY_ILLNESS",
        "NEW_NAR_OBJECT_SUBSTANCE",
        "NEW_INCIDENT_LOCATION",
        "nature_code_pred",
        "nature_title_pred",
        "event_title_pred",
        "source_title_pred",
        "part_title_pred",
    ]
    rows = [
        {
            "id": "SAFETY-1",
            "year_filing_for": filing_year,
            "date_of_incident": "01/10/2023",
            "incident_outcome": "2",
            "type_of_incident": "1",
            "NEW_INCIDENT_DESCRIPTION": (
                "The employee was moving a loaded cart when it tipped and struck "
                "the worker on the leg."
            ),
            "nature_code_pred": "12",
            "nature_title_pred": "Traumatic injuries to muscles and joints",
            "event_title_pred": "Struck by object",
            "source_title_pred": "Carts and hand trucks",
            "part_title_pred": "Leg",
        },
        {
            "id": "HEALTH-1",
            "year_filing_for": filing_year,
            "date_of_incident": "02/11/2023",
            "incident_outcome": "4",
            "type_of_incident": "6",
            "NEW_INCIDENT_DESCRIPTION": (
                "The employee developed a viral respiratory illness after an "
                "exposure that occurred while working in the care unit."
            ),
            "nature_code_pred": "32",
            "nature_title_pred": "Viral diseases",
            "event_title_pred": "Exposure to infectious agent",
            "source_title_pred": "Viruses",
            "part_title_pred": "Respiratory system",
        },
        {
            "id": "NO-CODE",
            "year_filing_for": filing_year,
            "date_of_incident": "03/12/2023",
            "incident_outcome": "4",
            "type_of_incident": "1",
            "NEW_INCIDENT_DESCRIPTION": (
                "The employee reported an event but the official nature code was "
                "not populated in the source record."
            ),
            "nature_code_pred": "",
            "nature_title_pred": "",
            "event_title_pred": "Unknown event",
            "source_title_pred": "",
            "part_title_pred": "",
        },
    ]
    if include_fallback_rows:
        rows.extend(
            [
                {
                    "id": "HEALTH-FALLBACK",
                    "year_filing_for": filing_year,
                    "date_of_incident": f"04/13/{filing_year}",
                    "incident_outcome": "4",
                    "type_of_incident": "3",
                    "NEW_INCIDENT_DESCRIPTION": (
                        "The employee developed a respiratory condition after repeated "
                        "workplace exposure and received medical evaluation for the symptoms."
                    ),
                    "nature_code_pred": "",
                    "nature_title_pred": "",
                    "event_title_pred": "",
                    "source_title_pred": "",
                    "part_title_pred": "",
                },
                {
                    "id": "OIICS-PRECEDENCE",
                    "year_filing_for": filing_year,
                    "date_of_incident": f"05/14/{filing_year}",
                    "incident_outcome": "2",
                    "type_of_incident": "3",
                    "NEW_INCIDENT_DESCRIPTION": (
                        "The employee sustained a traumatic injury when equipment moved "
                        "unexpectedly during a routine material handling operation."
                    ),
                    "nature_code_pred": "12",
                    "nature_title_pred": "Traumatic injury",
                    "event_title_pred": "Contact with equipment",
                    "source_title_pred": "Machinery",
                    "part_title_pred": "Multiple body parts",
                },
            ]
        )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("osha_2023.csv", stream.getvalue())


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _worksheet_xml(headers: list[str], rows: list[list[str]]) -> str:
    xml_rows: list[str] = []
    for row_number, values in enumerate([headers, *rows], start=1):
        cells: list[str] = []
        for column_number, value in enumerate(values, start=1):
            if value == "":
                continue
            reference = f"{_column_name(column_number)}{row_number}"
            cells.append(
                f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            )
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )


def _write_nrc_xlsx(path: Path) -> None:
    sheets = [
        (
            "INCIDENT_COMMONS",
            [
                "SEQNOS",
                "DESCRIPTION_OF_INCIDENT",
                "TYPE_OF_INCIDENT",
                "INCIDENT_CAUSE",
                "INCIDENT_DATE_TIME",
                "INCIDENT_LOCATION",
                "LOCATION_NEAREST_CITY",
                "LOCATION_STATE",
                "LOCATION_COUNTY",
            ],
            [
                [
                    "1001",
                    (
                        "The caller reported that a valve failed and released chlorine "
                        "from the fixed processing unit inside the facility."
                    ),
                    "FIXED",
                    "EQUIPMENT FAILURE",
                    "01/15/2023 14:30",
                    "PROCESS BUILDING",
                    "HOUSTON",
                    "TX",
                    "HARRIS",
                ],
                [
                    "1002",
                    (
                        "The caller reported that a vessel released oil while it was "
                        "underway near the harbor entrance."
                    ),
                    "VESSEL",
                    "OPERATOR ERROR",
                    "01/16/2023 10:00",
                    "HARBOR",
                    "HOUSTON",
                    "TX",
                    "HARRIS",
                ],
            ],
        ),
        (
            "INCIDENT_DETAILS",
            [
                "SEQNOS",
                "ANY_INJURIES",
                "NUMBER_INJURED",
                "NUMBER_HOSPITALIZED",
                "ANY_FATALITIES",
                "NUMBER_FATALITIES",
                "ANY_EVACUATIONS",
                "NUMBER_EVACUATED",
                "ANY_DAMAGES",
                "ADDITIONAL_INFO",
                "DESC_REMEDIAL_ACTION",
            ],
            [
                [
                    "1001",
                    "YES",
                    "1",
                    "0",
                    "NO",
                    "0",
                    "NO",
                    "0",
                    "YES",
                    "The caller stated that one employee was treated at the scene.",
                    "The facility isolated the valve and secured the release.",
                ]
            ],
        ),
        (
            "INCIDENTS",
            [
                "SEQNOS",
                "TYPE_OF_FIXED_OBJECT",
                "PIPELINE_TYPE",
                "DESCRIPTION_OF_TANK",
                "PLATFORM_RIG_NAME",
            ],
            [["1001", "CHEMICAL PROCESSING UNIT", "", "", ""]],
        ),
        (
            "MATERIAL_INVOLVED",
            [
                "SEQNOS",
                "NAME_OF_MATERIAL",
                "AMOUNT_OF_MATERIAL",
                "UNIT_OF_MEASURE",
                "IF_REACHED_WATER",
            ],
            [["1001", "CHLORINE", "10", "POUNDS", "NO"]],
        ),
    ]
    workbook_sheets = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _, _) in enumerate(sheets, start=1)
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    relationships = "".join(
        (
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        for index in range(1, len(sheets) + 1)
    )
    relationships_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{relationships}</Relationships>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships_xml)
        for index, (_, headers, rows) in enumerate(sheets, start=1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(headers, rows),
            )


class ExpansionAdapterTests(unittest.TestCase):
    def test_osha_oiics_groups_produce_disjoint_exact_case_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "osha_2023.zip"
            _write_osha_zip(source_path)

            safety, safety_stats = build_osha_safety_candidates(
                str(source_path), 10, 17, 5
            )
            health, health_stats = build_osha_health_candidates(
                str(source_path), 10, 17, 5
            )

            self.assertEqual([record.source_record_id for record in safety], ["2023:SAFETY-1"])
            self.assertEqual([record.case_type for record in safety], ["Occupational Safety"])
            self.assertEqual(safety[0].event_date, "2023-01-10")
            self.assertEqual([record.source_record_id for record in health], ["2023:HEALTH-1"])
            self.assertEqual([record.case_type for record in health], ["Occupational Health"])
            self.assertEqual(safety_stats["eligible_unique"], 1)
            self.assertEqual(health_stats["eligible_unique"], 1)
            self.assertIn("nature_code_pred=32", health[0].raw_case_type)

    def test_osha_directory_scopes_reused_native_ids_by_filing_year(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_directory = Path(temporary_directory)
            _write_osha_zip(source_directory / "osha_2023.zip", "2023")
            _write_osha_zip(source_directory / "osha_2024.zip", "2024")

            records, statistics = build_osha_health_candidates(
                str(source_directory), 10, 19, 5
            )

            self.assertEqual(
                {record.source_record_id for record in records},
                {"2023:HEALTH-1", "2024:HEALTH-1"},
            )
            self.assertEqual(statistics["source_files_read"], 2)
            self.assertEqual(statistics["eligible_unique"], 2)

    def test_osha_type_fallback_is_health_only_and_oiics_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "osha_2025.csv.zip"
            _write_osha_zip(source_path, "2025", include_fallback_rows=True)

            safety, _ = build_osha_safety_candidates(str(source_path), 20, 29, 5)
            health, health_statistics = build_osha_health_candidates(
                str(source_path), 20, 29, 5
            )

            safety_ids = {record.source_record_id for record in safety}
            health_ids = {record.source_record_id for record in health}
            self.assertIn("2025:OIICS-PRECEDENCE", safety_ids)
            self.assertNotIn("2025:OIICS-PRECEDENCE", health_ids)
            self.assertIn("2025:HEALTH-FALLBACK", health_ids)
            self.assertNotIn("2025:HEALTH-FALLBACK", safety_ids)
            self.assertEqual(health_statistics["matched_by_type_fallback"], 1)
            fallback = next(
                record
                for record in health
                if record.source_record_id == "2025:HEALTH-FALLBACK"
            )
            self.assertIn("OIICS absent", fallback.raw_case_type)

    def test_nrc_keeps_only_process_equipment_types_and_joins_source_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "nrc.xlsx"
            _write_nrc_xlsx(source_path)

            records, statistics = build_nrc_process_safety_candidates(
                str(source_path), 10, 23, 5
            )

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.case_no, "USCG-NRC:1001")
            self.assertEqual(record.case_type, "Process Safety")
            self.assertEqual(record.actual_severity, "Medium")
            self.assertIn("one employee was treated", record.description)
            self.assertIn("material=CHLORINE", record.raw_hazard)
            self.assertIn("CHEMICAL PROCESSING UNIT", record.raw_hazard)
            self.assertEqual(statistics["wrong_incident_type"], 1)

    def test_caloes_preserves_caller_description_and_deduplicates_control_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "caloes.csv"
            headers = [
                "Control#",
                "Notified Date",
                "1. Substance",
                "1. Type",
                "Description",
                "Water?",
                "Water Way",
                "Location",
                "City",
                "County",
                "Incident Date",
                "Spill Site",
                "CAUSE",
                "Injuries YES/NO",
                "Injuries #",
                "Fatals YES/NO",
                "Fatals #",
                "Evacs YES/NO",
                "Evacs #",
                "Cleanup",
            ]
            authentic_description = (
                "Per the reporting party, a vehicle struck a transformer and oil "
                "entered the storm drain before the crew contained the release."
            )
            rows = [
                [
                    "'24-0001",
                    "12/31/2024",
                    "MINERAL OIL",
                    "PETROLEUM",
                    authentic_description,
                    "Yes",
                    "Storm Drain",
                    "100 Main Street",
                    "Sacramento",
                    "Sacramento County",
                    "12/31/2024",
                    "Road",
                    "Collision",
                    "No",
                    "0",
                    "No",
                    "0",
                    "No",
                    "0",
                    "Contractor",
                ],
                [
                    "'24-0001",
                    "12/31/2024",
                    "MINERAL OIL",
                    "PETROLEUM",
                    "The duplicate export row repeats the same source control number.",
                    "Yes",
                    "Storm Drain",
                    "100 Main Street",
                    "Sacramento",
                    "Sacramento County",
                    "12/31/2024",
                    "Road",
                    "Collision",
                    "No",
                    "0",
                    "No",
                    "0",
                    "No",
                    "0",
                    "Contractor",
                ],
            ]
            with source_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(headers)
                writer.writerows(rows)

            first, statistics = build_caloes_environment_candidates(
                str(source_path), 10, 31, 5
            )
            second, _ = build_caloes_environment_candidates(
                str(source_path), 10, 31, 5
            )

            self.assertEqual([record.case_no for record in first], ["CALOES:24-0001"])
            self.assertEqual([record.case_no for record in first], [record.case_no for record in second])
            self.assertEqual(first[0].description, clean_text(authentic_description))
            self.assertEqual(first[0].case_type, "Environment")
            self.assertEqual(first[0].hazard_type, "Environmental Pollution")
            self.assertEqual(first[0].actual_severity, "Low")
            self.assertEqual(first[0].event_date, "2024-12-31")
            self.assertTrue(first[0].raw_severity)
            self.assertEqual(statistics["duplicate_source_ids"], 1)

    def test_caloes_future_incident_year_falls_back_to_notification_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "caloes.csv"
            description = (
                "The reporting party observed transformer oil entering a storm "
                "drain after equipment fell from a truck during transport."
            )
            with source_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "Control#",
                        "Notified Date",
                        "Incident Date",
                        "1. Substance",
                        "1. Type",
                        "1. Quantity",
                        "Description",
                        "Water?",
                    ]
                )
                writer.writerow(
                    [
                        "'23-2942",
                        "05/01/2023",
                        "05/01/2029",
                        "Transformer oil",
                        "PETROLEUM",
                        "30",
                        description,
                        "Yes",
                    ]
                )

            records, _ = build_caloes_environment_candidates(
                str(source_path), 10, 41, 5
            )

            self.assertEqual(records[0].event_date, "2023-05-01")
            self.assertIn("incident_date=05/01/2029", records[0].raw_hazard)
            self.assertIn("notified_date=05/01/2023", records[0].raw_hazard)


if __name__ == "__main__":
    unittest.main()
