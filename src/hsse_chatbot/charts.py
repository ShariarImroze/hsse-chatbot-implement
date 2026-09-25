"""Model-planned, server-validated charts over the local incident corpus."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .model import ChatGenerator, ModelLoadError
from .query_intent import extract_explicit_keyword_search
from .repository import IncidentRepository


_CHART_INTENT = re.compile(
    r"\b(chart|graph|plot|visuali[sz](e|ation)|histogram|pie)\b", re.I
)
_ALLOWED_DIMENSIONS = {
    "case_type",
    "country",
    "hazard_type",
    "actual_severity",
    "potential_severity",
    "source",
    "dataset_split",
    "event_year",
    "description_word_count",
}
_ALLOWED_TYPES = {"bar", "line", "pie"}
_DIMENSION_LABELS = {
    "case_type": "Case type",
    "country": "Country",
    "hazard_type": "Hazard type",
    "actual_severity": "Actual severity",
    "potential_severity": "Potential severity",
    "source": "Source",
    "dataset_split": "Dataset split",
    "event_year": "Event year",
    "description_word_count": "Description word count",
}

_PLANNER_PROMPT = """You plan charts for a local HSSE incident database.
Return exactly one JSON object and no Markdown or explanation:
{"chart_type":"bar","group_by":["case_type"],"top_n":10,
 "log_scale":false,"title":"Incidents by case type",
 "search_text":null,"search_mode":"all"}

Rules:
- chart_type must be bar, line, or pie.
- group_by must contain one or two of: case_type, country, hazard_type,
  actual_severity, potential_severity, source, dataset_split, event_year,
  description_word_count.
- Use two dimensions for requests such as "case type by country". Put the
  category that should appear on the axis first and the series second.
- Use event_year with line for trends over time.
- Use description_word_count with bar for word-count distributions.
- Use pie only when explicitly requested; otherwise use bar for categories.
- top_n must be 3 through 20. Use 10 unless the user requests another limit.
- log_scale is true only when the user explicitly requests a logarithmic scale.
- The title must be neutral and describe what is plotted.
- Put report-text keywords in search_text. Use search_mode "any" when any
  alternative may match (for example, "crack or cracks"); otherwise use "all".
- search_text must be text or null, and search_mode must be all or any.
- The only metric is incident count. Never emit SQL, code, or new keys.
"""


@dataclass(frozen=True)
class ChartPlan:
    chart_type: str
    group_by: tuple[str, ...]
    top_n: int
    log_scale: bool
    title: str
    search_text: str | None = None
    search_mode: str = "all"
    exact_search: bool = False


class ChartPlanner:
    """Ask the model for a plan, then reduce it to a strict safe schema."""

    def __init__(self, generator: ChatGenerator) -> None:
        self.generator = generator

    @staticmethod
    def is_chart_request(text: str) -> bool:
        return bool(_CHART_INTENT.search(text))

    def plan(self, text: str) -> ChartPlan:
        try:
            response = self.generator.generate(
                [
                    {"role": "system", "content": _PLANNER_PROMPT},
                    {"role": "user", "content": text[:4000]},
                ]
            )
            parsed = self._parse_json_object(response)
            return self._enforce_keyword_intent(text, self._validated_plan(parsed))
        except (ModelLoadError, ValueError, TypeError, json.JSONDecodeError):
            return self._heuristic_plan(text)

    @staticmethod
    def _parse_json_object(value: str) -> dict[str, object]:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            raise ValueError("chart planner did not return JSON")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("chart plan must be an object")
        return parsed

    @staticmethod
    def _validated_plan(value: dict[str, object]) -> ChartPlan:
        chart_type = str(value.get("chart_type", "bar")).casefold()
        if chart_type not in _ALLOWED_TYPES:
            raise ValueError("unsupported chart type")

        raw_dimensions = value.get("group_by")
        if not isinstance(raw_dimensions, list):
            raise ValueError("group_by must be a list")
        dimensions = tuple(str(item) for item in raw_dimensions[:2])
        if not dimensions or any(item not in _ALLOWED_DIMENSIONS for item in dimensions):
            raise ValueError("unsupported chart dimension")
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("chart dimensions must be distinct")
        if "description_word_count" in dimensions and len(dimensions) != 1:
            raise ValueError("word-count distribution cannot be a series")

        try:
            top_n = max(3, min(20, int(value.get("top_n", 10))))
        except (TypeError, ValueError) as error:
            raise ValueError("top_n must be an integer") from error
        log_scale = value.get("log_scale") is True
        title = re.sub(r"[\x00-\x1f]+", " ", str(value.get("title", ""))).strip()
        if not title:
            title = f"Incidents by {_DIMENSION_LABELS[dimensions[0]].casefold()}"
        raw_search = value.get("search_text")
        if raw_search is not None and not isinstance(raw_search, str):
            raise ValueError("search_text must be text or null")
        search_text = raw_search.strip()[:500] if isinstance(raw_search, str) else None
        search_text = search_text or None
        search_mode = str(value.get("search_mode", "all")).casefold()
        if search_mode not in {"all", "any"}:
            raise ValueError("search_mode must be all or any")
        return ChartPlan(
            chart_type,
            dimensions,
            top_n,
            log_scale,
            title[:120],
            search_text,
            search_mode,
            False,
        )

    @staticmethod
    def _enforce_keyword_intent(text: str, plan: ChartPlan) -> ChartPlan:
        explicit = extract_explicit_keyword_search(text)
        if explicit is None:
            return plan
        search_text, search_mode = explicit
        return ChartPlan(
            plan.chart_type,
            plan.group_by,
            plan.top_n,
            plan.log_scale,
            plan.title,
            search_text,
            search_mode,
            True,
        )

    @staticmethod
    def _heuristic_plan(text: str) -> ChartPlan:
        lowered = text.casefold()
        dimensions: list[str] = []
        candidates = (
            ("word count", "description_word_count"),
            ("case type", "case_type"),
            ("hazard type", "hazard_type"),
            ("actual severity", "actual_severity"),
            ("potential severity", "potential_severity"),
            ("country", "country"),
            ("source", "source"),
            ("dataset split", "dataset_split"),
        )
        for phrase, field in candidates:
            if phrase in lowered and field not in dimensions:
                dimensions.append(field)
        if re.search(r"\b(year|yearly|over time|trend|timeline)\b", lowered):
            dimensions = ["event_year"]
        if not dimensions:
            dimensions = ["case_type"]

        # "case type by country" reads most naturally as countries on the axis
        # with case-type series.
        if "case_type" in dimensions and "country" in dimensions:
            dimensions = ["country", "case_type"]
        dimensions = dimensions[:2]
        chart_type = "line" if dimensions[0] == "event_year" else "bar"
        if "pie" in lowered:
            chart_type = "pie"
        number = re.search(r"\b(?:top|first)\s+(\d{1,2})\b", lowered)
        top_n = max(3, min(20, int(number.group(1)))) if number else 10
        log_scale = bool(re.search(r"\b(log|logarithmic)\b", lowered))
        label = " by ".join(_DIMENSION_LABELS[item].casefold() for item in dimensions)
        plan = ChartPlan(
            chart_type,
            tuple(dimensions),
            top_n,
            log_scale,
            f"Incidents by {label}",
        )
        return ChartPlanner._enforce_keyword_intent(text, plan)


class ChartBuilder:
    """Turn a validated plan into a small JSON-safe chart specification."""

    def __init__(self, repository: IncidentRepository) -> None:
        self.repository = repository

    def build(self, plan: ChartPlan) -> dict[str, object]:
        if plan.group_by == ("description_word_count",):
            rows = self.repository.description_word_count_distribution(
                plan.search_text,
                plan.search_mode,
                plan.exact_search,
            )
        else:
            rows = self.repository.aggregate_counts(
                plan.group_by,
                plan.top_n,
                plan.search_text,
                plan.search_mode,
                plan.exact_search,
            )
        if not rows:
            raise ValueError("No chartable records were found for that request.")

        if len(plan.group_by) == 1:
            labels = [str(row[0]) for row in rows]
            values = [int(row[1]) for row in rows]
            chart_type = plan.chart_type
            if chart_type == "pie" and len(labels) > 8:
                chart_type = "bar"
            datasets = [{"label": "Incidents", "values": values}]
        else:
            chart_type = "stacked_bar"
            labels, datasets = self._stacked_data(rows)

        corpus_total = self.repository.corpus_counts()[0]
        if plan.search_text:
            count_result = self.repository.execute_dataset_query(
                operation="count",
                filters=(),
                group_by=(),
                search_text=plan.search_text,
                search_mode=plan.search_mode,
                exact_search=plan.exact_search,
                limit=1,
                offset=0,
            )
            total_scope = f"{int(count_result['total']):,} matching source cases"
        else:
            total_scope = f"{corpus_total:,} source cases"
        if plan.group_by == ("description_word_count",):
            scope = " · Space-delimited description-length bins"
        else:
            dimension_label = _DIMENSION_LABELS[plan.group_by[0]].casefold()
            scope = f" · Up to {plan.top_n} {dimension_label} values"
        return {
            "type": chart_type,
            "title": plan.title,
            "subtitle": f"Incident count{scope} · {total_scope}",
            "labels": labels,
            "datasets": datasets,
            "log_scale": plan.log_scale,
            "x_label": _DIMENSION_LABELS[plan.group_by[0]],
            "y_label": "Incident count",
        }

    @staticmethod
    def _stacked_data(
        rows: list[tuple[str, str, int]],
    ) -> tuple[list[str], list[dict[str, object]]]:
        labels: list[str] = []
        series_totals: dict[str, int] = {}
        values: dict[tuple[str, str], int] = {}
        for label, series, count in rows:
            if label not in labels:
                labels.append(label)
            series_totals[series] = series_totals.get(series, 0) + count
            values[(label, series)] = count

        kept_series = [
            name
            for name, _ in sorted(
                series_totals.items(), key=lambda item: (-item[1], item[0])
            )[:8]
        ]
        datasets: list[dict[str, object]] = []
        for series in kept_series:
            datasets.append(
                {
                    "label": series,
                    "values": [values.get((label, series), 0) for label in labels],
                }
            )
        omitted = set(series_totals) - set(kept_series)
        if omitted:
            datasets.append(
                {
                    "label": "Other",
                    "values": [
                        sum(values.get((label, series), 0) for series in omitted)
                        for label in labels
                    ],
                }
            )
        return labels, datasets


class ChartService:
    def __init__(self, repository: IncidentRepository, generator: ChatGenerator) -> None:
        self.planner = ChartPlanner(generator)
        self.builder = ChartBuilder(repository)

    def create(self, text: str) -> dict[str, object]:
        return self.builder.build(self.planner.plan(text))
