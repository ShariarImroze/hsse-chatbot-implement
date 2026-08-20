import unittest

from incident_pipeline.text import is_explicit_non_release


class ReleaseEvidenceTests(unittest.TestCase):
    def test_absent_and_potential_releases_are_rejected(self) -> None:
        self.assertTrue(
            is_explicit_non_release(
                "The tank was vulnerable, but currently no oil release was noted."
            )
        )
        self.assertTrue(
            is_explicit_non_release(
                "Potential release due to an alarm; crews will investigate."
            )
        )

    def test_positive_release_survives_unaffected_waterway_statement(self) -> None:
        self.assertFalse(
            is_explicit_non_release(
                "A vehicle released 25 gallons of oil onto soil. "
                "There was no release to waterways."
            )
        )


if __name__ == "__main__":
    unittest.main()
