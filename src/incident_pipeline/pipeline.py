"""Balanced corpus construction, high-precision deduplication, and export."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Optional

from .models import CASE_TYPES, IncidentRecord
from .text import normalized_for_hash, stable_split


CandidateBuilder = Callable[
    [str, int, int, int, Optional[int]],
    tuple[list[IncidentRecord], dict[str, int]],
]


@dataclass(frozen=True)
class CandidateJob:
    """One authentic source pool assigned to one canonical case type."""

    key: str
    case_type: str
    source_path: Path
    builder: CandidateBuilder
    apply_near_duplicate_filter: bool = True


class NearDuplicateIndex:
    """High-precision near-duplicate filter based on shared text boundaries."""

    def __init__(self, similarity_threshold: float = 0.96) -> None:
        self.similarity_threshold = similarity_threshold
        self._texts: list[str] = []
        self._prefix: dict[str, list[int]] = {}
        self._suffix: dict[str, list[int]] = {}

    @staticmethod
    def _keys(text: str) -> tuple[str, str]:
        tokens = text.split()
        length_bucket = len(tokens) // 10
        return (
            f"{length_bucket}|{' '.join(tokens[:8])}",
            f"{length_bucket}|{' '.join(tokens[-8:])}",
        )

    def is_duplicate(self, text: str) -> bool:
        normalized = normalized_for_hash(text)
        prefix, suffix = self._keys(normalized)
        candidate_ids = set(self._prefix.get(prefix, [])[-20:]) | set(
            self._suffix.get(suffix, [])[-20:]
        )
        for candidate_id in candidate_ids:
            other = self._texts[candidate_id]
            length_ratio = min(len(normalized), len(other)) / max(
                len(normalized), len(other)
            )
            if length_ratio < 0.85:
                continue
            matcher = SequenceMatcher(None, normalized, other, autojunk=False)
            if matcher.quick_ratio() < self.similarity_threshold:
                continue
            if matcher.ratio() >= self.similarity_threshold:
                return True
        return False

    def add(self, text: str) -> None:
        normalized = normalized_for_hash(text)
        prefix, suffix = self._keys(normalized)
        record_id = len(self._texts)
        self._texts.append(normalized)
        self._prefix.setdefault(prefix, []).append(record_id)
        self._suffix.setdefault(suffix, []).append(record_id)


def _write_json_array(path: Path, records: Iterable[IncidentRecord]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        stream.write("[\n")
        first = True
        for record in records:
            if not first:
                stream.write(",\n")
            json.dump(record.public_dict(), stream, ensure_ascii=False)
            first = False
        stream.write("\n]\n")


def _write_jsonl(path: Path, records: Iterable[IncidentRecord]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            json.dump(record.provenance_dict(), stream, ensure_ascii=False)
            stream.write("\n")


def build_balanced_dataset(
    jobs: dict[str, CandidateJob],
    output_directory: Path,
    dataset_name: str,
    case_type_quotas: dict[str, int],
    seed: int,
    minimum_words: int,
    similarity_threshold: float,
    maximum_rows_per_source: dict[str, int] | None = None,
    split_ratios: dict[str, float] | None = None,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Build a deterministic, mutually exclusive case-type-balanced corpus."""

    expected_case_types = set(CASE_TYPES)
    configured_case_types = set(case_type_quotas)
    if configured_case_types != expected_case_types:
        raise ValueError(
            "Case-type quotas must match the canonical taxonomy exactly; "
            f"missing={sorted(expected_case_types - configured_case_types)}, "
            f"extra={sorted(configured_case_types - expected_case_types)}"
        )
    if set(jobs) != expected_case_types:
        raise ValueError("Exactly one candidate job is required for every case type")
    if any(job.case_type != case_type for case_type, job in jobs.items()):
        raise ValueError("Candidate job keys and declared case types must agree")
    if any(quota <= 0 or quota > 100_000 for quota in case_type_quotas.values()):
        raise ValueError("Every case-type quota must be between 1 and 100,000")

    target_size = sum(case_type_quotas.values())
    output_directory.mkdir(parents=True, exist_ok=True)
    maximum_rows_per_source = maximum_rows_per_source or {}
    split_ratios = split_ratios or {
        "train": 0.8,
        "validation": 0.1,
        "test": 0.1,
    }
    if set(split_ratios) != {"train", "validation", "test"}:
        raise ValueError("splits must define train, validation, and test")
    if any(value < 0 for value in split_ratios.values()) or abs(
        sum(split_ratios.values()) - 1.0
    ) > 1e-9:
        raise ValueError("split ratios must be non-negative and sum to 1")

    selected: list[IncidentRecord] = []
    exact_hashes: set[str] = set()
    near_duplicate_index = NearDuplicateIndex(similarity_threshold)
    source_stats: dict[str, dict[str, int]] = {}
    rejected_exact: Counter[str] = Counter()
    rejected_near: Counter[str] = Counter()

    for case_type in CASE_TYPES:
        job = jobs[case_type]
        quota = case_type_quotas[case_type]
        buffer_size = max(10_000, quota // 2)
        candidates, stats = job.builder(
            str(job.source_path),
            quota + buffer_size,
            seed,
            minimum_words,
            maximum_rows_per_source.get(job.key),
        )
        source_stats[job.key] = stats
        mismatched = Counter(
            record.case_type
            for record in candidates
            if record.case_type != case_type
        )
        if mismatched:
            raise RuntimeError(
                f"{job.key} returned records outside {case_type}: {dict(mismatched)}"
            )

        case_selected = 0
        for record in candidates:
            if case_selected >= quota:
                break
            if record.text_hash in exact_hashes:
                rejected_exact[job.key] += 1
                continue
            if (
                job.apply_near_duplicate_filter
                and near_duplicate_index.is_duplicate(record.description)
            ):
                rejected_near[job.key] += 1
                continue
            exact_hashes.add(record.text_hash)
            near_duplicate_index.add(record.description)
            selected.append(record)
            case_selected += 1

        print(
            f"{job.key}: read {stats.get('rows_read', 0):,}, "
            f"eligible {stats.get('eligible_unique', 0):,}, "
            f"selected {case_selected:,}/{quota:,}"
        )
        if case_selected != quota:
            raise RuntimeError(
                f"{job.key} supplied only {case_selected:,} unique {case_type} records "
                f"after duplicate filtering; required {quota:,}."
            )

    if len(selected) != target_size:
        raise RuntimeError(f"Selected {len(selected):,} records; expected {target_size:,}")

    selected.sort(key=lambda record: (record.sampling_rank, record.case_no))
    for record in selected:
        record.dataset_split = stable_split(
            record.case_no,
            train=split_ratios["train"],
            validation=split_ratios["validation"],
        )

    public_path = output_directory / f"{dataset_name}.json"
    provenance_path = output_directory / f"{dataset_name}_with_provenance.jsonl"
    _write_json_array(public_path, selected)
    _write_jsonl(provenance_path, selected)

    report: dict[str, object] = {
        "dataset_name": dataset_name,
        "dataset_rows": len(selected),
        "public_schema": list(selected[0].public_dict()),
        "case_type_quotas": case_type_quotas,
        "case_type_sources": {
            case_type: jobs[case_type].key for case_type in CASE_TYPES
        },
        "near_duplicate_filter_by_source": {
            jobs[case_type].key: jobs[case_type].apply_near_duplicate_filter
            for case_type in CASE_TYPES
        },
        "selected_by_source": dict(Counter(record.source for record in selected)),
        "case_type_distribution": dict(
            Counter(record.case_type for record in selected)
        ),
        "hazard_type_distribution": dict(
            Counter(record.hazard_type for record in selected)
        ),
        "actual_severity_distribution": dict(
            Counter(record.actual_severity for record in selected)
        ),
        "potential_severity_distribution": dict(
            Counter(record.potential_severity for record in selected)
        ),
        "split_distribution": dict(
            Counter(record.dataset_split for record in selected)
        ),
        "adapter_statistics": source_stats,
        "cross_source_exact_duplicates_rejected": dict(rejected_exact),
        "near_duplicates_rejected": dict(rejected_near),
        "random_seed": seed,
        "minimum_description_words": minimum_words,
        "near_duplicate_similarity": similarity_threshold,
        "split_ratios": split_ratios,
        "source_records_only": True,
        "ai_generated_reports": 0,
        "public_json": str(public_path),
        "provenance_jsonl": str(provenance_path),
    }
    report_path = report_path or (
        output_directory.parent.parent / "reports" / "build_report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Wrote {len(selected):,} incidents to {public_path}")
    print(f"Wrote auditable provenance to {provenance_path}")
    return report
