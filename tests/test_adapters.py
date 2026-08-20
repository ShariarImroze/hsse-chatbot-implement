import json
import tempfile
import unittest
from pathlib import Path

from incident_pipeline.adapters import build_vcdb_candidates


class AdapterTests(unittest.TestCase):
    def test_vcdb_filename_is_used_when_incident_id_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            validated = Path(temporary_directory) / "data" / "json" / "validated"
            validated.mkdir(parents=True)
            common = {
                "incident_id": "REUSED-SOURCE-ID",
                "summary": "The organization reported that an external actor accessed a system and exposed confidential customer records.",
                "security_incident": "Confirmed",
                "action": {"hacking": {"variety": ["Use of stolen creds"]}},
                "attribute": {"confidentiality": {"data_disclosure": "Yes"}},
                "impact": {"overall_rating": "Unknown"},
                "victim": {"country": ["US"]},
                "timeline": {"incident": {"year": 2025}},
            }
            first = dict(common)
            second = dict(common)
            second["summary"] = "A separate company discovered that attackers entered its network and downloaded internal business information."
            (validated / "FILE-ONE.json").write_text(json.dumps(first), encoding="utf-8")
            (validated / "FILE-TWO.json").write_text(json.dumps(second), encoding="utf-8")

            records, statistics = build_vcdb_candidates(temporary_directory, 10, 7, 20)

            self.assertEqual(statistics["eligible_unique"], 2)
            self.assertEqual({record.case_no for record in records}, {"VCDB:FILE-ONE", "VCDB:FILE-TWO"})


if __name__ == "__main__":
    unittest.main()
