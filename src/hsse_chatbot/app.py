"""Dependency-free local web UI and HTTP API for the HSSE chatbot."""

from __future__ import annotations

import argparse
import json
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from incident_pipeline.models import CASE_TYPES

from .config import ChatbotConfig
from .service import ChatService, SessionState
from .wizard import CaseWizard, WizardState


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
_MAX_REQUEST_BYTES = 100_000
_STATIC_PATH = Path(__file__).with_name("static") / "index.html"


def find_project_root(start: Path) -> Path:
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "incident_pipeline"
        ).is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not locate the project root. Run the command from the repository "
        "or pass --project-root."
    )


class ChatApplication:
    """Own per-browser wizard state while sharing one model and repository."""

    def __init__(self, service: ChatService) -> None:
        self.service = service
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def chat(
        self,
        session_id: str,
        message: str,
        history: list[dict[str, str]],
    ) -> dict[str, object]:
        with self._lock:
            state = self._sessions.get(session_id)
        if state is None:
            state = self._load_session(session_id)
        reply, charts, updated_state = self.service.respond_with_artifacts(
            message, history, state
        )
        with self._lock:
            self._sessions[session_id] = updated_state
        self._persist_session(session_id, updated_state)
        return {"reply": reply, "charts": charts}

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
        self.service.repository.delete_chat_draft(session_id)

    def _load_session(self, session_id: str) -> SessionState:
        payload = self.service.repository.load_chat_draft(session_id)
        if payload is None:
            return SessionState()
        try:
            wizard = self._wizard_from_payload(payload)
        except (KeyError, TypeError, ValueError):
            self.service.repository.delete_chat_draft(session_id)
            return SessionState()
        return SessionState(wizard=wizard)

    def _persist_session(self, session_id: str, state: SessionState) -> None:
        wizard = state.wizard
        if not wizard.active:
            self.service.repository.delete_chat_draft(session_id)
            return
        self.service.repository.save_chat_draft(
            session_id,
            {
                "active": True,
                "step_index": wizard.step_index,
                "values": wizard.values,
                "confirming": wizard.confirming,
                "suggested_case_type": wizard.suggested_case_type,
                "suggested_case_type_reason": wizard.suggested_case_type_reason,
            },
        )

    @staticmethod
    def _wizard_from_payload(payload: dict[str, object]) -> WizardState:
        if payload.get("active") is not True:
            raise ValueError("persisted draft is not active")
        step_index = payload.get("step_index")
        confirming = payload.get("confirming")
        values = payload.get("values")
        if not isinstance(step_index, int) or not 0 <= step_index < len(CaseWizard.steps):
            raise ValueError("invalid persisted wizard step")
        if not isinstance(confirming, bool) or not isinstance(values, dict):
            raise ValueError("invalid persisted wizard state")
        expected_keys = {
            step.key
            for step in (
                CaseWizard.steps if confirming else CaseWizard.steps[:step_index]
            )
        }
        if set(values) != expected_keys or not all(
            isinstance(value, str) and len(value) <= 20_000
            for value in values.values()
        ):
            raise ValueError("invalid persisted wizard values")
        suggestion = payload.get("suggested_case_type")
        reason = payload.get("suggested_case_type_reason")
        if suggestion is not None and suggestion not in CASE_TYPES:
            raise ValueError("invalid persisted case-type suggestion")
        if reason is not None and (
            not isinstance(reason, str) or len(reason) > 300
        ):
            raise ValueError("invalid persisted suggestion reason")
        return WizardState(
            active=True,
            step_index=step_index,
            values={str(key): str(value) for key, value in values.items()},
            confirming=confirming,
            suggested_case_type=suggestion,
            suggested_case_type_reason=reason,
        )


def _handler_factory(application: ChatApplication) -> type[BaseHTTPRequestHandler]:
    index_html = _STATIC_PATH.read_bytes()

    class ChatRequestHandler(BaseHTTPRequestHandler):
        server_version = "HSSEChatbot/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path == "/":
                self._send_bytes("text/html; charset=utf-8", index_html)
                return
            if self.path == "/api/status":
                source_count, user_count = application.service.repository.corpus_counts()
                self._send_json(
                    {
                        "model": application.service.config.model_id,
                        "backend": application.service.config.backend,
                        "source_cases": source_count,
                        "user_cases": user_count,
                    }
                )
                return
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path not in {"/api/chat", "/api/reset"}:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self._read_json()
                session_id = str(payload.get("session_id", ""))
                if not _SESSION_ID_PATTERN.fullmatch(session_id):
                    raise ValueError("invalid session_id")
                if self.path == "/api/reset":
                    application.reset(session_id)
                    self._send_json({"ok": True})
                    return

                message = str(payload.get("message", "")).strip()
                if not message or len(message) > 20_000:
                    raise ValueError("message must contain 1–20,000 characters")
                raw_history = payload.get("history", [])
                history = self._validated_history(raw_history)
                result = application.chat(session_id, message, history)
                self._send_json(result)
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(
                    {"error": str(error)}, HTTPStatus.BAD_REQUEST
                )
            except Exception as error:
                self._send_json(
                    {"error": f"Chat request failed: {error}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _read_json(self) -> dict[str, Any]:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if not 0 < content_length <= _MAX_REQUEST_BYTES:
                raise ValueError("request body is empty or too large")
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        @staticmethod
        def _validated_history(value: object) -> list[dict[str, str]]:
            if not isinstance(value, list):
                raise ValueError("history must be a list")
            validated: list[dict[str, str]] = []
            for item in value[-16:]:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                content = item.get("content")
                if role in {"user", "assistant"} and isinstance(content, str):
                    validated.append({"role": role, "content": content[:4000]})
            return validated

        def _send_json(
            self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, content_type: str, body: bytes) -> None:
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'",
            )

        def log_message(self, message_format: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {message_format % args}")

    return ChatRequestHandler


def create_server(
    service: ChatService,
    host: str = "127.0.0.1",
    port: int = 7860,
) -> ThreadingHTTPServer:
    application = ChatApplication(service)
    return ThreadingHTTPServer((host, port), _handler_factory(application))


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local HSSE Gemma chatbot")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--user-database", type=Path)
    parser.add_argument("--model-id")
    parser.add_argument(
        "--backend", choices=("llama-cpp", "transformers")
    )
    parser.add_argument("--model-base-url")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--max-new-tokens", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--open-browser", action="store_true")
    return parser


def main() -> None:
    args = _argument_parser().parse_args()
    project_root = (
        args.project_root.resolve()
        if args.project_root
        else find_project_root(Path.cwd())
    )
    config = ChatbotConfig.from_project_root(
        project_root,
        database_path=args.database,
        user_database_path=args.user_database,
        model_id=args.model_id,
        backend=args.backend,
        model_base_url=args.model_base_url,
        top_k=args.top_k,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        enable_thinking=(True if args.thinking else None),
        local_files_only=(True if args.local_files_only else None),
    )
    service = ChatService(config)
    server = create_server(service, args.host, args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"HSSE chatbot: {url}")
    print(f"Model (loaded on first analytical question): {config.model_id}")
    print(f"Model backend: {config.backend}")
    print(f"Corpus database: {config.database_path}")
    print(f"User cases: {config.user_database_path}")
    if args.open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping HSSE chatbot.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
