#!/usr/bin/env python3
"""Export the HSSE corpus as deterministic chat-format classification data."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


SYSTEM_PROMPT = (
    "Classify the supplied HSSE incident. Return only a JSON object with the "
    "keys case_type, hazard, hazard_type, actual_severity, and "
    "potential_severity. Do not invent facts that are absent from the report."
)
SPLITS = ("train", "validation", "test")


def training_record(row: sqlite3.Row) -> dict[str, object]:
    """Convert one normalized incident into one supervised chat example."""

    user_payload = {
        "title": row["title"],
        "description": row["description"],
        "country": row["country"],
    }
    assistant_payload = {
        "case_type": row["case_type"],
        "hazard": row["hazard"],
        "hazard_type": row["hazard_type"],
        "actual_severity": row["actual_severity"],
        "potential_severity": row["potential_severity"],
    }
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
            {
                "role": "assistant",
                "content": json.dumps(assistant_payload, ensure_ascii=False),
            },
        ],
        "metadata": {
            "case_no": row["case_no"],
            "source": row["source"],
            "label_methods": {
                "hazard": row["hazard_label_method"],
                "actual_severity": row["actual_severity_label_method"],
                "potential_severity": row["potential_severity_label_method"],
            },
        },
    }


def export(database: Path, output_directory: Path, limit: int | None) -> dict[str, int]:
    """Write one JSONL file per existing corpus split."""

    if not database.is_file():
        raise FileNotFoundError(f"dataset not found: {database}")
    output_directory.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        for split in SPLITS:
            query = """
                SELECT case_no, country, title, description, case_type, hazard,
                       hazard_type, actual_severity, potential_severity, source,
                       hazard_label_method, actual_severity_label_method,
                       potential_severity_label_method
                FROM incidents
                WHERE dataset_split = ?
                ORDER BY sampling_rank, case_no
            """
            parameters: list[object] = [split]
            if limit is not None:
                query += " LIMIT ?"
                parameters.append(limit)
            count = 0
            destination = output_directory / f"{split}.jsonl"
            with destination.open("w", encoding="utf-8") as output:
                for row in connection.execute(query, parameters):
                    output.write(
                        json.dumps(training_record(row), ensure_ascii=False) + "\n"
                    )
                    count += 1
            counts[split] = count
    finally:
        connection.close()
    (output_directory / "manifest.json").write_text(
        json.dumps(
            {
                "task": "hsse_incident_classification",
                "source_database": str(database.resolve()),
                "counts": counts,
                "warning": (
                    "Several labels are deterministic weak labels. Evaluate on "
                    "the held-out test split and do not treat labels as ground truth."
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/processed/hsse_incidents.sqlite"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("data/training/hsse_sft"),
    )
    parser.add_argument(
        "--limit-per-split",
        type=int,
        help="Optional smoke-test limit; omit to export all 400,000 records.",
    )
    args = parser.parse_args()
    if args.limit_per_split is not None and args.limit_per_split <= 0:
        parser.error("--limit-per-split must be positive")
    counts = export(args.database, args.output_directory, args.limit_per_split)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
