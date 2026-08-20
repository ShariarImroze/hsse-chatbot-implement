"""Canonicalize United States labels in existing generated dataset artifacts."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIRECTORY = PROJECT_ROOT / "src"
if str(SRC_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SRC_DIRECTORY))

from incident_pipeline.models import canonicalize_country  # noqa: E402


def _temporary_path(path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".country-normalizing",
    )
    os.close(descriptor)
    return Path(name)


def _replace(path: Path, temporary_path: Path) -> None:
    temporary_path.chmod(path.stat().st_mode)
    os.replace(temporary_path, path)


def normalize_json_array(path: Path) -> tuple[int, int]:
    with path.open(encoding="utf-8") as stream:
        records = json.load(stream)

    updated = 0
    for record in records:
        country = str(record["Country"])
        canonical = canonicalize_country(country)
        if canonical != country:
            record["Country"] = canonical
            updated += 1

    if updated:
        temporary_path = _temporary_path(path)
        try:
            with temporary_path.open("w", encoding="utf-8") as stream:
                stream.write("[\n")
                for index, record in enumerate(records):
                    if index:
                        stream.write(",\n")
                    json.dump(record, stream, ensure_ascii=False)
                stream.write("\n]\n")
            _replace(path, temporary_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    return len(records), updated


def normalize_jsonl(path: Path) -> tuple[int, int]:
    rows = 0
    updated = 0
    temporary_path = _temporary_path(path)
    try:
        with path.open(encoding="utf-8") as source, temporary_path.open(
            "w", encoding="utf-8"
        ) as destination:
            for line in source:
                if not line.strip():
                    continue
                record = json.loads(line)
                rows += 1
                country = str(record["Country"])
                canonical = canonicalize_country(country)
                if canonical != country:
                    record["Country"] = canonical
                    updated += 1
                json.dump(record, destination, ensure_ascii=False)
                destination.write("\n")
        if updated:
            _replace(path, temporary_path)
        else:
            temporary_path.unlink()
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return rows, updated


def normalize_csv(path: Path) -> tuple[int, int]:
    rows = 0
    updated = 0
    temporary_path = _temporary_path(path)
    try:
        with path.open(encoding="utf-8", newline="") as source, temporary_path.open(
            "w", encoding="utf-8", newline=""
        ) as destination:
            reader = csv.DictReader(source)
            if not reader.fieldnames or "Country" not in reader.fieldnames:
                raise ValueError(f"Expected a Country column in {path}")
            writer = csv.DictWriter(destination, fieldnames=reader.fieldnames)
            writer.writeheader()
            for record in reader:
                rows += 1
                country = str(record["Country"])
                canonical = canonicalize_country(country)
                if canonical != country:
                    record["Country"] = canonical
                    updated += 1
                writer.writerow(record)
        if updated:
            _replace(path, temporary_path)
        else:
            temporary_path.unlink()
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return rows, updated


def normalize_sqlite(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path, timeout=30)
    connection.create_function(
        "canonicalize_country",
        1,
        lambda value: canonicalize_country(str(value)),
        deterministic=True,
    )
    try:
        rows = int(connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0])
        updated = int(
            connection.execute(
                "SELECT COUNT(*) FROM incidents "
                "WHERE canonicalize_country(country) != country"
            ).fetchone()[0]
        )
        if updated:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE incidents SET country = canonicalize_country(country) "
                "WHERE canonicalize_country(country) != country"
            )
            if cursor.rowcount != updated:
                raise RuntimeError(
                    f"Expected to update {updated:,} rows in {path}, updated {cursor.rowcount:,}"
                )
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            if quick_check != "ok":
                raise RuntimeError(f"SQLite quick check failed for {path}: {quick_check}")
            connection.commit()
        else:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            if quick_check != "ok":
                raise RuntimeError(f"SQLite quick check failed for {path}: {quick_check}")
        busy, wal_frames, checkpointed_frames = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if busy or wal_frames or checkpointed_frames:
            raise RuntimeError(
                "SQLite WAL checkpoint did not truncate cleanly for "
                f"{path}: busy={busy}, WAL={wal_frames}, checkpointed={checkpointed_frames}"
            )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return rows, updated


def main() -> None:
    targets = (
        (normalize_json_array, PROJECT_ROOT / "data" / "processed" / "pilot_1000.json"),
        (
            normalize_jsonl,
            PROJECT_ROOT / "data" / "processed" / "pilot_1000_with_provenance.jsonl",
        ),
        (normalize_json_array, PROJECT_ROOT / "data" / "processed" / "master_400K.json"),
        (
            normalize_jsonl,
            PROJECT_ROOT / "data" / "processed" / "master_400K_with_provenance.jsonl",
        ),
        (
            normalize_sqlite,
            PROJECT_ROOT / "data" / "processed" / "hsse_incidents.sqlite",
        ),
        (normalize_csv, PROJECT_ROOT / "reports" / "pilot_label_review.csv"),
    )

    total_rows = 0
    total_updated = 0
    for normalizer, path in targets:
        rows, updated = normalizer(path)
        total_rows += rows
        total_updated += updated
        print(f"{path.relative_to(PROJECT_ROOT)}: {rows:,} rows; {updated:,} updated")
    print(f"Total: {total_rows:,} rows scanned; {total_updated:,} country labels updated")


if __name__ == "__main__":
    main()
