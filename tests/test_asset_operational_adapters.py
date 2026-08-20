import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from incident_pipeline.asset_operational_adapters import (
    FAA_SDR_SOURCE_URL,
    FDA_RES_SOURCE_LICENSE,
    build_faa_sdr_asset_reputation_candidates,
    build_fda_operational_loss_candidates,
)
from incident_pipeline.models import UNITED_STATES_COUNTRY
from incident_pipeline.text import clean_text, text_digest


class AssetOperationalAdapterTests(unittest.TestCase):
    def test_faa_sdr_uses_native_id_and_source_discrepancy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "SDR-2025.csv"
            field_names = [
                "OperatorControlNumber",
                "DifficultyDate",
                "SDRType",
                "AircraftMake",
                "AircraftModel",
                "PartName",
                "ComponentName",
                "PartCondition",
                "NatureOfConditionA",
                "JASCCode",
                "Discrepancy",
            ]
            source_narrative = (
                "During the scheduled inspection, the technician found that the hydraulic "
                "pump housing was cracked and leaking fluid, so the damaged component was "
                "removed from the aircraft and replaced before the next flight."
            )
            rows = [
                {
                    "OperatorControlNumber": "FAA-NATIVE-001",
                    "DifficultyDate": "01/02/2025",
                    "SDRType": "A",
                    "AircraftMake": "BOEING",
                    "AircraftModel": "737",
                    "PartName": "",
                    "ComponentName": "HYDRAULIC PUMP",
                    "PartCondition": "CRACKED",
                    "NatureOfConditionA": "F",
                    "JASCCode": "2910",
                    "Discrepancy": source_narrative,
                },
                {
                    "OperatorControlNumber": "FAA-NATIVE-002",
                    "DifficultyDate": "01/03/2025",
                    "AircraftMake": "AIRBUS",
                    "AircraftModel": "A320",
                    "PartName": "VALVE",
                    "PartCondition": "FAILED",
                    "Discrepancy": (
                        "The flight crew reported that the cabin pressure valve failed during "
                        "descent, and maintenance personnel confirmed the defect after landing "
                        "before replacing the valve and completing the required operational checks."
                    ),
                },
                {
                    "OperatorControlNumber": "FAA-NATIVE-003",
                    "Discrepancy": source_narrative,
                },
                {
                    "OperatorControlNumber": "FAA-NATIVE-004",
                    "Discrepancy": "Short defect note",
                },
                {
                    "OperatorControlNumber": "FAA-NATIVE-005",
                    "Discrepancy": (
                        "Falla mecanica observada durante inspeccion programada; componentes "
                        "danados requirieron reemplazo inmediato antes de continuar operaciones "
                        "normales bajo procedimientos establecidos por mantenimiento certificado."
                    ),
                },
            ]
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=field_names)
                writer.writeheader()
                writer.writerows(rows)

            records, statistics = build_faa_sdr_asset_reputation_candidates(
                str(path),
                capacity=10,
                seed=19,
                minimum_words=20,
            )

            self.assertEqual(
                {record.case_no for record in records},
                {"FAA-SDR:FAA-NATIVE-001", "FAA-SDR:FAA-NATIVE-002"},
            )
            first = next(
                record for record in records if record.source_record_id == "FAA-NATIVE-001"
            )
            cleaned_narrative = clean_text(source_narrative)
            self.assertEqual(first.description, cleaned_narrative)
            self.assertEqual(first.text_hash, text_digest(cleaned_narrative))
            self.assertEqual(first.case_type, "Asset and Reputation Damage/Loss")
            self.assertEqual(first.hazard_type, "Asset Damage/Loss")
            self.assertEqual(first.country, UNITED_STATES_COUNTRY)
            self.assertEqual(first.source_url, FAA_SDR_SOURCE_URL)
            self.assertIn("HYDRAULIC PUMP", first.title)
            self.assertEqual(statistics["exact_duplicates"], 1)
            self.assertEqual(statistics["empty_or_short"], 1)
            self.assertEqual(statistics["non_english"], 1)
            self.assertEqual(statistics["eligible_unique"], 2)

            sampled_once, _ = build_faa_sdr_asset_reputation_candidates(
                str(path), 1, 23, 20
            )
            sampled_twice, _ = build_faa_sdr_asset_reputation_candidates(
                str(path), 1, 23, 20
            )
            self.assertEqual(
                [(record.case_no, record.sampling_rank) for record in sampled_once],
                [(record.case_no, record.sampling_rank) for record in sampled_twice],
            )

    def test_fda_zip_keeps_distinct_recalls_from_the_same_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "food-enforcement.json.zip"
            common_reason = (
                "The firm recalled the product because routine testing found undeclared milk "
                "that was not identified on the package label and could affect allergic consumers."
            )
            results = [
                {
                    "recall_number": "F-1001-2025",
                    "event_id": "EVENT-SHARED",
                    "reason_for_recall": common_reason,
                    "product_description": (
                        "One pound retail packages of sesame crackers distributed under the "
                        "North Market brand in printed plastic bags."
                    ),
                    "code_info": "Lot 1001 with best by date 2025-10-01",
                    "classification": "Class I",
                    "product_type": "Food",
                    "country": "United States",
                    "recall_initiation_date": "19300102",
                    "report_date": "20250103",
                    "voluntary_mandated": "Voluntary: Firm initiated",
                    "status": "Ongoing",
                },
                {
                    "recall_number": "F-1002-2025",
                    "event_id": "EVENT-SHARED",
                    "reason_for_recall": common_reason,
                    "product_description": (
                        "Two pound food-service packages of sesame crackers distributed under "
                        "the North Market brand in sealed cartons."
                    ),
                    "code_info": "Lot 1002 with best by date 2025-10-02",
                    "classification": "Class II",
                    "product_type": "Food",
                    "country": "United States",
                    "recall_initiation_date": "20250102",
                },
                {
                    "recall_number": "F-1001-2025",
                    "event_id": "EVENT-OTHER",
                    "reason_for_recall": (
                        "The duplicate native recall number must not create another record even "
                        "when the source text is otherwise different and sufficiently long."
                    ),
                    "product_description": "A different product description for duplicate testing.",
                },
                {
                    "recall_number": "F-1003-2025",
                    "event_id": "EVENT-OTHER",
                    "reason_for_recall": "",
                    "product_description": (
                        "A long source product description cannot substitute for the required "
                        "source-authored reason for recall field in this adapter."
                    ),
                },
            ]
            document = {
                "meta": {"results": {"skip": 0, "limit": len(results)}},
                "results": results,
            }
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("food-enforcement.json", json.dumps(document))

            records, statistics = build_fda_operational_loss_candidates(
                str(path),
                capacity=10,
                seed=31,
                minimum_words=20,
            )

            self.assertEqual(
                {record.source_record_id for record in records},
                {"F-1001-2025", "F-1002-2025"},
            )
            self.assertEqual(
                {
                    json.loads(record.raw_case_type)["event_id"]
                    for record in records
                },
                {"EVENT-SHARED"},
            )
            first = next(
                record for record in records if record.source_record_id == "F-1001-2025"
            )
            second = next(
                record for record in records if record.source_record_id == "F-1002-2025"
            )
            self.assertTrue(first.description.startswith(common_reason))
            self.assertIn("Lot 1001", first.description)
            self.assertEqual(first.case_type, "Operational Loss")
            self.assertEqual(first.hazard_type, "Operational Disruption/Loss")
            self.assertEqual(first.actual_severity, "Severe")
            self.assertEqual(first.event_date, "20250103")
            self.assertEqual(second.actual_severity, "Medium")
            self.assertEqual(first.country, UNITED_STATES_COUNTRY)
            self.assertEqual(first.source_license, FDA_RES_SOURCE_LICENSE)
            self.assertEqual(statistics["duplicate_source_ids"], 1)
            self.assertEqual(statistics["empty_or_short"], 1)
            self.assertEqual(statistics["eligible_unique"], 2)


if __name__ == "__main__":
    unittest.main()
