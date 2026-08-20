"""Transparent rules for harmonizing hazards and severity labels.

These functions create weak labels for corpus construction. They are deliberately
deterministic and auditable; they must not be presented as expert-validated ground
truth in model evaluation.
"""

from __future__ import annotations

from collections.abc import Iterable

from .text import clean_text


SEVERITY_ORDER = {"Unknown": -1, "Low": 0, "Medium": 1, "Severe": 2}
PROCESS_SAFETY_HAZARD_TYPES = frozenset(
    {
        "Fire/Explosion",
        "Loss of Containment/Release",
        "Mechanical/Material Failure",
        "Toxic/Chemical Exposure",
    }
)


def maximum_severity(*values: str) -> str:
    known = [value for value in values if value in SEVERITY_ORDER]
    return max(known, key=lambda value: SEVERITY_ORDER[value], default="Unknown")


def occupational_case_type(
    type_code: str,
    type_label: str = "",
    hazard_type: str = "",
) -> str:
    """Map OSHA/MSHA injury and illness categories to the case taxonomy."""
    if hazard_type in PROCESS_SAFETY_HAZARD_TYPES:
        return "Process Safety"
    normalized = f"{type_code} {type_label}".lower()
    if type_code in {"2", "3", "4", "5", "6"}:
        return "Occupational Health"
    health_terms = (
        "illness", "disease", "hearing", "poison", "respiratory", "skin disorder",
        "dust disease", "occupational disease",
    )
    if any(term in normalized for term in health_terms):
        return "Occupational Health"
    return "Occupational Safety"


def classify_hazard(*source_values: object) -> str:
    """Map source event, classification, and narrative evidence to a hazard type."""
    text = " ".join(clean_text(value).lower() for value in source_values if value)

    # OSHA's combined source heading "effects of radiation and noise" must
    # not turn explicit hearing-loss/noise cases into Radiation/Nuclear events.
    hearing_terms = (
        "hearing loss",
        "hearing impairment",
        "noise exposure",
        "occupational noise",
        "excessive noise",
    )
    if any(term in text for term in hearing_terms) or (
        "hearing" in text and "noise" in text
    ):
        return "Biological/Health"

    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Fire/Explosion", ("explosion", "explosive", "fire", "flame", "combust")),
        ("Loss of Containment/Release", ("loss of containment", "release", "leak", "spill", "rupture", "overflow")),
        ("Radiation/Nuclear", ("radiation", "radioactive", "nuclear")),
        ("Electrical", ("electroc", "electric shock", "arc flash", "electrical current", "contact with electric")),
        ("Toxic/Chemical Exposure", ("exposure to harmful substances", "toxic", "chemical", "caustic", "corrosive", "poison", "gas inhal", "fume", "solvent")),
        ("Fall from Height", ("fall to lower", "from height", "from elevation", "from ladder", "from scaffold", "from roof")),
        ("Same-Level Slip/Trip/Fall", ("same level", "slip", "trip", "stumble")),
        ("Transport/Collision", ("transportation", "vehicle", "collision", "crash", "roadway", "haul truck", "railway", "aircraft")),
        ("Physical Security", ("violence", "assault", "homicide", "robbery", "shooting", "intentional injury")),
        ("Ergonomic/Manual Handling", ("overexert", "repetitive", "lifting", "manual handling", "bodily reaction", "bodily position and motion", "strain while")),
        ("Biological/Health", ("virus", "bacteria", "biological", "infectious", "respiratory condition", "hearing loss", "occupational illness")),
        ("Machinery/Caught-In/Struck-By", ("caught", "crush", "struck", "contact with non-running", "contact with running", "contact with object", "contact with equipment", "machinery", "machine", "pinch")),
        ("Mechanical/Material Failure", ("fall of roof", "roof fall", "mechanical failure", "structural failure", "material failure", "collapse", "broken", "equipment failure")),
        ("Environmental Pollution", ("pollution", "environmental damage", "contamination", "discharge")),
        ("Human/Organisational Factors", ("human factor", "procedure", "training", "fatigue", "communication", "supervision")),
    )
    for label, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return label
    return "Other/Unknown"


def osha_actual_severity(incident_outcome: str) -> str:
    """Use the OSHA data-dictionary outcome order, not narrative inference."""
    return {
        "1": "Severe",  # Death
        "2": "Medium",  # Days away from work
        "3": "Medium",  # Job transfer or restriction
        "4": "Low",  # Other recordable case
    }.get(clean_text(incident_outcome), "Unknown")


def msha_actual_severity(degree_injury: str, days_lost: str, days_restricted: str) -> str:
    normalized = clean_text(degree_injury).lower()
    if any(term in normalized for term in ("fatal", "permanent total", "permanent partial")):
        return "Severe"
    if any(term in normalized for term in ("no days", "no dys", "first aid", "no injury", "all other")):
        return "Low"
    if any(term in normalized for term in ("days away", "dys away", "restricted", "rstr", "lost time")):
        return "Medium"
    try:
        if int(float(clean_text(days_lost) or 0)) > 0 or int(float(clean_text(days_restricted) or 0)) > 0:
            return "Medium"
    except ValueError:
        pass
    return "Unknown"


def occupational_potential_severity(actual: str, hazard_type: str) -> str:
    """Estimate credible potential consequence using a documented hazard matrix."""
    severe_potential = {
        "Fire/Explosion",
        "Loss of Containment/Release",
        "Toxic/Chemical Exposure",
        "Electrical",
        "Radiation/Nuclear",
        "Fall from Height",
        "Machinery/Caught-In/Struck-By",
        "Transport/Collision",
        "Physical Security",
    }
    medium_potential = {
        "Mechanical/Material Failure",
        "Same-Level Slip/Trip/Fall",
        "Ergonomic/Manual Handling",
        "Biological/Health",
        "Environmental Pollution",
        "Human/Organisational Factors",
    }
    rule_value = "Severe" if hazard_type in severe_potential else "Medium" if hazard_type in medium_potential else "Unknown"
    return maximum_severity(actual, rule_value)


def vcdb_action_details(action: dict[str, object]) -> tuple[list[str], list[str]]:
    categories: list[str] = []
    varieties: list[str] = []
    for category, detail in action.items():
        categories.append(clean_text(category).title())
        if isinstance(detail, dict):
            raw_varieties = detail.get("variety", [])
            if isinstance(raw_varieties, list):
                varieties.extend(clean_text(value) for value in raw_varieties if clean_text(value))
    return sorted(set(categories)), sorted(set(varieties))


def vcdb_hazard_type(action_categories: Iterable[str], action_varieties: Iterable[str], availability: Iterable[str]) -> str:
    categories = {clean_text(value).lower() for value in action_categories}
    detail = " ".join(clean_text(value).lower() for value in (*action_varieties, *availability))
    if "denial of service" in detail or " dos" in f" {detail}" or "interruption" in detail:
        return "Cyber—Availability/Denial of Service"
    if "malware" in categories:
        return "Cyber—Malware"
    if "hacking" in categories:
        return "Cyber—Hacking/Intrusion"
    if "social" in categories:
        return "Cyber—Social Engineering"
    if "misuse" in categories:
        return "Cyber—Misuse/Insider"
    if "error" in categories:
        return "Cyber—Data Disclosure/Loss"
    if "physical" in categories:
        return "Physical Security"
    if availability:
        return "Cyber—Availability/Denial of Service"
    return "Other/Unknown"


def vcdb_actual_severity(overall_rating: str) -> str:
    normalized = clean_text(overall_rating).lower()
    if normalized in {"catastrophic", "damaging", "painful"}:
        return "Severe"
    if normalized == "distracting":
        return "Medium"
    if normalized == "insignificant":
        return "Low"
    return "Unknown"


def vcdb_potential_severity(
    actual: str,
    hazard_type: str,
    action_varieties: Iterable[str],
    availability: Iterable[str],
    data_total: int | None,
    data_disclosure: str,
) -> str:
    detail = " ".join(clean_text(value).lower() for value in (*action_varieties, *availability))
    severe_signal = (
        (data_total is not None and data_total >= 100_000)
        or "ransomware" in detail
        or "destruction" in detail
        or "loss" in detail
    )
    if severe_signal:
        rule_value = "Severe"
    elif hazard_type.startswith("Cyber—") or hazard_type == "Physical Security" or clean_text(data_disclosure) in {"Yes", "Potentially"}:
        rule_value = "Medium"
    else:
        rule_value = "Unknown"
    return maximum_severity(actual, rule_value)
