"""Create a deterministic, case-type-balanced human review sheet."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from incident_pipeline.models import CASE_TYPES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = (
    PROJECT_ROOT / "data" / "processed" / "pilot_1000_with_provenance.jsonl"
)
OUTPUT_PATH = PROJECT_ROOT / "reports" / "pilot_label_review.csv"

REVIEW_FIELDS = (
    "review_case_type_correct",
    "review_hazard_correct",
    "review_severity_correct",
    "review_notes",
)


def main(rows_per_case_type: int = 10) -> None:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    with INPUT_PATH.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                record = json.loads(line)
                grouped[str(record["Case Type"])].append(record)

    selected: list[dict[str, object]] = []
    for case_type in CASE_TYPES:
        records = sorted(
            grouped[case_type],
            key=lambda record: (
                int(record["sampling_rank"]),
                str(record["Case No"]),
            ),
        )[:rows_per_case_type]
        if len(records) != rows_per_case_type:
            raise RuntimeError(
                f"{case_type} supplied {len(records)} review rows; "
                f"expected {rows_per_case_type}"
            )
        selected.extend(records)

    fields = list(selected[0]) + list(REVIEW_FIELDS)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in selected:
            writer.writerow({**record, **{field: "" for field in REVIEW_FIELDS}})
    print(
        f"Wrote {len(selected)} review rows "
        f"({rows_per_case_type} per case type) to {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
