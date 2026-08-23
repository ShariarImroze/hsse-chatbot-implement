import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsse_chatbot.app import ChatApplication
from hsse_chatbot.config import ChatbotConfig
from hsse_chatbot.model import LlamaCppServerGenerator
from hsse_chatbot.repository import IncidentRepository
from hsse_chatbot.service import ChatService, SessionState
from hsse_chatbot.wizard import HAZARD_TYPE_CHOICES


def _create_source_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
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
                event_date TEXT,
                dataset_split TEXT NOT NULL DEFAULT 'train'
            );
            CREATE VIRTUAL TABLE incidents_fts USING fts5(
                case_no UNINDEXED,
                title,
                description,
                hazard,
                content='incidents',
                content_rowid='rowid'
            );
            """
        )
        connection.execute(
            """
            INSERT INTO incidents (
                case_no, country, title, description, case_type, hazard,
                hazard_type, actual_severity, potential_severity, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "TEST:001",
                "United States of America",
                "Pump seal fire",
                "A failed pump seal released flammable liquid which ignited near the unit.",
                "Process Safety",
                "Flammable liquid release near ignition source",
                "Fire/Explosion",
                "Medium",
                "Severe",
                "TEST_SOURCE",
            ),
        )
        connection.execute(
            "INSERT INTO incidents_fts(rowid, case_no, title, description, hazard) "
            "SELECT rowid, case_no, title, description, hazard FROM incidents"
        )
        connection.commit()
    finally:
        connection.close()


class _FakeGenerator:
    def __init__(self) -> None:
        self.messages = []

    def generate(self, messages):
        self.messages = messages
        return "The retrieved pump event involved fire [TEST:001]."


class ChatbotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_database = self.root / "incidents.sqlite"
        self.user_database = self.root / "user_cases.sqlite"
        _create_source_database(self.source_database)
        self.repository = IncidentRepository(
            self.source_database, self.user_database
        )
        self.config = ChatbotConfig(
            project_root=self.root,
            database_path=self.source_database,
            user_database_path=self.user_database,
            top_k=4,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_fts_query_is_bounded_and_searches_source_database(self) -> None:
        query = self.repository.build_fts_query(
            "Please show cases about pump seal fire and explosion"
        )
        self.assertIn('"pump"*', query)
        self.assertNotIn('"cases"*', query)
        results = self.repository.search("pump fire", limit=3)
        self.assertEqual([case.case_no for case in results], ["TEST:001"])
        self.assertFalse(results[0].is_user_case)

    def test_source_specific_event_year_normalization(self) -> None:
        self.assertEqual(
            self.repository._event_year("USCG_NRC", "4/14/2022 15:37"),
            "2022",
        )
        self.assertEqual(
            self.repository._event_year("FDA_RES", "20250103"), "2025"
        )
        self.assertIsNone(self.repository._event_year("USCG_NRC", "not-a-date"))

    def test_chat_uses_retrieved_case_as_evidence(self) -> None:
        generator = _FakeGenerator()
        service = ChatService(
            self.config, repository=self.repository, generator=generator
        )
        reply, _ = service.respond("What pump fire cases are available?", [], None)
        self.assertIn("TEST:001", reply)
        final_prompt = generator.messages[-1]["content"]
        self.assertIn("Case: TEST:001", final_prompt)
        self.assertIn("balanced", generator.messages[0]["content"])

    def test_plain_language_count_uses_complete_dataset_query(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        reply, _ = service.respond(
            "In the dataset, how many Process Safety incidents are there?", [], None
        )
        self.assertIn("**1 matching incidents**", reply)
        self.assertIn("calculated directly", reply)

    def test_model_planned_group_count_is_parameterized_and_exact(self) -> None:
        class _QueryGenerator:
            def generate(self, messages):
                return json.dumps(
                    {
                        "operation": "group_count",
                        "filters": [],
                        "group_by": ["case_type"],
                        "search_text": None,
                        "search_mode": "all",
                        "limit": 20,
                        "offset": 0,
                        "columns": ["case_no", "title"],
                    }
                )

        service = ChatService(
            self.config, repository=self.repository, generator=_QueryGenerator()
        )
        reply, _ = service.respond("Break down incidents by case type", [], None)
        self.assertIn("Process Safety", reply)
        self.assertIn("| 1 |", reply)

    def test_dataset_query_planner_cannot_execute_model_sql(self) -> None:
        class _UnsafeQueryGenerator:
            def generate(self, messages):
                return json.dumps(
                    {
                        "operation": "count",
                        "filters": [
                            {
                                "field": "case_type); DROP TABLE incidents; --",
                                "operator": "eq",
                                "value": "Process Safety",
                            }
                        ],
                    }
                )

        service = ChatService(
            self.config,
            repository=self.repository,
            generator=_UnsafeQueryGenerator(),
        )
        reply, _ = service.respond("How many Process Safety cases?", [], None)
        self.assertIn("**1 matching incidents**", reply)
        with self.repository._source_connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_invalid_model_category_filter_uses_safe_intent_fallback(self) -> None:
        class _InvalidCategoryGenerator:
            def generate(self, messages):
                return json.dumps(
                    {
                        "operation": "count",
                        "filters": [
                            {
                                "field": "case_type",
                                "operator": "eq",
                                "value": "Process Safety",
                            },
                            {
                                "field": "dataset_split",
                                "operator": "eq",
                                "value": "400K",
                            },
                        ],
                    }
                )

        service = ChatService(
            self.config,
            repository=self.repository,
            generator=_InvalidCategoryGenerator(),
        )
        reply, _ = service.respond(
            "In the 400K dataset, how many Process Safety incidents are there?",
            [],
            None,
        )
        self.assertIn("**1 matching incidents**", reply)
        self.assertNotIn("Dataset split", reply)

    def test_chart_request_returns_validated_corpus_counts(self) -> None:
        generator = _FakeGenerator()
        service = ChatService(
            self.config, repository=self.repository, generator=generator
        )
        reply, charts, _ = service.respond_with_artifacts(
            "Show a bar chart of incidents by country", [], None
        )
        self.assertIn("complete source corpus", reply)
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]["type"], "bar")
        self.assertEqual(charts[0]["labels"], ["United States of America"])
        self.assertEqual(charts[0]["datasets"][0]["values"], [1])

    def test_chart_planner_rejects_model_sql_and_uses_safe_fallback(self) -> None:
        class _UnsafeGenerator:
            def generate(self, messages):
                return '{"chart_type":"bar","group_by":["DROP TABLE incidents"]}'

        service = ChatService(
            self.config, repository=self.repository, generator=_UnsafeGenerator()
        )
        _, charts, _ = service.respond_with_artifacts(
            "Graph incidents by case type", [], None
        )
        self.assertEqual(charts[0]["labels"], ["Process Safety"])
        with self.repository._source_connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_llama_cpp_backend_uses_local_chat_completions_api(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = (
            b'{"choices":[{"message":{"content":"Local answer"}}]}'
        )
        response.__enter__.return_value = response
        with mock.patch("hsse_chatbot.model.urlopen", return_value=response) as urlopen:
            generator = LlamaCppServerGenerator(self.config)
            result = generator.generate([{"role": "user", "content": "Hello"}])
        self.assertEqual(result, "Local answer")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "http://127.0.0.1:8080/v1/chat/completions"
        )
        payload = json.loads(request.data)
        self.assertEqual(
            payload["chat_template_kwargs"], {"enable_thinking": False}
        )
        self.assertEqual(payload["thinking_budget_tokens"], 0)

    def test_llama_cpp_backend_accepts_openai_content_parts(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = (
            b'{"choices":[{"message":{"content":'
            b'[{"type":"text","text":"Local "},'
            b'{"type":"text","text":"answer"}]}}]}'
        )
        response.__enter__.return_value = response
        with mock.patch("hsse_chatbot.model.urlopen", return_value=response):
            generator = LlamaCppServerGenerator(self.config)
            result = generator.generate([{"role": "user", "content": "Hello"}])
        self.assertEqual(result, "Local answer")

    def test_guided_case_creation_saves_to_overlay_and_becomes_searchable(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        state = SessionState()
        reply, state = service.respond("add case", [], state)
        self.assertIn("Step 1 of 8", reply)

        description = (
            "During a routine transfer, a hose connection failed and released product "
            "near hot equipment before operators isolated the line and raised the alarm."
        )
        hazard_choice = str(HAZARD_TYPE_CHOICES.index("Fire/Explosion") + 1)
        answers = (
            "United States",
            "Transfer hose release",
            description,
            "3",
            "Flammable product release near hot equipment",
            hazard_choice,
            "1",
            "3",
        )
        for answer in answers:
            reply, state = service.respond(answer, [], state)

        self.assertIn("Review the new case", reply)
        reply, state = service.respond("save", [], state)
        self.assertIn("USR-", reply)
        self.assertFalse(state.wizard.active)
        source_count, user_count = self.repository.corpus_counts()
        self.assertEqual((source_count, user_count), (1, 1))
        results = self.repository.search("transfer hose release", limit=4)
        self.assertTrue(any(case.is_user_case for case in results))

    def test_model_suggests_case_type_and_yes_accepts_it(self) -> None:
        class _SuggestionGenerator:
            def __init__(self) -> None:
                self.messages = []

            def generate(self, messages):
                self.messages = messages
                return json.dumps(
                    {
                        "case_type": "Process Safety",
                        "reason": "The oil spill is a loss of containment.",
                    }
                )

        generator = _SuggestionGenerator()
        service = ChatService(
            self.config, repository=self.repository, generator=generator
        )
        state = SessionState()
        _, state = service.respond("add case", [], state)
        _, state = service.respond("Germany", [], state)
        _, state = service.respond("Oil spill and wrist fracture", [], state)
        description = (
            "Oil and water spilled beside turbine one, causing the on-duty engineer "
            "to slip, fracture a wrist, receive first aid, and attend hospital."
        )
        reply, state = service.respond(description, [], state)

        self.assertIn("Suggested case type: **Process Safety**", reply)
        self.assertIn("The oil spill is a loss of containment", reply)
        self.assertIn("Reply **yes**", reply)
        self.assertNotIn("case_type", state.wizard.values)
        self.assertIn("Oil spill and wrist fracture", generator.messages[-1]["content"])

        reply, state = service.respond("Yes, that recommendation is correct.", [], state)
        self.assertIn("Recorded **Case Type**: Process Safety", reply)
        self.assertIn("Step 5 of 8", reply)
        self.assertEqual(state.wizard.values["case_type"], "Process Safety")

    def test_user_can_override_case_type_suggestion_in_natural_language(self) -> None:
        class _SuggestionGenerator:
            def generate(self, messages):
                return json.dumps(
                    {
                        "case_type": "Process Safety",
                        "reason": "The description mentions a release.",
                    }
                )

        service = ChatService(
            self.config,
            repository=self.repository,
            generator=_SuggestionGenerator(),
        )
        state = SessionState()
        _, state = service.respond("add case", [], state)
        for answer in (
            "Canada",
            "Worker slipped beside equipment",
            (
                "A worker slipped beside operating equipment during a routine round, "
                "fell onto one arm, reported pain, and was sent for medical assessment."
            ),
        ):
            _, state = service.respond(answer, [], state)

        reply, state = service.respond(
            "No, this should be Occupational Safety.", [], state
        )
        self.assertIn("Recorded **Case Type**: Occupational Safety", reply)
        self.assertEqual(state.wizard.values["case_type"], "Occupational Safety")

    def test_invalid_model_suggestion_uses_deterministic_fallback(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        state = SessionState()
        _, state = service.respond("add case", [], state)
        for answer in (
            "Germany",
            "Turbine area oil spill",
            (
                "Oil spilled from a process line beside turbine one before operators "
                "isolated the equipment, contained the release, and notified the supervisor."
            ),
        ):
            reply, state = service.respond(answer, [], state)

        self.assertIn("Suggested case type: **Process Safety**", reply)
        self.assertEqual(state.wizard.suggested_case_type, "Process Safety")

    def test_suggest_case_type_repeats_wizard_recommendation(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        state = SessionState()
        _, state = service.respond("add case", [], state)
        for answer in (
            "Germany",
            "Turbine process line spill",
            (
                "Oil spilled from a process line beside turbine one before operators "
                "isolated the equipment, contained the release, and notified the supervisor."
            ),
        ):
            _, state = service.respond(answer, [], state)

        reply, state = service.respond("suggest case type", [], state)
        self.assertIn("Suggested case type: **Process Safety**", reply)
        self.assertIn("Nothing will be recorded until", reply)
        self.assertEqual(state.wizard.step_index, 3)
        self.assertNotIn("case_type", state.wizard.values)

    def test_case_recording_status_does_not_start_or_advance_wizard(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        reply, state = service.respond(
            "did you record the add case?", [], SessionState()
        )
        self.assertIn("no active add-case draft", reply.casefold())
        self.assertIn("**0 saved user-entered cases**", reply)
        self.assertFalse(state.wizard.active)
        self.assertNotIn("Step 1 of 8", reply)

        _, state = service.respond("add case", [], state)
        reply, state = service.respond("did you record the add case?", [], state)
        self.assertIn("**unsaved draft**", reply)
        self.assertIn("Step 1 of 8", reply)
        self.assertEqual(state.wizard.step_index, 0)

    def test_unfinished_wizard_is_recovered_from_chat_history(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        state = SessionState()
        history: list[dict[str, str]] = []

        reply, state = service.respond("add case", history, state)
        history.extend(
            (
                {"role": "user", "content": "add case"},
                {"role": "assistant", "content": reply},
            )
        )
        answers = (
            "Germany",
            "Turbine area oil spill",
            (
                "Oil spilled from a process line beside turbine one before operators "
                "isolated the equipment, contained the release, and notified the supervisor."
            ),
            "suggest case type",
        )
        for answer in answers:
            reply, state = service.respond(answer, history, state)
            history.extend(
                (
                    {"role": "user", "content": answer},
                    {"role": "assistant", "content": reply},
                )
            )

        history.extend(
            (
                {"role": "user", "content": "Occupational safety"},
                {
                    "role": "assistant",
                    "content": "The complete corpus contains 50,000 matching incidents.",
                },
                {"role": "user", "content": "did you record the add case?"},
                {
                    "role": "assistant",
                    "content": service.wizard.start().message,
                },
            )
        )
        reply, recovered = service.respond(
            "Slippery oil-contaminated floor", history, SessionState()
        )
        self.assertIn("Recorded **Hazard**: Slippery oil-contaminated floor", reply)
        self.assertIn("Step 6 of 8", reply)
        self.assertEqual(recovered.wizard.step_index, 5)
        self.assertEqual(
            recovered.wizard.values["case_type"], "Occupational Safety"
        )

    def test_wizard_draft_persists_across_application_restart(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        first_application = ChatApplication(service)
        session_id = "persist-session-01"
        first_application.chat(session_id, "add case", [])
        first_application.chat(session_id, "Germany", [])

        restarted_application = ChatApplication(service)
        result = restarted_application.chat(
            session_id, "Persistent turbine event", []
        )
        self.assertIn("Step 3 of 8", result["reply"])
        draft = self.repository.load_chat_draft(session_id)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["values"]["country"], "Germany")
        self.assertEqual(
            draft["values"]["title"], "Persistent turbine event"
        )

        restarted_application.reset(session_id)
        self.assertIsNone(self.repository.load_chat_draft(session_id))

    def test_wizard_rejects_short_description_and_lower_potential_severity(self) -> None:
        service = ChatService(
            self.config, repository=self.repository, generator=_FakeGenerator()
        )
        state = SessionState()
        _, state = service.respond("add incident", [], state)
        for answer in ("Canada", "Warehouse equipment event"):
            _, state = service.respond(answer, [], state)
        reply, state = service.respond("Too short to save.", [], state)
        self.assertIn("at least 20", reply)
        self.assertEqual(state.wizard.step_index, 2)


if __name__ == "__main__":
    unittest.main()
