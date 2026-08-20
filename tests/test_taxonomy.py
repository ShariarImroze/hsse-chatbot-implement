import unittest

from incident_pipeline.taxonomy import (
    PROCESS_SAFETY_HAZARD_TYPES,
    classify_hazard,
    msha_actual_severity,
    occupational_case_type,
    occupational_potential_severity,
    osha_actual_severity,
    vcdb_actual_severity,
    vcdb_hazard_type,
)


class TaxonomyTests(unittest.TestCase):
    def test_osha_outcome_mapping_uses_official_order(self) -> None:
        self.assertEqual(osha_actual_severity("1"), "Severe")
        self.assertEqual(osha_actual_severity("2"), "Medium")
        self.assertEqual(osha_actual_severity("3"), "Medium")
        self.assertEqual(osha_actual_severity("4"), "Low")

    def test_hazard_rules_prioritize_high_consequence_events(self) -> None:
        self.assertEqual(classify_hazard("Explosion involving pressure vessel"), "Fire/Explosion")
        self.assertEqual(classify_hazard("Worker fell from ladder"), "Fall from Height")
        self.assertEqual(classify_hazard("Slip, trip, stumble or fall on same level"), "Same-Level Slip/Trip/Fall")
        self.assertEqual(classify_hazard("Exposure to caustic chemical"), "Toxic/Chemical Exposure")
        self.assertEqual(classify_hazard("Contact with non-running objects or equipment"), "Machinery/Caught-In/Struck-By")
        self.assertEqual(classify_hazard("Bodily position and motion"), "Ergonomic/Manual Handling")
        self.assertEqual(classify_hazard("Fall of roof or back"), "Mechanical/Material Failure")

    def test_potential_severity_is_never_below_actual(self) -> None:
        self.assertEqual(occupational_potential_severity("Severe", "Other/Unknown"), "Severe")
        self.assertEqual(occupational_potential_severity("Low", "Fire/Explosion"), "Severe")

    def test_process_safety_hazards_override_occupational_case_type(self) -> None:
        for hazard_type in PROCESS_SAFETY_HAZARD_TYPES:
            with self.subTest(hazard_type=hazard_type):
                self.assertEqual(
                    occupational_case_type("3", "Respiratory condition", hazard_type),
                    "Process Safety",
                )

        self.assertEqual(
            occupational_case_type("3", "Respiratory condition", "Biological/Health"),
            "Occupational Health",
        )
        self.assertEqual(
            occupational_case_type("1", "Injury", "Machinery/Caught-In/Struck-By"),
            "Occupational Safety",
        )

    def test_hearing_loss_takes_precedence_over_combined_radiation_heading(self) -> None:
        self.assertEqual(
            classify_hazard(
                "Exposure to harmful substances",
                "Effects of radiation and noise",
                "Employee developed hearing loss after excessive noise exposure",
            ),
            "Biological/Health",
        )
        self.assertEqual(
            classify_hazard(
                "Exposure to radiation and noise",
                "Nervous system and sense organs diseases",
                "Recordable threshold shift in employee hearing after an audiogram",
            ),
            "Biological/Health",
        )

    def test_msha_fatality_and_lost_time_mapping(self) -> None:
        self.assertEqual(msha_actual_severity("FATALITY", "0", "0"), "Severe")
        self.assertEqual(msha_actual_severity("NO DYS AWY FRM WRK,NO RSTR ACT", "0", "0"), "Low")
        self.assertEqual(msha_actual_severity("DAYS AWAY FROM WORK ONLY", "4", "0"), "Medium")

    def test_vcdb_hazard_and_impact_mapping(self) -> None:
        self.assertEqual(vcdb_hazard_type(["Malware"], ["Ransomware"], []), "Cyber—Malware")
        self.assertEqual(
            vcdb_hazard_type(["Hacking"], ["DoS"], ["Interruption"]),
            "Cyber—Availability/Denial of Service",
        )
        self.assertEqual(vcdb_actual_severity("Catastrophic"), "Severe")
        self.assertEqual(vcdb_actual_severity("Insignificant"), "Low")
        self.assertEqual(vcdb_actual_severity("Unknown"), "Unknown")


if __name__ == "__main__":
    unittest.main()
