"""Release-blocking quality checks for balanced incident corpora."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from .models import CASE_TYPES, PUBLIC_FIELDS, canonicalize_country
from .taxonomy import SEVERITY_ORDER
from .text import is_probably_english, text_digest, word_count


SEVERITIES = {"Low", "Medium", "Severe", "Unknown"}
HAZARD_TYPES = {
    "Fire/Explosion",
    "Loss of Containment/Release",
    "Mechanical/Material Failure",
    "Toxic/Chemical Exposure",
    "Electrical",
    "Radiation/Nuclear",
    "Fall from Height",
    "Same-Level Slip/Trip/Fall",
    "Machinery/Caught-In/Struck-By",
    "Ergonomic/Manual Handling",
    "Biological/Health",
    "Environmental Pollution",
    "Transport/Collision",
    "Human/Organisational Factors",
    "Physical Security",
    "Asset Damage/Loss",
    "Reputation Damage/Loss",
    "Operational Disruption/Loss",
    "Cyber—Malware",
    "Cyber—Hacking/Intrusion",
    "Cyber—Social Engineering",
    "Cyber—Misuse/Insider",
    "Cyber—Data Disclosure/Loss",
    "Cyber—Identity Theft/Fraud",
    "Cyber—Availability/Denial of Service",
    "Other/Unknown",
}

SOURCE_CASE_TYPES = {
    "OSHA_ITA_CASE_DETAIL": {
        "Occupational Safety",
        "Occupational Health",
    },
    "USCG_NRC": {"Process Safety"},
    "CALOES_SPILL": {"Environment"},
    "FAA_SDR": {"Asset and Reputation Damage/Loss"},
    "FDA_RES": {"Operational Loss"},
    "CFPB_CONSUMER_COMPLAINT": {"Information Security"},
    "POLICE_UK": {"Physical Security"},
}

NRC_PROCESS_SAFETY_TYPES = {
    "FIXED",
    "STORAGE TANK",
    "PIPELINE",
    "PLATFORM",
}

def _valid_event_date(
    source: str,
    value: str,
    source_record_id: str = "",
) -> bool:
    formats = {
        "OSHA_ITA_CASE_DETAIL": ("%Y-%m-%d",),
        "USCG_NRC": ("%m/%d/%Y %H:%M", "%m/%d/%Y"),
        "CALOES_SPILL": ("%Y-%m-%d",),
        "FAA_SDR": ("%m/%d/%Y",),
        "FDA_RES": ("%Y%m%d",),
        "POLICE_UK": ("%Y-%m",),
    }
    if source == "CFPB_CONSUMER_COMPLAINT":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        parsed_date = parsed.date()
    else:
        parsed_date = None
        for date_format in formats.get(source, ()):
            try:
                parsed_date = datetime.strptime(value, date_format).date()
            except ValueError:
                continue
            break
        if parsed_date is None:
            return False

    if not date(1970, 1, 1) <= parsed_date <= date.today():
        return False

    if source == "CALOES_SPILL":
        match = re.match(r"^(\d{2})-", source_record_id)
        if match and parsed_date.year > 2000 + int(match.group(1)):
            return False
    return True


def validate_dataset(
    master_json: Path,
    provenance_jsonl: Path,
    report_path: Path,
    expected_case_type_quotas: dict[str, int] | None = None,
    minimum_description_words: int = 20,
) -> dict[str, object]:
    """Validate schema, grain, provenance, language, quotas, and label rules."""

    with master_json.open(encoding="utf-8") as stream:
        records = json.load(stream)

    failures: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []

    def fail(check: str, evidence: object) -> None:
        failures.append(
            {"check": check, "evidence": evidence, "severity": "Critical"}
        )

    def warn(check: str, evidence: object, severity: str = "Medium") -> None:
        warnings.append({"check": check, "evidence": evidence, "severity": severity})

    expected_case_type_quotas = expected_case_type_quotas or {}
    if expected_case_type_quotas:
        if set(expected_case_type_quotas) != set(CASE_TYPES):
            fail(
                "configured_case_type_domain",
                {
                    "expected": list(CASE_TYPES),
                    "configured": list(expected_case_type_quotas),
                },
            )
        expected_rows = sum(expected_case_type_quotas.values())
        if len(records) != expected_rows:
            fail(
                "exact_row_count",
                {"expected": expected_rows, "observed": len(records)},
            )

    schema_errors = sum(list(record) != list(PUBLIC_FIELDS) for record in records)
    if schema_errors:
        fail("exact_public_schema", {"rows_with_wrong_schema": schema_errors})

    case_numbers = [str(record.get("Case No", "")) for record in records]
    duplicate_case_numbers = len(case_numbers) - len(set(case_numbers))
    if duplicate_case_numbers:
        fail("case_number_uniqueness", {"duplicate_rows": duplicate_case_numbers})
    malformed_case_numbers = sum(
        ":" not in case_number or not all(case_number.split(":", 1))
        for case_number in case_numbers
    )
    if malformed_case_numbers:
        fail(
            "independent_source_identifiers",
            {"malformed_case_numbers": malformed_case_numbers},
        )

    missing_by_field = {
        field: sum(not str(record.get(field, "")).strip() for record in records)
        for field in PUBLIC_FIELDS
    }
    missing_by_field = {
        field: count for field, count in missing_by_field.items() if count
    }
    if missing_by_field:
        fail("required_field_completeness", missing_by_field)

    noncanonical_countries = Counter(
        str(record.get("Country", ""))
        for record in records
        if canonicalize_country(str(record.get("Country", "")))
        != str(record.get("Country", ""))
    )
    if noncanonical_countries:
        fail("canonical_country_labels", dict(noncanonical_countries))

    case_type_counts = Counter(
        str(record.get("Case Type", "")) for record in records
    )
    invalid_case_types = {
        value: count
        for value, count in case_type_counts.items()
        if value not in CASE_TYPES
    }
    if invalid_case_types:
        fail("case_type_domain", invalid_case_types)
    if expected_case_type_quotas and case_type_counts != Counter(
        expected_case_type_quotas
    ):
        fail(
            "exact_case_type_quotas",
            {
                "expected": expected_case_type_quotas,
                "observed": dict(case_type_counts),
            },
        )
    if len(records) >= 400_000:
        out_of_bounds = {
            case_type: count
            for case_type, count in case_type_counts.items()
            if count < 50_000 or count > 100_000
        }
        if out_of_bounds:
            fail("case_type_minimum_and_cap", out_of_bounds)

    description_hashes = [
        text_digest(str(record.get("Description", ""))) for record in records
    ]
    duplicate_descriptions = len(description_hashes) - len(
        set(description_hashes)
    )
    if duplicate_descriptions:
        fail(
            "exact_description_duplicates",
            {"duplicate_rows": duplicate_descriptions},
        )

    short_descriptions = sum(
        word_count(str(record.get("Description", "")))
        < minimum_description_words
        for record in records
    )
    if short_descriptions:
        fail(
            "minimum_description_length",
            {
                "minimum_words": minimum_description_words,
                "rows_below_minimum": short_descriptions,
            },
        )
    non_english = sum(
        not is_probably_english(
            str(record.get("Description", "")), minimum_source_words=5
        )
        for record in records
    )
    if non_english:
        fail("english_descriptions", {"rows_rejected": non_english})

    invalid_hazards = Counter(
        record.get("Hazard Type")
        for record in records
        if record.get("Hazard Type") not in HAZARD_TYPES
    )
    invalid_actual = Counter(
        record.get("Actual Severity")
        for record in records
        if record.get("Actual Severity") not in SEVERITIES
    )
    invalid_potential = Counter(
        record.get("Potential Severity")
        for record in records
        if record.get("Potential Severity") not in SEVERITIES
    )
    for check, evidence in (
        ("hazard_type_domain", invalid_hazards),
        ("actual_severity_domain", invalid_actual),
        ("potential_severity_domain", invalid_potential),
    ):
        if evidence:
            fail(check, dict(evidence))

    potential_below_actual = Counter(
        f"{record.get('Actual Severity')} -> {record.get('Potential Severity')}"
        for record in records
        if SEVERITY_ORDER.get(str(record.get("Potential Severity")), -1)
        < SEVERITY_ORDER.get(str(record.get("Actual Severity")), -1)
    )
    if potential_below_actual:
        fail("potential_not_below_actual", dict(potential_below_actual))

    provenance_count = 0
    source_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    source_case_counts: Counter[str] = Counter()
    source_record_keys: set[tuple[str, str]] = set()
    duplicate_source_keys = 0
    provenance_projection_mismatches = 0
    provenance_hash_mismatches = 0
    missing_provenance: Counter[str] = Counter()
    noncanonical_provenance_countries: Counter[str] = Counter()
    invalid_event_dates: Counter[str] = Counter()

    provenance_required = (
        "source",
        "source_record_id",
        "source_url",
        "source_license",
        "event_date",
        "raw_case_type",
        "raw_hazard",
        "raw_severity",
        "title_provenance",
        "hazard_label_method",
        "actual_severity_label_method",
        "potential_severity_label_method",
        "text_hash",
        "sampling_rank",
        "dataset_split",
    )
    source_case_mismatches: Counter[str] = Counter()
    process_safety_source_type_mismatches: Counter[str] = Counter()
    with provenance_jsonl.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            if provenance_count < len(records):
                projection = {
                    field: record.get(field) for field in PUBLIC_FIELDS
                }
                if projection != records[provenance_count]:
                    provenance_projection_mismatches += 1
            provenance_count += 1

            for field in provenance_required:
                if record.get(field) is None or not str(record.get(field)).strip():
                    missing_provenance[field] += 1
            source = str(record.get("source", ""))
            source_record_id = str(record.get("source_record_id", ""))
            key = (source, source_record_id)
            if key in source_record_keys:
                duplicate_source_keys += 1
            source_record_keys.add(key)
            source_counts[source] += 1
            if not _valid_event_date(
                source,
                str(record.get("event_date", "")),
                source_record_id,
            ):
                invalid_event_dates[source] += 1
            case_type = str(record.get("Case Type", ""))
            source_case_counts[
                f"{source} | {case_type}"
            ] += 1
            allowed_case_types = SOURCE_CASE_TYPES.get(source)
            if allowed_case_types is None or case_type not in allowed_case_types:
                source_case_mismatches[f"{source} | {case_type}"] += 1
            if (
                case_type == "Process Safety"
                and str(record.get("raw_case_type", "")).upper()
                not in NRC_PROCESS_SAFETY_TYPES
            ):
                process_safety_source_type_mismatches[
                    str(record.get("raw_case_type", ""))
                ] += 1
            split_counts[str(record.get("dataset_split", ""))] += 1
            country = str(record.get("Country", ""))
            if canonicalize_country(country) != country:
                noncanonical_provenance_countries[country] += 1
            if str(record.get("text_hash", "")) != text_digest(
                str(record.get("Description", ""))
            ):
                provenance_hash_mismatches += 1

    if provenance_count != len(records):
        fail(
            "provenance_row_coverage",
            {"master": len(records), "provenance": provenance_count},
        )
    if provenance_projection_mismatches:
        fail(
            "public_provenance_projection",
            {"mismatched_rows": provenance_projection_mismatches},
        )
    if duplicate_source_keys:
        fail(
            "source_record_key_uniqueness",
            {"duplicate_rows": duplicate_source_keys},
        )
    if missing_provenance:
        fail("provenance_completeness", dict(missing_provenance))
    if source_case_mismatches:
        fail("source_case_type_mapping", dict(source_case_mismatches))
    if process_safety_source_type_mismatches:
        fail(
            "process_safety_source_type_mapping",
            dict(process_safety_source_type_mismatches),
        )
    if provenance_hash_mismatches:
        fail(
            "provenance_text_hash_integrity",
            {"mismatched_rows": provenance_hash_mismatches},
        )
    if noncanonical_provenance_countries:
        fail(
            "canonical_country_labels_in_provenance",
            dict(noncanonical_provenance_countries),
        )
    if invalid_event_dates:
        fail("source_event_date_format", dict(invalid_event_dates))
    invalid_splits = {
        split: count
        for split, count in split_counts.items()
        if split not in {"train", "validation", "test"}
    }
    if invalid_splits:
        fail("dataset_split_domain", invalid_splits)

    unknown_actual = sum(
        record.get("Actual Severity") == "Unknown" for record in records
    )
    if unknown_actual / max(len(records), 1) > 0.10:
        warn(
            "actual_severity_unknown_rate",
            {
                "rows": unknown_actual,
                "rate": round(unknown_actual / len(records), 4),
            },
            "High",
        )
    unknown_hazard = sum(
        record.get("Hazard Type") == "Other/Unknown" for record in records
    )
    if unknown_hazard / max(len(records), 1) > 0.10:
        warn(
            "hazard_type_unknown_rate",
            {
                "rows": unknown_hazard,
                "rate": round(unknown_hazard / len(records), 4),
            },
        )

    report: dict[str, object] = {
        "status": "PASS" if not failures else "FAIL",
        "row_count": len(records),
        "column_count": len(PUBLIC_FIELDS),
        "schema": list(PUBLIC_FIELDS),
        "source_distribution": dict(source_counts),
        "source_case_type_distribution": dict(source_case_counts),
        "country_distribution": dict(
            Counter(record["Country"] for record in records)
        ),
        "split_distribution": dict(split_counts),
        "case_type_distribution": dict(case_type_counts),
        "hazard_type_distribution": dict(
            Counter(record["Hazard Type"] for record in records)
        ),
        "actual_severity_distribution": dict(
            Counter(record["Actual Severity"] for record in records)
        ),
        "potential_severity_distribution": dict(
            Counter(record["Potential Severity"] for record in records)
        ),
        "exact_duplicate_case_numbers": duplicate_case_numbers,
        "exact_duplicate_descriptions": duplicate_descriptions,
        "minimum_description_words": min(
            (word_count(record["Description"]) for record in records),
            default=0,
        ),
        "non_english_descriptions": non_english,
        "failures": failures,
        "warnings": warnings,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Data-quality status: {report['status']}; report: {report_path}")
    if failures:
        raise ValueError(
            f"Dataset quality validation failed with {len(failures)} critical finding(s)"
        )
    return report
