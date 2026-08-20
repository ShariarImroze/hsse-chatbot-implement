"""Canonical source-to-case-type assignments for the balanced corpus."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .asset_operational_adapters import (
    build_faa_sdr_asset_reputation_candidates,
    build_fda_operational_loss_candidates,
)
from .caloes_adapter import build_caloes_environment_candidates
from .models import CASE_TYPES
from .osha_oiics_adapter import (
    build_osha_health_candidates,
    build_osha_safety_candidates,
)
from .pipeline import CandidateBuilder, CandidateJob
from .security_adapters import (
    build_cfpb_identity_theft_candidates,
    build_police_uk_physical_security_candidates,
)
from .uscg_nrc_adapter import build_nrc_process_safety_candidates


@dataclass(frozen=True)
class SourceDefinition:
    case_type: str
    relative_path: str
    builder: CandidateBuilder
    apply_near_duplicate_filter: bool = True


SOURCE_DEFINITIONS = {
    "osha_ita_safety": SourceDefinition(
        "Occupational Safety",
        "osha_ita",
        build_osha_safety_candidates,
    ),
    "osha_ita_health": SourceDefinition(
        "Occupational Health",
        "osha_ita",
        build_osha_health_candidates,
    ),
    "uscg_nrc_process": SourceDefinition(
        "Process Safety",
        "uscg_nrc",
        build_nrc_process_safety_candidates,
    ),
    "caloes_environment": SourceDefinition(
        "Environment",
        "caloes_spills",
        build_caloes_environment_candidates,
    ),
    "faa_sdr_asset_reputation": SourceDefinition(
        "Asset and Reputation Damage/Loss",
        "faa_sdr",
        build_faa_sdr_asset_reputation_candidates,
        False,
    ),
    "fda_operational": SourceDefinition(
        "Operational Loss",
        "fda_enforcement",
        build_fda_operational_loss_candidates,
        False,
    ),
    "cfpb_information_security": SourceDefinition(
        "Information Security",
        "cfpb",
        build_cfpb_identity_theft_candidates,
        False,
    ),
    "police_uk_physical_security": SourceDefinition(
        "Physical Security",
        "police_uk",
        build_police_uk_physical_security_candidates,
        False,
    ),
}


def candidate_jobs(
    raw_directory: Path,
    case_type_sources: dict[str, str],
) -> dict[str, CandidateJob]:
    """Resolve configured case types to auditable adapter jobs."""

    if set(case_type_sources) != set(CASE_TYPES):
        raise ValueError("Case-type source assignments must cover all case types")
    jobs: dict[str, CandidateJob] = {}
    for case_type in CASE_TYPES:
        source_key = case_type_sources[case_type]
        try:
            definition = SOURCE_DEFINITIONS[source_key]
        except KeyError as error:
            raise ValueError(f"Unknown source assignment: {source_key}") from error
        if definition.case_type != case_type:
            raise ValueError(
                f"{source_key} is assigned to {definition.case_type}, not {case_type}"
            )
        jobs[case_type] = CandidateJob(
            key=source_key,
            case_type=case_type,
            source_path=raw_directory / definition.relative_path,
            builder=definition.builder,
            apply_near_duplicate_filter=definition.apply_near_duplicate_filter,
        )
    return jobs


__all__ = ["SOURCE_DEFINITIONS", "candidate_jobs"]
