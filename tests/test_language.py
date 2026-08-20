import unittest

from incident_pipeline.text import is_probably_english


class LanguageScreenTests(unittest.TestCase):
    def test_english_incident_report_passes(self) -> None:
        text = (
            "The employee was moving a loaded cart when one wheel failed and the "
            "cart struck their left hand, requiring treatment at a local clinic."
        )
        self.assertTrue(is_probably_english(text))

    def test_spanish_incident_report_fails(self) -> None:
        text = (
            "El trabajador sufrio una lesion en la mano cuando una maquina cayo "
            "durante el turno y necesito atencion medica inmediata en el hospital."
        )
        self.assertFalse(is_probably_english(text))

    def test_french_incident_report_fails(self) -> None:
        text = (
            "Le travailleur a subi une blessure a la main lorsque la machine est "
            "tombee pendant le quart et a necessite des soins medicaux immediats."
        )
        self.assertFalse(is_probably_english(text))


if __name__ == "__main__":
    unittest.main()
