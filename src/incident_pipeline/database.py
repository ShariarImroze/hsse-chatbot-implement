"""Load the corpus into a zero-cost SQLite database with full-text search."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .models import canonicalize_country


CREATE_INCIDENTS = """
CREATE TABLE incidents (
    case_no TEXT PRIMARY KEY,
    country TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    case_type TEXT NOT NULL,
    hazard TEXT NOT NULL,
    hazard_type TEXT NOT NULL,
    actual_severity TEXT NOT NULL,
    potential_severity TEXT NOT NULL,
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_license TEXT NOT NULL,
    event_date TEXT,
    raw_case_type TEXT,
    raw_hazard TEXT,
    raw_severity TEXT,
    title_provenance TEXT NOT NULL,
    hazard_label_method TEXT NOT NULL,
    actual_severity_label_method TEXT NOT NULL,
    potential_severity_label_method TEXT NOT NULL,
    text_hash TEXT NOT NULL UNIQUE,
    sampling_rank INTEGER NOT NULL,
    dataset_split TEXT NOT NULL CHECK(dataset_split IN ('train', 'validation', 'test'))
)
"""

_DATABASE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _sidecar_paths(database_path: Path) -> tuple[Path, ...]:
    return tuple(
        database_path.with_name(database_path.name + suffix)
        for suffix in _DATABASE_SIDECAR_SUFFIXES
    )


def _remove_temporary_database(temporary_path: Path) -> None:
    """Remove only the unique build database and its own sidecars."""

    for path in (temporary_path, *_sidecar_paths(temporary_path)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _new_temporary_database_path(database_path: Path) -> Path:
    """Reserve a unique build path beside the destination for atomic rename."""

    descriptor, name = tempfile.mkstemp(
        prefix=f".{database_path.name}.",
        suffix=".building",
        dir=database_path.parent,
    )
    os.close(descriptor)
    return Path(name)


def _database_uri(database_path: Path, mode: str) -> str:
    return f"{database_path.resolve().as_uri()}?mode={mode}"


def _quick_check(connection: sqlite3.Connection) -> None:
    results = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    if results != ["ok"]:
        raise RuntimeError(f"SQLite quick_check failed: {results}")


def _verify_fts_index(
    connection: sqlite3.Connection,
    expected_rows: int,
) -> None:
    """Check both FTS5 index/content consistency and visible row coverage."""

    # rank=1 asks FTS5 to compare an external-content index against its source
    # table, rather than merely checking the internal index structures.
    connection.execute(
        "INSERT INTO incidents_fts(incidents_fts, rank) "
        "VALUES ('integrity-check', 1)"
    )
    indexed_rows = int(
        connection.execute("SELECT COUNT(*) FROM incidents_fts").fetchone()[0]
    )
    if indexed_rows != expected_rows:
        raise RuntimeError(
            "FTS row coverage mismatch: "
            f"expected {expected_rows:,}, observed {indexed_rows:,}"
        )


def _verify_standalone_database(
    database_path: Path,
    expected_rows: int,
    fts_enabled: bool,
) -> tuple[int, dict[str, int]]:
    """Reopen the closed build read-only and verify its main file alone."""

    connection = sqlite3.connect(
        _database_uri(database_path, "ro"),
        uri=True,
        timeout=0,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        _quick_check(connection)
        row_count = int(
            connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        )
        if row_count != expected_rows:
            raise RuntimeError(
                "SQLite row count mismatch: "
                f"expected {expected_rows:,}, observed {row_count:,}"
            )
        source_counts = {
            str(source): int(count)
            for source, count in connection.execute(
                "SELECT source, COUNT(*) FROM incidents GROUP BY source"
            )
        }
        if sum(source_counts.values()) != expected_rows:
            raise RuntimeError(
                "SQLite source counts do not sum to the expected row count"
            )
        if fts_enabled:
            indexed_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM incidents_fts"
                ).fetchone()[0]
            )
            if indexed_rows != expected_rows:
                raise RuntimeError(
                    "Reopened FTS row coverage mismatch: "
                    f"expected {expected_rows:,}, observed {indexed_rows:,}"
                )
    finally:
        connection.close()
    return row_count, source_counts


def _assert_no_sidecars(database_path: Path, *, context: str) -> None:
    sidecars = [str(path) for path in _sidecar_paths(database_path) if path.exists()]
    if sidecars:
        raise RuntimeError(
            f"Refusing SQLite replacement because {context} sidecars remain: "
            + ", ".join(sidecars)
        )


def _checkpoint_existing_destination(database_path: Path) -> None:
    """Checkpoint a destination WAL or refuse replacement when it is busy.

    A truncating checkpoint is intentionally used here.  A passive checkpoint
    can report success while readers still pin frames in the WAL, which is not
    sufficient before atomically replacing the main database file.
    """

    if not database_path.exists():
        _assert_no_sidecars(database_path, context="orphaned destination")
        return

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _database_uri(database_path, "rw"),
            uri=True,
            timeout=0,
        )
        connection.execute("PRAGMA busy_timeout=0")
        checkpoint = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if checkpoint is None or len(checkpoint) != 3:
            raise RuntimeError(
                "Refusing SQLite replacement because the destination returned an "
                f"unexpected WAL checkpoint result: {checkpoint!r}"
            )
        busy, _, _ = (int(value) for value in checkpoint)
        if busy:
            raise RuntimeError(
                "Refusing SQLite replacement because the existing destination WAL "
                "checkpoint is busy"
            )
        journal_mode = str(
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        ).casefold()
        if journal_mode != "delete":
            raise RuntimeError(
                "Refusing SQLite replacement because the existing destination "
                f"could not leave WAL mode: journal_mode={journal_mode}"
            )
    except sqlite3.Error as error:
        raise RuntimeError(
            "Refusing SQLite replacement because the existing destination "
            f"could not complete a WAL checkpoint: {error}"
        ) from error
    finally:
        if connection is not None:
            connection.close()

    # A successful DELETE-mode transition under SQLite's own locking removes
    # an idle persistent WAL/SHM pair. Remaining sidecars indicate a reader,
    # unexpected filesystem state, or a race and still block replacement.
    _assert_no_sidecars(database_path, context="destination")


def _row_from_json(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record["Case No"],
        canonicalize_country(str(record["Country"])),
        record["Title"],
        record["Description"],
        record["Case Type"],
        record["Hazard"],
        record["Hazard Type"],
        record["Actual Severity"],
        record["Potential Severity"],
        record["source"],
        record["source_record_id"],
        record["source_url"],
        record["source_license"],
        record["event_date"],
        record["raw_case_type"],
        record["raw_hazard"],
        record["raw_severity"],
        record["title_provenance"],
        record["hazard_label_method"],
        record["actual_severity_label_method"],
        record["potential_severity_label_method"],
        record["text_hash"],
        record["sampling_rank"],
        record["dataset_split"],
    )


def _build_temporary_database(
    provenance_jsonl: Path,
    temporary_path: Path,
) -> tuple[int, bool]:
    """Build and validate the writable connection before closing it."""

    expected_rows = 0
    fts_enabled = False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary_path)
        journal_mode = str(
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        ).casefold()
        if journal_mode != "delete":
            raise RuntimeError(
                "Temporary SQLite database is not self-contained: "
                f"journal_mode={journal_mode}"
            )
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(CREATE_INCIDENTS)
        insert_sql = (
            "INSERT INTO incidents VALUES ("
            + ",".join("?" for _ in range(24))
            + ")"
        )
        batch: list[tuple[object, ...]] = []
        with provenance_jsonl.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                expected_rows += 1
                batch.append(_row_from_json(json.loads(line)))
                if len(batch) >= 2_000:
                    connection.executemany(insert_sql, batch)
                    batch.clear()
            if batch:
                connection.executemany(insert_sql, batch)

        for column in (
            "country",
            "case_type",
            "hazard_type",
            "actual_severity",
            "potential_severity",
            "source",
            "dataset_split",
        ):
            connection.execute(
                f"CREATE INDEX idx_incidents_{column} ON incidents({column})"
            )

        try:
            connection.execute(
                "CREATE VIRTUAL TABLE incidents_fts USING fts5("
                "case_no UNINDEXED, title, description, hazard, "
                "content='incidents', content_rowid='rowid')"
            )
            connection.execute(
                "INSERT INTO incidents_fts(incidents_fts) VALUES ('rebuild')"
            )
            fts_enabled = True
        except sqlite3.OperationalError as error:
            if "no such module: fts5" not in str(error).casefold():
                raise
        connection.commit()

        row_count = int(
            connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        )
        if row_count != expected_rows:
            raise RuntimeError(
                "SQLite row count mismatch before close: "
                f"expected {expected_rows:,}, observed {row_count:,}"
            )
        if fts_enabled:
            _verify_fts_index(connection, expected_rows)
            connection.commit()
        _quick_check(connection)
    finally:
        if connection is not None:
            connection.close()
    return expected_rows, fts_enabled


def load_sqlite(provenance_jsonl: Path, database_path: Path) -> dict[str, object]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _new_temporary_database_path(database_path)

    try:
        expected_rows, fts_enabled = _build_temporary_database(
            provenance_jsonl,
            temporary_path,
        )
        _assert_no_sidecars(temporary_path, context="temporary build")
        row_count, source_counts = _verify_standalone_database(
            temporary_path,
            expected_rows,
            fts_enabled,
        )
        _assert_no_sidecars(temporary_path, context="verified temporary build")

        # Checkpoint immediately before rename so a long build cannot rely on
        # an earlier, stale observation of the destination's WAL state.
        _checkpoint_existing_destination(database_path)
        os.replace(temporary_path, database_path)
    finally:
        _remove_temporary_database(temporary_path)

    result = {
        "database": str(database_path),
        "rows": row_count,
        "sources": source_counts,
        "full_text_search_enabled": fts_enabled,
    }
    print(f"Loaded {row_count:,} incidents into {database_path}")
    return result
