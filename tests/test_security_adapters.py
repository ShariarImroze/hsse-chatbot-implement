import tempfile
import unittest
import zipfile
from pathlib import Path

from incident_pipeline.security_adapters import (
    CFPB_SOURCE_LICENSE,
    POLICE_UK_SOURCE_LICENSE,
    build_cfpb_identity_theft_candidates,
    build_police_uk_physical_security_candidates,
    cfpb_template_fingerprint,
    format_police_uk_description,
)
from incident_pipeline.text import clean_text, word_count


FIXTURES = Path(__file__).parent / "fixtures"


class CfpbSecurityAdapterTests(unittest.TestCase):
    def test_authentic_narratives_are_filtered_and_deduplicated(self) -> None:
        records, statistics = build_cfpb_identity_theft_candidates(
            str(FIXTURES / "cfpb_identity_theft_tiny.csv"),
            capacity=10,
            seed=17,
            minimum_words=10,
        )

        self.assertEqual(statistics["rows_read"], 7)
        self.assertEqual(statistics["eligible_unique"], 2)
        self.assertEqual(statistics["near_duplicates"], 1)
        self.assertEqual(statistics["exact_duplicates"], 1)
        self.assertEqual(statistics["non_english"], 1)
        self.assertEqual(statistics["not_identity_theft"], 2)
        self.assertEqual({record.source_record_id for record in records}, {"1001", "1003"})

        first = next(record for record in records if record.source_record_id == "1001")
        self.assertEqual(
            first.description,
            clean_text(
                "I discovered identity theft when account number 123456 appeared on my report. "
                "I told the company that the account was not mine and requested its removal."
            ),
        )
        self.assertEqual(first.case_type, "Information Security")
        self.assertEqual(first.hazard_type, "Cyber—Identity Theft/Fraud")
        self.assertEqual(first.country, "United States of America")
        self.assertTrue(first.source_url.endswith("/1001"))
        self.assertIn("CC0", CFPB_SOURCE_LICENSE)
        self.assertIn('"Sub-issue":"Information belongs to someone else"', first.raw_case_type)

    def test_masked_and_numbered_template_variants_share_a_fingerprint(self) -> None:
        numbered = "The identity theft account 1234 was added to my report on 2025-01-02."
        masked = "The identity theft account XXXX was added to my report on XX-XX-XXXX."

        self.assertEqual(
            cfpb_template_fingerprint(numbered),
            cfpb_template_fingerprint(masked),
        )

    def test_sampling_is_deterministic_and_near_duplicate_hook_can_be_disabled(self) -> None:
        arguments = (
            str(FIXTURES / "cfpb_identity_theft_tiny.csv"),
            1,
            91,
            10,
        )
        first, _ = build_cfpb_identity_theft_candidates(*arguments)
        second, _ = build_cfpb_identity_theft_candidates(*arguments)
        without_near_dedupe, statistics = build_cfpb_identity_theft_candidates(
            *arguments,
            near_duplicate_key=None,
        )

        self.assertEqual([record.case_no for record in first], [record.case_no for record in second])
        self.assertEqual(len(without_near_dedupe), 1)
        self.assertEqual(statistics["eligible_unique"], 3)


class PoliceUkSecurityAdapterTests(unittest.TestCase):
    def test_bulk_zip_uses_only_street_files_and_restricted_categories(self) -> None:
        street_fixture = FIXTURES / "2025-01-example-street.csv"
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "police-bulk.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "2025-01/2025-01-example-street.csv",
                    street_fixture.read_text(encoding="utf-8"),
                )
                archive.writestr(
                    "2025-01/2025-01-example-outcomes.csv",
                    "Crime ID,Outcome type\nignored,Ignored outcome\n",
                )

            records, statistics = build_police_uk_physical_security_candidates(
                str(archive_path),
                capacity=10,
                seed=23,
                minimum_words=20,
            )

        self.assertEqual(statistics["rows_read"], 6)
        self.assertEqual(statistics["eligible_unique"], 2)
        self.assertEqual(statistics["not_allowed_category"], 2)
        self.assertEqual(statistics["missing_id"], 1)
        self.assertEqual(statistics["duplicate_source_id"], 1)
        self.assertEqual({record.source_record_id for record in records}, {"crime-a", "crime-c"})
        self.assertTrue(all(record.case_type == "Physical Security" for record in records))
        self.assertTrue(all(record.hazard_type == "Physical Security" for record in records))
        self.assertTrue(all(record.country == "United Kingdom" for record in records))
        self.assertEqual(len({record.text_hash for record in records}), 2)
        self.assertIn("Open Government Licence", POLICE_UK_SOURCE_LICENSE)

        robbery = next(record for record in records if record.source_record_id == "crime-c")
        self.assertIn("approximate location as On or near Park Lane", robbery.description)
        self.assertIn("source context field states: Public source note", robbery.description)
        self.assertIn('"Longitude":"-1.210000"', robbery.raw_hazard)

    def test_description_is_a_fixed_source_field_rendering(self) -> None:
        row = {
            "Crime ID": "crime-z",
            "Month": "2025-02",
            "Reported by": "Example Police",
            "Falls within": "Example Police",
            "Location": "On or near Example Road",
            "Crime type": "Vehicle crime",
            "Last outcome category": "Under investigation",
            "Context": "",
        }

        first = format_police_uk_description(row)
        second = format_police_uk_description(dict(reversed(list(row.items()))))

        self.assertEqual(first, second)
        self.assertGreaterEqual(word_count(first), 20)
        self.assertIn("crime-z", first)
        self.assertIn("Vehicle crime".lower(), first)

    def test_directory_input_is_deterministic(self) -> None:
        fixture = FIXTURES / "2025-01-example-street.csv"
        first, _ = build_police_uk_physical_security_candidates(
            str(FIXTURES),
            capacity=1,
            seed=31,
            minimum_words=20,
        )
        second, _ = build_police_uk_physical_security_candidates(
            str(FIXTURES),
            capacity=1,
            seed=31,
            minimum_words=20,
        )

        self.assertTrue(fixture.exists())
        self.assertEqual([record.case_no for record in first], [record.case_no for record in second])


if __name__ == "__main__":
    unittest.main()
