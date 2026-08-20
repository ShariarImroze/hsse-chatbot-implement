import unittest

from incident_pipeline.models import (
    PUBLIC_FIELDS,
    UNITED_STATES_COUNTRY,
    IncidentRecord,
    canonicalize_country,
)


class ModelTests(unittest.TestCase):
    def test_public_schema_has_exact_requested_order(self) -> None:
        record = IncidentRecord(
            case_no="TEST:1",
            country=UNITED_STATES_COUNTRY,
            title="Test incident",
            description="A sufficiently descriptive test incident narrative for checking the public schema without using external data.",
            case_type="Occupational Safety",
            hazard="Test hazard",
            hazard_type="Other/Unknown",
            actual_severity="Unknown",
            potential_severity="Unknown",
            source="TEST",
            source_record_id="1",
            source_url="https://example.org",
            source_license="Test only",
            event_date="",
            raw_case_type="",
            raw_hazard="",
            raw_severity="",
            title_provenance="test",
            hazard_label_method="test",
            actual_severity_label_method="test",
            potential_severity_label_method="test",
            text_hash="abc",
            sampling_rank=1,
        )
        self.assertEqual(tuple(record.public_dict()), PUBLIC_FIELDS)

    def test_country_canonicalization_is_exact_and_token_aware(self) -> None:
        aliases = (
            "United States",
            "Unites States",
            "united states of america",
            "United States of Americal",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertEqual(canonicalize_country(alias), UNITED_STATES_COUNTRY)

        self.assertEqual(
            canonicalize_country("Norway; United States"),
            f"Norway; {UNITED_STATES_COUNTRY}",
        )
        self.assertEqual(
            canonicalize_country("United States Virgin Islands"),
            "United States Virgin Islands",
        )


if __name__ == "__main__":
    unittest.main()
