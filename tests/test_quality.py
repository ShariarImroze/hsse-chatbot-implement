import unittest

from incident_pipeline.quality import _valid_event_date


class EventDateValidationTests(unittest.TestCase):
    def test_source_specific_date_formats(self) -> None:
        valid = {
            "OSHA_ITA_CASE_DETAIL": "2024-06-05",
            "USCG_NRC": "2/4/2010 21:00",
            "CALOES_SPILL": "2019-03-31",
            "FAA_SDR": "03/23/2024",
            "FDA_RES": "20160614",
            "CFPB_CONSUMER_COMPLAINT": "2023-05-24T07:20:45.000Z",
            "POLICE_UK": "2025-01",
        }
        for source, value in valid.items():
            with self.subTest(source=source):
                self.assertTrue(_valid_event_date(source, value))

    def test_malformed_and_implausible_dates_fail(self) -> None:
        self.assertFalse(_valid_event_date("CALOES_SPILL", "08/25/0208"))
        self.assertFalse(_valid_event_date("OSHA_ITA_CASE_DETAIL", "05JUN2024"))
        self.assertFalse(_valid_event_date("UNKNOWN", "2024-01-01"))
        self.assertFalse(
            _valid_event_date("CALOES_SPILL", "2029-05-01", "23-2942")
        )


if __name__ == "__main__":
    unittest.main()
