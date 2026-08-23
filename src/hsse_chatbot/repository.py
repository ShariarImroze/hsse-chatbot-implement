"""SQLite retrieval and isolated persistence for locally entered cases."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from incident_pipeline.models import canonicalize_country


_TOKEN_PATTERN = re.compile(r"[^\W_][\w'-]{1,}", re.UNICODE)
_STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "case",
    "cases",
    "could",
    "describe",
    "find",
    "for",
    "from",
    "give",
    "how",
    "incident",
    "incidents",
    "into",
    "me",
    "please",
    "show",
    "that",
    "the",
    "their",
    "these",
    "this",
    "those",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
}


@dataclass(frozen=True)
class RetrievedCase:
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
    rank: float
    is_user_case: bool = False


class IncidentRepository:
    """Search the immutable corpus and maintain a separate user-case overlay."""

    def __init__(self, database_path: Path, user_database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.user_database_path = user_database_path.resolve()
        if not self.database_path.is_file():
            raise FileNotFoundError(
                f"HSSE corpus database not found: {self.database_path}. "
                "Build the dataset first or set HSSE_DATABASE_PATH."
            )
        self.user_database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_user_database()

    @staticmethod
    def _readonly_uri(path: Path) -> str:
        return f"{path.as_uri()}?mode=ro"

    def _source_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._readonly_uri(self.database_path), uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        connection.create_function(
            "event_year", 2, self._event_year, deterministic=True
        )
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _event_year(source: object, value: object) -> str | None:
        """Normalize the source-specific stored date formats to a four-digit year."""

        source_text = str(source)
        value_text = str(value).strip()
        formats = {
            "OSHA_ITA_CASE_DETAIL": ("%Y-%m-%d",),
            "USCG_NRC": ("%m/%d/%Y %H:%M", "%m/%d/%Y"),
            "CALOES_SPILL": ("%Y-%m-%d",),
            "FAA_SDR": ("%m/%d/%Y",),
            "FDA_RES": ("%Y%m%d",),
            "POLICE_UK": ("%Y-%m",),
        }
        if source_text == "CFPB_CONSUMER_COMPLAINT":
            try:
                return str(
                    datetime.fromisoformat(value_text.replace("Z", "+00:00")).year
                )
            except ValueError:
                return None
        for date_format in formats.get(source_text, ()):
            try:
                return str(datetime.strptime(value_text, date_format).year)
            except ValueError:
                continue
        return None

    def _user_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.user_database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_user_database(self) -> None:
        with closing(self._user_connection()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS user_incidents (
                    case_no TEXT PRIMARY KEY,
                    country TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    case_type TEXT NOT NULL,
                    hazard TEXT NOT NULL,
                    hazard_type TEXT NOT NULL,
                    actual_severity TEXT NOT NULL,
                    potential_severity TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS user_incidents_fts USING fts5(
                    case_no UNINDEXED,
                    title,
                    description,
                    hazard,
                    content='user_incidents',
                    content_rowid='rowid'
                );
                CREATE TRIGGER IF NOT EXISTS user_incidents_after_insert
                AFTER INSERT ON user_incidents BEGIN
                    INSERT INTO user_incidents_fts(
                        rowid, case_no, title, description, hazard
                    ) VALUES (
                        new.rowid, new.case_no, new.title, new.description, new.hazard
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS user_incidents_after_delete
                AFTER DELETE ON user_incidents BEGIN
                    INSERT INTO user_incidents_fts(
                        user_incidents_fts, rowid, case_no, title, description, hazard
                    ) VALUES (
                        'delete', old.rowid, old.case_no, old.title,
                        old.description, old.hazard
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS user_incidents_after_update
                AFTER UPDATE ON user_incidents BEGIN
                    INSERT INTO user_incidents_fts(
                        user_incidents_fts, rowid, case_no, title, description, hazard
                    ) VALUES (
                        'delete', old.rowid, old.case_no, old.title,
                        old.description, old.hazard
                    );
                    INSERT INTO user_incidents_fts(
                        rowid, case_no, title, description, hazard
                    ) VALUES (
                        new.rowid, new.case_no, new.title, new.description, new.hazard
                    );
                END;
                CREATE TABLE IF NOT EXISTS chat_drafts (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def load_chat_draft(self, session_id: str) -> dict[str, object] | None:
        """Load a locally persisted wizard draft for one browser session."""

        with closing(self._user_connection()) as connection:
            row = connection.execute(
                "SELECT state_json FROM chat_drafts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["state_json"]))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def save_chat_draft(
        self, session_id: str, state: dict[str, object]
    ) -> None:
        """Persist an in-progress wizard draft so a local restart cannot lose it."""

        serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._user_connection()) as connection:
            connection.execute(
                """
                INSERT INTO chat_drafts (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, serialized, updated_at),
            )
            connection.commit()

    def delete_chat_draft(self, session_id: str) -> None:
        """Delete a completed, cancelled, or explicitly cleared wizard draft."""

        with closing(self._user_connection()) as connection:
            connection.execute(
                "DELETE FROM chat_drafts WHERE session_id = ?", (session_id,)
            )
            connection.commit()

    @staticmethod
    def build_fts_query(
        text: str, maximum_terms: int = 12, joiner: str = "OR"
    ) -> str:
        """Build a bounded, syntax-safe FTS5 OR query from user text."""

        if joiner not in {"OR", "AND"}:
            raise ValueError("FTS joiner must be OR or AND")

        terms: list[str] = []
        for match in _TOKEN_PATTERN.finditer(text.casefold()):
            token = match.group(0).strip("'-")
            if len(token) < 3 or token in _STOPWORDS or token in terms:
                continue
            escaped = token.replace('"', '""')
            terms.append(f'"{escaped}"*')
            if len(terms) >= maximum_terms:
                break
        return f" {joiner} ".join(terms)

    def search(self, text: str, limit: int = 6) -> list[RetrievedCase]:
        query = self.build_fts_query(text)
        if not query or limit <= 0:
            return []

        candidates = [
            *self._search_user_cases(query, limit),
            *self._search_source_cases(query, limit),
        ]
        candidates.sort(key=lambda case: (case.rank, not case.is_user_case))
        return candidates[:limit]

    def _search_source_cases(self, query: str, limit: int) -> list[RetrievedCase]:
        sql = """
            SELECT
                i.case_no, i.country, i.title,
                SUBSTR(i.description, 1, 2400) AS description,
                i.case_type, i.hazard, i.hazard_type,
                i.actual_severity, i.potential_severity, i.source,
                bm25(incidents_fts, 0.0, 4.0, 1.0, 2.0) AS rank
            FROM incidents_fts
            JOIN incidents AS i ON i.rowid = incidents_fts.rowid
            WHERE incidents_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        with closing(self._source_connection()) as connection:
            rows = connection.execute(sql, (query, limit)).fetchall()
        return [self._case_from_row(row, is_user_case=False) for row in rows]

    def _search_user_cases(self, query: str, limit: int) -> list[RetrievedCase]:
        sql = """
            SELECT
                i.case_no, i.country, i.title,
                SUBSTR(i.description, 1, 2400) AS description,
                i.case_type, i.hazard, i.hazard_type,
                i.actual_severity, i.potential_severity,
                'USER_ENTERED' AS source,
                bm25(user_incidents_fts, 0.0, 4.0, 1.0, 2.0) AS rank
            FROM user_incidents_fts
            JOIN user_incidents AS i ON i.rowid = user_incidents_fts.rowid
            WHERE user_incidents_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        with closing(self._user_connection()) as connection:
            rows = connection.execute(sql, (query, limit)).fetchall()
        return [self._case_from_row(row, is_user_case=True) for row in rows]

    @staticmethod
    def _case_from_row(row: sqlite3.Row, *, is_user_case: bool) -> RetrievedCase:
        return RetrievedCase(
            case_no=str(row["case_no"]),
            country=str(row["country"]),
            title=str(row["title"]),
            description=str(row["description"]),
            case_type=str(row["case_type"]),
            hazard=str(row["hazard"]),
            hazard_type=str(row["hazard_type"]),
            actual_severity=str(row["actual_severity"]),
            potential_severity=str(row["potential_severity"]),
            source=str(row["source"]),
            rank=float(row["rank"]),
            is_user_case=is_user_case,
        )

    def add_user_case(self, values: dict[str, str]) -> str:
        """Persist a validated case and return its generated local identifier."""

        now = datetime.now(timezone.utc)
        case_no = f"USR-{now:%Y%m%d}-{uuid4().hex[:10].upper()}"
        row = (
            case_no,
            canonicalize_country(values["country"].strip()),
            values["title"].strip(),
            values["description"].strip(),
            values["case_type"],
            values["hazard"].strip(),
            values["hazard_type"],
            values["actual_severity"],
            values["potential_severity"],
            now.isoformat(),
        )
        with closing(self._user_connection()) as connection:
            connection.execute(
                """
                INSERT INTO user_incidents (
                    case_no, country, title, description, case_type,
                    hazard, hazard_type, actual_severity,
                    potential_severity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            connection.commit()
        return case_no

    def corpus_counts(self) -> tuple[int, int]:
        with closing(self._source_connection()) as connection:
            source_count = int(
                connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            )
        with closing(self._user_connection()) as connection:
            user_count = int(
                connection.execute("SELECT COUNT(*) FROM user_incidents").fetchone()[0]
            )
        return source_count, user_count

    _CHART_EXPRESSIONS = {
        "case_type": "case_type",
        "country": "country",
        "hazard_type": "hazard_type",
        "actual_severity": "actual_severity",
        "potential_severity": "potential_severity",
        "source": "source",
        "dataset_split": "dataset_split",
        "event_year": "event_year(source, event_date)",
    }

    def aggregate_counts(
        self, dimensions: tuple[str, ...], limit: int
    ) -> list[tuple]:
        """Count source incidents by one or two allowlisted dimensions."""

        if not 1 <= len(dimensions) <= 2:
            raise ValueError("one or two chart dimensions are required")
        try:
            expressions = [self._CHART_EXPRESSIONS[item] for item in dimensions]
        except KeyError as error:
            raise ValueError(f"unsupported chart dimension: {error.args[0]}") from error
        limit = max(3, min(20, int(limit)))

        if len(expressions) == 1:
            expression = expressions[0]
            chronological = dimensions[0] == "event_year"
            order = "label ASC" if chronological else "value DESC, label ASC"
            limit_clause = "" if chronological else "LIMIT ?"
            sql = f"""
                SELECT {expression} AS label, COUNT(*) AS value
                FROM incidents
                WHERE {expression} IS NOT NULL AND TRIM({expression}) <> ''
                GROUP BY label
                ORDER BY {order}
                {limit_clause}
            """
            with closing(self._source_connection()) as connection:
                parameters = () if chronological else (limit,)
                rows = connection.execute(sql, parameters).fetchall()
            if chronological:
                rows = rows[-limit:]
            return [(str(row["label"]), int(row["value"])) for row in rows]

        primary, series = expressions
        sql = f"""
            WITH top_primary AS (
                SELECT {primary} AS label, COUNT(*) AS total
                FROM incidents
                WHERE {primary} IS NOT NULL AND TRIM({primary}) <> ''
                GROUP BY label
                ORDER BY total DESC, label ASC
                LIMIT ?
            )
            SELECT {primary} AS label, {series} AS series, COUNT(*) AS value
            FROM incidents
            JOIN top_primary ON {primary} = top_primary.label
            WHERE {series} IS NOT NULL AND TRIM({series}) <> ''
            GROUP BY label, series
            ORDER BY top_primary.total DESC, label ASC, value DESC, series ASC
        """
        with closing(self._source_connection()) as connection:
            rows = connection.execute(sql, (limit,)).fetchall()
        return [
            (str(row["label"]), str(row["series"]), int(row["value"]))
            for row in rows
        ]

    def description_word_count_distribution(self) -> list[tuple[str, int]]:
        """Return space-delimited word-count bins for all source descriptions."""

        word_count = (
            "LENGTH(TRIM(description)) - "
            "LENGTH(REPLACE(TRIM(description), ' ', '')) + 1"
        )
        sql = f"""
            SELECT CASE
                WHEN {word_count} <= 25 THEN '1–25'
                WHEN {word_count} <= 50 THEN '26–50'
                WHEN {word_count} <= 100 THEN '51–100'
                WHEN {word_count} <= 200 THEN '101–200'
                WHEN {word_count} <= 400 THEN '201–400'
                WHEN {word_count} <= 800 THEN '401–800'
                ELSE '801+'
            END AS label, COUNT(*) AS value,
            CASE
                WHEN {word_count} <= 25 THEN 1
                WHEN {word_count} <= 50 THEN 2
                WHEN {word_count} <= 100 THEN 3
                WHEN {word_count} <= 200 THEN 4
                WHEN {word_count} <= 400 THEN 5
                WHEN {word_count} <= 800 THEN 6
                ELSE 7
            END AS bin_order
            FROM incidents
            GROUP BY label
            ORDER BY bin_order
        """
        with closing(self._source_connection()) as connection:
            rows = connection.execute(sql).fetchall()
        return [(str(row["label"]), int(row["value"])) for row in rows]

    _DATASET_QUERY_EXPRESSIONS = {
        "case_no": "i.case_no",
        "country": "i.country",
        "title": "i.title",
        "description": "i.description",
        "case_type": "i.case_type",
        "hazard": "i.hazard",
        "hazard_type": "i.hazard_type",
        "actual_severity": "i.actual_severity",
        "potential_severity": "i.potential_severity",
        "source": "i.source",
        "dataset_split": "i.dataset_split",
        "event_year": "event_year(i.source, i.event_date)",
        "event_date": "i.event_date",
    }
    _EXACT_QUERY_FIELDS = {
        "case_no",
        "country",
        "case_type",
        "hazard_type",
        "actual_severity",
        "potential_severity",
        "dataset_split",
        "event_year",
    }

    def execute_dataset_query(
        self,
        *,
        operation: str,
        filters: tuple[tuple[str, str, tuple[str, ...]], ...],
        group_by: tuple[str, ...],
        search_text: str | None,
        search_mode: str,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        """Execute a validated full-corpus query using only allowlisted SQL."""

        if operation not in {"count", "group_count", "list_cases"}:
            raise ValueError("unsupported dataset query operation")
        if search_mode not in {"all", "any"}:
            raise ValueError("search mode must be all or any")
        limit = max(1, min(50, int(limit)))
        offset = max(0, min(399_999, int(offset)))

        joins = ""
        conditions: list[str] = []
        parameters: list[object] = []
        if search_text:
            fts_query = self.build_fts_query(
                search_text, joiner=("AND" if search_mode == "all" else "OR")
            )
            if fts_query:
                joins = "JOIN incidents_fts ON incidents_fts.rowid = i.rowid"
                conditions.append("incidents_fts MATCH ?")
                parameters.append(fts_query)

        for field, operator, values in filters:
            try:
                expression = self._DATASET_QUERY_EXPRESSIONS[field]
            except KeyError as error:
                raise ValueError(f"unsupported dataset filter: {field}") from error
            if operator == "eq" and len(values) == 1:
                if field in self._EXACT_QUERY_FIELDS:
                    conditions.append(f"{expression} = ?")
                else:
                    conditions.append(
                        f"LOWER(CAST({expression} AS TEXT)) = LOWER(?)"
                    )
                parameters.append(values[0])
            elif operator == "contains" and len(values) == 1:
                conditions.append(f"INSTR(LOWER({expression}), LOWER(?)) > 0")
                parameters.append(values[0])
            elif operator == "in" and values:
                if field in self._EXACT_QUERY_FIELDS:
                    placeholders = ", ".join("?" for _ in values)
                    conditions.append(f"{expression} IN ({placeholders})")
                else:
                    placeholders = ", ".join("LOWER(?)" for _ in values)
                    conditions.append(
                        f"LOWER(CAST({expression} AS TEXT)) IN ({placeholders})"
                    )
                parameters.extend(values)
            else:
                raise ValueError("invalid dataset filter")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        from_sql = f"FROM incidents AS i {joins} {where}"
        with closing(self._source_connection()) as connection:
            if operation == "count":
                total = int(
                    connection.execute(
                        f"SELECT COUNT(*) {from_sql}", parameters
                    ).fetchone()[0]
                )
                return {"total": total, "rows": []}

            if operation == "group_count":
                if not 1 <= len(group_by) <= 2:
                    raise ValueError("group_count requires one or two fields")
                try:
                    expressions = [
                        self._DATASET_QUERY_EXPRESSIONS[field] for field in group_by
                    ]
                except KeyError as error:
                    raise ValueError(
                        f"unsupported grouping field: {error.args[0]}"
                    ) from error
                select = ", ".join(
                    f"{expression} AS {field}"
                    for field, expression in zip(group_by, expressions)
                )
                group = ", ".join(expressions)
                matched_total = int(
                    connection.execute(
                        f"SELECT COUNT(*) {from_sql}", parameters
                    ).fetchone()[0]
                )
                group_total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM (SELECT 1 {from_sql} GROUP BY {group})",
                        parameters,
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    f"""
                    SELECT {select}, COUNT(*) AS count
                    {from_sql}
                    GROUP BY {group}
                    ORDER BY count DESC, {group}
                    LIMIT ? OFFSET ?
                    """,
                    (*parameters, limit, offset),
                ).fetchall()
                return {
                    "total": matched_total,
                    "group_total": group_total,
                    "rows": [dict(row) for row in rows],
                }

            total = int(
                connection.execute(
                    f"SELECT COUNT(*) {from_sql}", parameters
                ).fetchone()[0]
            )
            output_fields = (
                "case_no",
                "country",
                "title",
                "description",
                "case_type",
                "hazard",
                "hazard_type",
                "actual_severity",
                "potential_severity",
                "source",
                "event_date",
            )
            select = ", ".join(
                f"{self._DATASET_QUERY_EXPRESSIONS[field]} AS {field}"
                for field in output_fields
            )
            order = "bm25(incidents_fts), i.rowid" if joins else "i.rowid"
            id_rows = connection.execute(
                f"""
                SELECT i.rowid AS row_id
                {from_sql}
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            row_ids = [int(row["row_id"]) for row in id_rows]
            if not row_ids:
                return {"total": total, "rows": []}
            placeholders = ", ".join("?" for _ in row_ids)
            detail_rows = connection.execute(
                f"SELECT i.rowid AS row_id, {select} FROM incidents AS i "
                f"WHERE i.rowid IN ({placeholders})",
                row_ids,
            ).fetchall()
        details = {int(row["row_id"]): dict(row) for row in detail_rows}
        rows = [details[row_id] for row_id in row_ids]
        for row in rows:
            row.pop("row_id", None)
        return {"total": total, "rows": rows}
