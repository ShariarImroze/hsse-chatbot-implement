import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from incident_pipeline.database import load_sqlite
from incident_pipeline.text import text_digest


def _provenance_record(case_no: str, description: str) -> dict[str, object]:
    source_id = case_no.split(":", 1)[-1]
    return {
        "Case No": case_no,
        "Country": "United States",
        "Title": f"Hydraulic equipment event {source_id}",
        "Description": description,
        "Case Type": "Asset and Reputation Damage/Loss",
        "Hazard": "Hydraulic equipment failure",
        "Hazard Type": "Asset Damage/Loss",
        "Actual Severity": "Unknown",
        "Potential Severity": "Unknown",
        "source": "TEST_SOURCE",
        "source_record_id": source_id,
        "source_url": "https://example.test/source",
        "source_license": "Test fixture",
        "event_date": "2025-01-02",
        "raw_case_type": "fixture",
        "raw_hazard": "hydraulic",
        "raw_severity": "unknown",
        "title_provenance": "test_fixture",
        "hazard_label_method": "test_fixture",
        "actual_severity_label_method": "test_fixture",
        "potential_severity_label_method": "test_fixture",
        "text_hash": text_digest(description),
        "sampling_rank": 1,
        "dataset_split": "train",
    }


def _write_provenance(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


class AtomicDatabaseLoadTests(unittest.TestCase):
    def test_build_is_self_contained_verified_and_replaceable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "records.jsonl"
            database_path = root / "hsse_incidents.sqlite"
            first_description = (
                "The technician found a hydraulic pump defect and replaced the damaged "
                "component before returning the equipment to operational service."
            )
            _write_provenance(
                input_path,
                [_provenance_record("TEST:ONE", first_description)],
            )

            first_result = load_sqlite(input_path, database_path)

            self.assertEqual(first_result["rows"], 1)
            self.assertEqual(first_result["sources"], {"TEST_SOURCE": 1})
            self.assertTrue(database_path.exists())
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{database_path}{suffix}").exists())
            self.assertEqual(
                list(root.glob(f".{database_path.name}.*.building")),
                [],
            )

            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
                if first_result["full_text_search_enabled"]:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM incidents_fts "
                            "WHERE incidents_fts MATCH 'hydraulic'"
                        ).fetchone()[0],
                        1,
                    )
            finally:
                connection.close()

            second_description = (
                "A separate inspection found that the hydraulic valve had failed, and the "
                "maintenance team installed a serviceable replacement before operation resumed."
            )
            _write_provenance(
                input_path,
                [_provenance_record("TEST:TWO", second_description)],
            )
            second_result = load_sqlite(input_path, database_path)

            self.assertEqual(second_result["rows"], 1)
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT case_no FROM incidents").fetchone()[0],
                    "TEST:TWO",
                )
            finally:
                connection.close()

    def test_busy_destination_wal_refuses_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "records.jsonl"
            database_path = root / "hsse_incidents.sqlite"
            _write_provenance(
                input_path,
                [
                    _provenance_record(
                        "TEST:NEW",
                        "The replacement input contains a valid hydraulic equipment report "
                        "that must not overwrite a database while its WAL is busy.",
                    )
                ],
            )

            existing = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    existing.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal",
                )
                existing.execute("CREATE TABLE sentinel(value TEXT)")
                existing.execute("INSERT INTO sentinel VALUES ('committed')")
                existing.commit()
                existing.execute("BEGIN IMMEDIATE")
                existing.execute("INSERT INTO sentinel VALUES ('uncommitted')")

                with self.assertRaisesRegex(RuntimeError, "checkpoint.*busy"):
                    load_sqlite(input_path, database_path)
                self.assertEqual(
                    list(root.glob(f".{database_path.name}.*.building")),
                    [],
                )
                self.assertEqual(
                    existing.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE name='incidents'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                existing.rollback()
                existing.close()

            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT value FROM sentinel").fetchall(),
                    [("committed",)],
                )
            finally:
                connection.close()

    def test_failed_verification_leaves_existing_destination_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "records.jsonl"
            database_path = root / "hsse_incidents.sqlite"
            _write_provenance(
                input_path,
                [
                    _provenance_record(
                        "TEST:NEW",
                        "The build is valid but a forced verification failure must prevent "
                        "replacement of the existing destination database file.",
                    )
                ],
            )
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE sentinel(value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('original')")
            connection.commit()
            connection.close()

            with mock.patch(
                "incident_pipeline.database._verify_standalone_database",
                side_effect=RuntimeError("forced verification failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "forced verification failure"):
                    load_sqlite(input_path, database_path)

            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT value FROM sentinel").fetchall(),
                    [("original",)],
                )
            finally:
                connection.close()
            self.assertEqual(
                list(root.glob(f".{database_path.name}.*.building")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
