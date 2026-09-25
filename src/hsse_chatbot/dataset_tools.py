"""Safe model-planned access to the complete local incident database."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from incident_pipeline.models import CASE_TYPES, canonicalize_country
from incident_pipeline.quality import HAZARD_TYPES

from .model import ChatGenerator, ModelLoadError
from .query_intent import extract_explicit_keyword_search
from .repository import IncidentRepository


_OPERATIONS = {"count", "group_count", "list_cases", "semantic_search"}
_FILTER_FIELDS = {
    "case_no",
    "country",
    "title",
    "case_type",
    "hazard",
    "hazard_type",
    "actual_severity",
    "potential_severity",
    "source",
    "dataset_split",
    "event_year",
}
_GROUP_FIELDS = {
    "country",
    "case_type",
    "hazard_type",
    "actual_severity",
    "potential_severity",
    "source",
    "dataset_split",
    "event_year",
}
_OUTPUT_COLUMNS = {
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
}
_DEFAULT_COLUMNS = ("case_no", "title", "country", "case_type")
_CANONICAL_FILTER_VALUES = {
    "case_type": CASE_TYPES,
    "hazard_type": tuple(HAZARD_TYPES),
    "actual_severity": ("Low", "Medium", "Severe", "Unknown"),
    "potential_severity": ("Low", "Medium", "Severe", "Unknown"),
    "dataset_split": ("train", "validation", "test"),
}
_FIELD_LABELS = {
    "case_no": "Case number",
    "country": "Country",
    "title": "Title",
    "description": "Description",
    "case_type": "Case type",
    "hazard": "Hazard",
    "hazard_type": "Hazard type",
    "actual_severity": "Actual severity",
    "potential_severity": "Potential severity",
    "source": "Source",
    "dataset_split": "Dataset split",
    "event_year": "Event year",
    "event_date": "Event date",
}

_QUERY_PLANNER_PROMPT = f"""You plan read-only queries for a local HSSE incident database.
Return exactly one JSON object with no Markdown or explanation:
{{"operation":"count","filters":[{{"field":"case_type","operator":"eq","value":"Process Safety"}}],"group_by":[],"search_text":null,"search_mode":"all","limit":20,"offset":0,"columns":["case_no","title"]}}

Allowed operations:
- count: exact number of all rows matching filters/search_text.
- group_count: exact counts grouped by one or two group_by fields.
- list_cases: matching case records, paginated with limit and offset.
- semantic_search: discussion, explanation, or similarity questions that need a
  small group of relevant narrative records rather than a full-corpus query.

Allowed filter fields: {", ".join(sorted(_FILTER_FIELDS))}.
Allowed filter operators: eq, contains, in.
Allowed group_by fields: {", ".join(sorted(_GROUP_FIELDS))}.
Allowed output columns: {", ".join(sorted(_OUTPUT_COLUMNS))}.
Exact case types: {", ".join(CASE_TYPES)}.
Severity values: Low, Medium, Severe, Unknown.

Rules:
- Use count for "how many", "count", "total", and "number of" questions.
- Use group_count for counts broken down by a category.
- Use list_cases when the user asks to show, list, or return records.
- Use semantic_search only when exact structured querying cannot answer.
- Put narrative keywords in search_text, not in a made-up filter field.
- search_mode is all when every keyword should match, otherwise any.
- Never output SQL, table names, code, regex, comments, or additional keys.
- limit is 1-50. offset is 0 or greater. Use offset for requested pages.
- filters is a list of at most 8 objects. An in value is a list of at most 20 strings.
- group_by contains at most 2 distinct fields.
- columns contains 1-6 distinct output columns and always includes case_no.
- Conversation history may contain incorrect earlier answers. Use it only to
  resolve references such as "those cases" or "next page".
- "400K" and "400,000" describe the corpus size. They are never filter values
  and must not be interpreted as a dataset_split, year, source, or category.
"""


@dataclass(frozen=True)
class DatasetFilter:
    field: str
    operator: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class DatasetQueryPlan:
    operation: str
    filters: tuple[DatasetFilter, ...] = ()
    group_by: tuple[str, ...] = ()
    search_text: str | None = None
    search_mode: str = "all"
    limit: int = 20
    offset: int = 0
    columns: tuple[str, ...] = _DEFAULT_COLUMNS
    exact_search: bool = False


class DatasetQueryPlanner:
    """Use the local model as a planner while enforcing a safe query schema."""

    def __init__(self, generator: ChatGenerator) -> None:
        self.generator = generator

    def plan(
        self, text: str, history: list[dict[str, str]] | None = None
    ) -> DatasetQueryPlan:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _QUERY_PLANNER_PROMPT}
        ]
        for item in (history or [])[-6:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                messages.append({"role": role, "content": content[:2000]})
        messages.append({"role": "user", "content": text[:4000]})
        try:
            response = self.generator.generate(messages)
            parsed = self._parse_json_object(response)
            plan = self._validated_plan(parsed)
            return self._enforce_obvious_intent(text, plan)
        except (ModelLoadError, ValueError, TypeError, json.JSONDecodeError):
            return self._heuristic_plan(text)

    @staticmethod
    def _parse_json_object(value: str) -> dict[str, object]:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            raise ValueError("dataset planner did not return JSON")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("dataset query plan must be an object")
        return parsed

    @classmethod
    def _validated_plan(cls, value: dict[str, object]) -> DatasetQueryPlan:
        operation = str(value.get("operation", "semantic_search")).casefold()
        if operation not in _OPERATIONS:
            raise ValueError("unsupported dataset operation")

        raw_filters = value.get("filters", [])
        if not isinstance(raw_filters, list):
            raise ValueError("filters must be a list")
        filters: list[DatasetFilter] = []
        for item in raw_filters[:8]:
            if not isinstance(item, dict):
                raise ValueError("each filter must be an object")
            field = str(item.get("field", ""))
            operator = str(item.get("operator", "eq")).casefold()
            if field not in _FILTER_FIELDS or operator not in {"eq", "contains", "in"}:
                raise ValueError("unsupported dataset filter")
            raw_value = item.get("value")
            if operator == "in":
                if not isinstance(raw_value, list) or not raw_value:
                    raise ValueError("in filters require a non-empty list")
                values = tuple(str(entry).strip()[:200] for entry in raw_value[:20])
            else:
                if isinstance(raw_value, (dict, list)) or raw_value is None:
                    raise ValueError("filter value must be text")
                values = (str(raw_value).strip()[:200],)
            if not all(values):
                raise ValueError("filter values cannot be empty")
            if field in _CANONICAL_FILTER_VALUES:
                canonical = {
                    entry.casefold(): entry
                    for entry in _CANONICAL_FILTER_VALUES[field]
                }
                try:
                    values = tuple(canonical[entry.casefold()] for entry in values)
                except KeyError as error:
                    raise ValueError(
                        f"invalid categorical value for {field}: {error.args[0]}"
                    ) from error
            if field == "country":
                values = tuple(canonicalize_country(entry) for entry in values)
            if field == "event_year":
                if any(
                    not re.fullmatch(r"\d{4}", entry)
                    or not 1900 <= int(entry) <= 2100
                    for entry in values
                ):
                    raise ValueError("event_year values must be years from 1900–2100")
            filters.append(DatasetFilter(field, operator, values))

        raw_group = value.get("group_by", [])
        if not isinstance(raw_group, list):
            raise ValueError("group_by must be a list")
        group_by = tuple(str(item) for item in raw_group[:2])
        if any(item not in _GROUP_FIELDS for item in group_by):
            raise ValueError("unsupported grouping field")
        if len(group_by) != len(set(group_by)):
            raise ValueError("grouping fields must be distinct")
        if operation == "group_count" and not group_by:
            raise ValueError("group_count requires a grouping field")

        raw_search = value.get("search_text")
        search_text = None
        if raw_search is not None:
            if not isinstance(raw_search, str):
                raise ValueError("search_text must be text or null")
            search_text = raw_search.strip()[:500] or None
        search_mode = str(value.get("search_mode", "all")).casefold()
        if search_mode not in {"all", "any"}:
            raise ValueError("search_mode must be all or any")

        try:
            limit = max(1, min(50, int(value.get("limit", 20))))
            offset = max(0, min(399_999, int(value.get("offset", 0))))
        except (TypeError, ValueError) as error:
            raise ValueError("limit and offset must be integers") from error

        raw_columns = value.get("columns", list(_DEFAULT_COLUMNS))
        if not isinstance(raw_columns, list):
            raise ValueError("columns must be a list")
        columns = tuple(str(item) for item in raw_columns[:6])
        if not columns or any(item not in _OUTPUT_COLUMNS for item in columns):
            raise ValueError("unsupported output column")
        columns = tuple(dict.fromkeys(("case_no", *columns)))[:6]
        return DatasetQueryPlan(
            operation,
            tuple(filters),
            group_by,
            search_text,
            search_mode,
            limit,
            offset,
            columns,
        )

    @classmethod
    def _enforce_obvious_intent(
        cls, text: str, plan: DatasetQueryPlan
    ) -> DatasetQueryPlan:
        explicit = extract_explicit_keyword_search(text)
        if explicit is not None:
            search_text, search_mode = explicit
            plan = DatasetQueryPlan(
                plan.operation,
                tuple(
                    item
                    for item in plan.filters
                    if item.field not in {"title", "description", "hazard"}
                ),
                plan.group_by,
                search_text,
                search_mode,
                plan.limit,
                plan.offset,
                plan.columns,
                True,
            )
        if cls._is_count_request(text) and plan.operation == "semantic_search":
            fallback = cls._heuristic_plan(text)
            if fallback.operation != "semantic_search":
                return fallback
        return plan

    @staticmethod
    def _is_count_request(text: str) -> bool:
        return bool(
            re.search(
                r"\b(how many|count|total number|number of|breakdown|grouped by)\b",
                text,
                re.I,
            )
        )

    @classmethod
    def _heuristic_plan(cls, text: str) -> DatasetQueryPlan:
        lowered = text.casefold()
        filters: list[DatasetFilter] = []
        for case_type in CASE_TYPES:
            if case_type.casefold() in lowered:
                filters.append(DatasetFilter("case_type", "eq", (case_type,)))
                break

        group_by: list[str] = []
        group_phrases = (
            ("by case type", "case_type"),
            ("by country", "country"),
            ("by hazard type", "hazard_type"),
            ("by actual severity", "actual_severity"),
            ("by potential severity", "potential_severity"),
            ("by source", "source"),
            ("by year", "event_year"),
        )
        for phrase, field in group_phrases:
            if phrase in lowered:
                group_by.append(field)

        if group_by:
            operation = "group_count"
        elif cls._is_count_request(text):
            operation = "count"
        elif re.search(r"\b(list|show|return|give me)\b.*\b(case|cases|incident|incidents)\b", lowered):
            operation = "list_cases"
        else:
            operation = "semantic_search"
        return DatasetQueryPlan(operation, tuple(filters), tuple(group_by[:2]))


class DatasetQueryService:
    """Execute validated plans and format exact, non-hallucinated answers."""

    def __init__(self, repository: IncidentRepository, generator: ChatGenerator) -> None:
        self.repository = repository
        self.planner = DatasetQueryPlanner(generator)

    def answer(
        self, text: str, history: list[dict[str, str]] | None = None
    ) -> str | None:
        plan = self.planner.plan(text, history)
        if plan.operation == "semantic_search":
            return None
        result = self.repository.execute_dataset_query(
            operation=plan.operation,
            filters=tuple(
                (item.field, item.operator, item.values) for item in plan.filters
            ),
            group_by=plan.group_by,
            search_text=plan.search_text,
            search_mode=plan.search_mode,
            limit=plan.limit,
            offset=plan.offset,
            exact_search=plan.exact_search,
        )
        if plan.operation == "count":
            return self._format_count(result, plan)
        if plan.operation == "group_count":
            return self._format_groups(result, plan)
        return self._format_cases(result, plan)

    def _format_count(
        self, result: dict[str, object], plan: DatasetQueryPlan
    ) -> str:
        count = int(result["total"])
        scope = self._scope(plan)
        return (
            f"The complete source corpus contains **{count:,} matching incidents**"
            f"{scope}.\n\nThis count was calculated directly over the local SQLite "
            "dataset, not inferred from retrieved examples."
        )

    def _format_groups(
        self, result: dict[str, object], plan: DatasetQueryPlan
    ) -> str:
        rows = result["rows"]
        if not isinstance(rows, list) or not rows:
            return "No source-corpus incidents matched that query."
        group_total = int(result.get("group_total", len(rows)))
        first = plan.offset + 1
        last = plan.offset + len(rows)
        headers = [*(_FIELD_LABELS[item] for item in plan.group_by), "Incidents"]
        lines = [
            "Counts calculated over the complete source corpus"
            + self._scope(plan)
            + f". Showing groups **{first:,}–{last:,} of {group_total:,}**:",
            "",
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        for row in rows:
            if not isinstance(row, dict):
                continue
            values = [self._cell(row.get(field, "")) for field in plan.group_by]
            values.append(f"{int(row.get('count', 0)):,}")
            lines.append("| " + " | ".join(values) + " |")
        if last < group_total:
            lines.append(
                f"\nThere are {group_total - last:,} additional groups. Ask for "
                f"the next grouped page starting at offset {last}."
            )
        return "\n".join(lines)

    def _format_cases(
        self, result: dict[str, object], plan: DatasetQueryPlan
    ) -> str:
        total = int(result["total"])
        rows = result["rows"]
        if not isinstance(rows, list) or not rows:
            return "No source-corpus incidents matched that query."
        first = plan.offset + 1
        last = plan.offset + len(rows)
        lines = [
            f"Found **{total:,} matching incidents** in the complete source corpus. "
            f"Showing **{first:,}–{last:,}**:",
            "",
            "| " + " | ".join(_FIELD_LABELS[item] for item in plan.columns) + " |",
            "| " + " | ".join("---" for _ in plan.columns) + " |",
        ]
        for row in rows:
            if not isinstance(row, dict):
                continue
            lines.append(
                "| "
                + " | ".join(self._cell(row.get(field, "")) for field in plan.columns)
                + " |"
            )
        if last < total:
            lines.append(
                f"\nThere are {total - last:,} additional matches. Ask for the next "
                f"page starting at offset {last}."
            )
        return "\n".join(lines)

    @staticmethod
    def _scope(plan: DatasetQueryPlan) -> str:
        descriptions: list[str] = []
        for item in plan.filters:
            values = ", ".join(item.values)
            descriptions.append(f"{_FIELD_LABELS[item.field]} {item.operator} {values}")
        if plan.search_text:
            descriptions.append(f"keywords: {plan.search_text}")
        return " for " + "; ".join(descriptions) if descriptions else ""

    @staticmethod
    def _cell(value: object) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip()
        if len(text) > 500:
            text = text[:497] + "…"
        return text.replace("|", "\\|")
