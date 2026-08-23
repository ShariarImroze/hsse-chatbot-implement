"""Deterministic, step-by-step collection of user-entered incident cases."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from incident_pipeline.models import CASE_TYPES, canonicalize_country
from incident_pipeline.quality import HAZARD_TYPES
from incident_pipeline.taxonomy import SEVERITY_ORDER, classify_hazard
from incident_pipeline.text import clean_text, word_count

from .case_type import CaseTypeSuggestion


SEVERITY_CHOICES = ("Low", "Medium", "Severe", "Unknown")
HAZARD_TYPE_CHOICES = tuple(
    sorted(HAZARD_TYPES, key=lambda value: (value == "Other/Unknown", value))
)


@dataclass
class WizardState:
    active: bool = False
    step_index: int = 0
    values: dict[str, str] = field(default_factory=dict)
    confirming: bool = False
    suggested_case_type: str | None = None
    suggested_case_type_reason: str | None = None


@dataclass(frozen=True)
class WizardTurn:
    message: str
    state: WizardState
    values_to_save: dict[str, str] | None = None


@dataclass(frozen=True)
class _Step:
    key: str
    label: str
    question: str


class CaseWizard:
    """Collect the public incident fields without delegating validation to an LLM."""

    steps = (
        _Step("country", "Country", "Which country did the incident occur in?"),
        _Step("title", "Title", "Enter a short, descriptive incident title."),
        _Step(
            "description",
            "Description",
            "Describe what happened in at least 20 words. Include the event, "
            "affected people/assets, and known outcome.",
        ),
        _Step("case_type", "Case Type", "Choose the case type."),
        _Step(
            "hazard",
            "Hazard",
            "Describe the specific hazard in a short phrase or sentence.",
        ),
        _Step("hazard_type", "Hazard Type", "Choose the standardized hazard type."),
        _Step("actual_severity", "Actual Severity", "Choose the actual severity."),
        _Step(
            "potential_severity",
            "Potential Severity",
            "Choose the credible potential severity.",
        ),
    )

    def __init__(
        self,
        case_type_suggester: Callable[[str, str], CaseTypeSuggestion] | None = None,
    ) -> None:
        self.case_type_suggester = case_type_suggester

    def start(self) -> WizardTurn:
        state = WizardState(active=True)
        return WizardTurn(
            "I’ll guide you through one field at a time. Type **cancel** at any "
            "time, or **back** to change the previous answer.\n\n"
            + self._step_prompt(state),
            state,
        )

    def handle(self, user_text: str, state: WizardState) -> WizardTurn:
        text = clean_text(user_text)
        command = text.casefold()
        if command in {"cancel", "stop", "quit"}:
            return WizardTurn(
                "Case creation cancelled. Nothing was saved.", WizardState()
            )
        if command == "back":
            return self._go_back(state)
        if state.confirming:
            return self._handle_confirmation(command, state)

        step = self.steps[state.step_index]
        if step.key == "case_type" and self._requests_case_type_suggestion(command):
            return WizardTurn(
                "Here is the recommendation for this draft. Nothing will be "
                "recorded until you confirm a case type and complete all remaining "
                "steps.\n\n"
                + self._step_prompt(state),
                state,
            )
        try:
            normalized = self._validate(step.key, text, state)
        except ValueError as error:
            return WizardTurn(
                f"That value is not valid: {error}\n\n{self._step_prompt(state)}",
                state,
            )

        values = dict(state.values)
        values[step.key] = normalized
        next_index = state.step_index + 1
        if next_index == len(self.steps):
            confirmation_state = WizardState(
                active=True,
                step_index=state.step_index,
                values=values,
                confirming=True,
                suggested_case_type=state.suggested_case_type,
                suggested_case_type_reason=state.suggested_case_type_reason,
            )
            return WizardTurn(self._summary(values), confirmation_state)

        suggested_case_type = state.suggested_case_type
        suggested_case_type_reason = state.suggested_case_type_reason
        if step.key == "description" and self.case_type_suggester is not None:
            suggestion = self.case_type_suggester(
                values.get("title", ""), values["description"]
            )
            suggested_case_type = suggestion.case_type
            suggested_case_type_reason = suggestion.reason

        next_state = WizardState(
            active=True,
            step_index=next_index,
            values=values,
            suggested_case_type=suggested_case_type,
            suggested_case_type_reason=suggested_case_type_reason,
        )
        return WizardTurn(
            f"Recorded **{step.label}**: {normalized}\n\n"
            + self._step_prompt(next_state),
            next_state,
        )

    def _go_back(self, state: WizardState) -> WizardTurn:
        if state.confirming:
            previous_index = len(self.steps) - 1
        elif state.step_index > 0:
            previous_index = state.step_index - 1
        else:
            return WizardTurn(
                "You are already at the first step.\n\n" + self._step_prompt(state),
                state,
            )
        previous_key = self.steps[previous_index].key
        values = dict(state.values)
        values.pop(previous_key, None)
        previous_state = WizardState(
            active=True,
            step_index=previous_index,
            values=values,
            suggested_case_type=(
                None if previous_key == "description" else state.suggested_case_type
            ),
            suggested_case_type_reason=(
                None
                if previous_key == "description"
                else state.suggested_case_type_reason
            ),
        )
        return WizardTurn(
            f"Going back to **{self.steps[previous_index].label}**.\n\n"
            + self._step_prompt(previous_state),
            previous_state,
        )

    def _handle_confirmation(self, command: str, state: WizardState) -> WizardTurn:
        if command in {"save", "confirm", "yes"}:
            return WizardTurn(
                "Saving the case…", WizardState(), dict(state.values)
            )
        return WizardTurn(
            "Type **save** to store this case, **back** to change the final field, "
            "or **cancel** to discard it.\n\n"
            + self._summary(state.values),
            state,
        )

    def _step_prompt(self, state: WizardState) -> str:
        step = self.steps[state.step_index]
        prompt = (
            f"**Step {state.step_index + 1} of {len(self.steps)} — {step.label}**\n\n"
            f"{step.question}"
        )
        if step.key == "case_type":
            if state.suggested_case_type:
                prompt += (
                    f"\n\nSuggested case type: **{state.suggested_case_type}**"
                )
                if state.suggested_case_type_reason:
                    prompt += f"\n\nReason: {state.suggested_case_type_reason}"
                prompt += (
                    "\n\nReply **yes** or **suggested** to accept this "
                    "recommendation. To override it, reply with another number, "
                    "the case-type name, or a sentence such as “No, this should be "
                    "Occupational Safety.”"
                )
            prompt += "\n\n" + self._numbered_choices(CASE_TYPES)
        elif step.key == "hazard_type":
            suggestion = classify_hazard(
                state.values.get("description", ""), state.values.get("hazard", "")
            )
            prompt += (
                f"\n\nSuggested from the description: **{suggestion}**. "
                "Type **suggested** to accept it, or choose a number:\n\n"
                + self._numbered_choices(HAZARD_TYPE_CHOICES)
            )
        elif step.key in {"actual_severity", "potential_severity"}:
            prompt += "\n\n" + self._numbered_choices(SEVERITY_CHOICES)
        return prompt

    @staticmethod
    def _numbered_choices(choices: tuple[str, ...]) -> str:
        return "\n".join(
            f"{index}. {choice}" for index, choice in enumerate(choices, start=1)
        )

    def _validate(self, key: str, value: str, state: WizardState) -> str:
        existing_values = state.values
        if not value:
            raise ValueError("a value is required")
        if key == "country":
            if len(value) < 2 or len(value) > 100:
                raise ValueError("country must contain 2–100 characters")
            return canonicalize_country(value)
        if key == "title":
            if len(value) < 5 or len(value) > 200:
                raise ValueError("title must contain 5–200 characters")
            return value
        if key == "description":
            words = word_count(value)
            if words < 20:
                raise ValueError(
                    f"description has {words} words; at least 20 are required"
                )
            if len(value) > 20_000:
                raise ValueError("description must not exceed 20,000 characters")
            return value
        if key == "case_type":
            return self._case_type_choice(value, state.suggested_case_type)
        if key == "hazard":
            if len(value) < 3 or len(value) > 500:
                raise ValueError("hazard must contain 3–500 characters")
            return value
        if key == "hazard_type":
            if value.casefold() in {"suggested", "suggestion", "recommended"}:
                return classify_hazard(
                    existing_values.get("description", ""),
                    existing_values.get("hazard", ""),
                )
            return self._choice(value, HAZARD_TYPE_CHOICES, "hazard type")
        if key in {"actual_severity", "potential_severity"}:
            severity = self._choice(value, SEVERITY_CHOICES, "severity")
            if key == "potential_severity":
                actual = existing_values["actual_severity"]
                if SEVERITY_ORDER[severity] < SEVERITY_ORDER[actual]:
                    raise ValueError(
                        f"potential severity cannot be below actual severity ({actual})"
                    )
            return severity
        raise ValueError(f"unknown field: {key}")

    @staticmethod
    def _requests_case_type_suggestion(command: str) -> bool:
        return "case type" in command and any(
            word in command for word in ("suggest", "recommend", "which", "what")
        )

    def draft_status(self, state: WizardState) -> str:
        """Explain whether the current draft has been saved without consuming a step."""

        if state.confirming:
            continuation = self._summary(state.values)
        else:
            continuation = self._step_prompt(state)
        return (
            "No — this case is still an **unsaved draft**. It is recorded only "
            "after all 8 steps are complete and you type **save** on the review "
            "screen.\n\n"
            + continuation
        )

    @classmethod
    def _case_type_choice(cls, value: str, suggested: str | None) -> str:
        normalized = value.casefold()
        try:
            return cls._choice(value, CASE_TYPES, "case type")
        except ValueError:
            mentioned = [
                choice for choice in CASE_TYPES if choice.casefold() in normalized
            ]
            number_match = re.search(r"\b([1-8])\b", value)
            if len(mentioned) == 1:
                return mentioned[0]
            if number_match is not None:
                return CASE_TYPES[int(number_match.group(1)) - 1]
            accepts_suggestion = normalized in {
                "suggested",
                "suggestion",
                "recommended",
                "recommendation",
            } or re.match(
                r"^(?:yes|y|confirm(?:ed)?|accept(?:ed)?)(?:\b|$)", normalized
            )
            if accepts_suggestion:
                if suggested is None:
                    raise ValueError(
                        "no recommendation is available; choose a case type"
                    )
                return suggested
            raise ValueError(
                "reply yes to accept the suggestion, or choose another listed "
                "case type by number or name"
            )

    @staticmethod
    def _choice(value: str, choices: tuple[str, ...], label: str) -> str:
        try:
            selected_index = int(value) - 1
        except ValueError:
            selected_index = -1
        if 0 <= selected_index < len(choices):
            return choices[selected_index]
        normalized = value.casefold()
        for choice in choices:
            if choice.casefold() == normalized:
                return choice
        raise ValueError(f"choose a listed {label} by number or exact name")

    @staticmethod
    def _summary(values: dict[str, str]) -> str:
        rows = [
            ("Country", values["country"]),
            ("Title", values["title"]),
            ("Description", values["description"]),
            ("Case Type", values["case_type"]),
            ("Hazard", values["hazard"]),
            ("Hazard Type", values["hazard_type"]),
            ("Actual Severity", values["actual_severity"]),
            ("Potential Severity", values["potential_severity"]),
        ]
        summary = "\n".join(f"- **{label}:** {value}" for label, value in rows)
        return (
            "**Review the new case**\n\n"
            + summary
            + "\n\nType **save** to store it, **back** to change the final field, "
            "or **cancel** to discard it."
        )
