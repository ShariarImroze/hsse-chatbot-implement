"""Canonical incident and provenance models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


PUBLIC_FIELDS = (
    "Case No",
    "Country",
    "Title",
    "Description",
    "Case Type",
    "Hazard",
    "Hazard Type",
    "Actual Severity",
    "Potential Severity",
)

CASE_TYPES = (
    "Occupational Safety",
    "Occupational Health",
    "Process Safety",
    "Environment",
    "Asset and Reputation Damage/Loss",
    "Operational Loss",
    "Information Security",
    "Physical Security",
)

UNITED_STATES_COUNTRY = "United States of America"
_UNITED_STATES_ALIASES = frozenset(
    {
        "united states",
        "unites states",
        "united states of america",
        "united states of americal",
    }
)


def canonicalize_country(value: str) -> str:
    """Return the exact canonical United States label without changing other countries."""
    labels = [label.strip() for label in value.split(";")]
    canonical_labels = [
        UNITED_STATES_COUNTRY if label.casefold() in _UNITED_STATES_ALIASES else label
        for label in labels
    ]
    canonical = "; ".join(canonical_labels)
    return canonical if canonical != value else value


@dataclass
class IncidentRecord:
    case_no: str
    country: str
    title: str
    description: str
    case_type: str
    hazard: str
    hazard_type: str
    actual_severity: str
    potential_severity: str
    source: str
    source_record_id: str
    source_url: str
    source_license: str
    event_date: str
    raw_case_type: str
    raw_hazard: str
    raw_severity: str
    title_provenance: str
    hazard_label_method: str
    actual_severity_label_method: str
    potential_severity_label_method: str
    text_hash: str
    sampling_rank: int
    dataset_split: str = ""

    def public_dict(self) -> dict[str, str]:
        """Return the exact nine-column schema requested for the master JSON."""
        return {
            "Case No": self.case_no,
            "Country": self.country,
            "Title": self.title,
            "Description": self.description,
            "Case Type": self.case_type,
            "Hazard": self.hazard,
            "Hazard Type": self.hazard_type,
            "Actual Severity": self.actual_severity,
            "Potential Severity": self.potential_severity,
        }

    def provenance_dict(self) -> dict[str, Any]:
        """Return model fields plus traceability metadata for audit and database use."""
        record = self.public_dict()
        metadata = asdict(self)
        for internal_name in (
            "case_no",
            "country",
            "title",
            "description",
            "case_type",
            "hazard",
            "hazard_type",
            "actual_severity",
            "potential_severity",
        ):
            metadata.pop(internal_name)
        record.update(metadata)
        return record
