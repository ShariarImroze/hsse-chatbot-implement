import unittest

from incident_pipeline.cli import _default_database, _default_quality_report


class DatasetSpecificCliDefaultsTests(unittest.TestCase):
    def test_master_defaults_are_canonical(self) -> None:
        config = {"dataset_name": "master_400K"}
        self.assertEqual(_default_quality_report(config).name, "data_quality_report.json")
        self.assertEqual(_default_database(config).name, "hsse_incidents.sqlite")

    def test_pilot_defaults_cannot_overwrite_master_artifacts(self) -> None:
        config = {"dataset_name": "pilot_1000"}
        self.assertEqual(
            _default_quality_report(config).name,
            "pilot_data_quality_report.json",
        )
        self.assertEqual(_default_database(config).name, "pilot_1000.sqlite")


if __name__ == "__main__":
    unittest.main()
