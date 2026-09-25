#!/usr/bin/env python3
"""Dependency-free, read-only MCP server for local HSSE chat history."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


SERVER_INFO = {"name": "hsse-chat-history", "version": "0.1.0"}

TOOLS = [
    {
        "name": "list_conversations",
        "description": "List recent locally saved HSSE chatbot conversations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_conversation",
        "description": "Get the messages and chart metadata for one HSSE chatbot conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "minLength": 8, "maxLength": 100},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 100,
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_conversations",
        "description": "Search saved HSSE chatbot messages for a phrase or keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


class ChatHistoryServer:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    def _connection(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(f"Chat history database not found: {self.database}")
        connection = sqlite3.connect(
            f"{self.database.as_uri()}?mode=ro", uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _bounded_int(value: Any, default: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("limit must be an integer")
        if not 1 <= value <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return value

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_conversations":
            return self.list_conversations(
                self._bounded_int(arguments.get("limit"), 20, 100)
            )
        if name == "get_conversation":
            session_id = arguments.get("session_id")
            if not isinstance(session_id, str) or not 8 <= len(session_id) <= 100:
                raise ValueError("session_id must contain 8–100 characters")
            return self.get_conversation(
                session_id,
                self._bounded_int(arguments.get("limit"), 100, 200),
            )
        if name == "search_conversations":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                raise ValueError("query must contain 1–500 characters")
            return self.search_conversations(
                query.strip(),
                self._bounded_int(arguments.get("limit"), 20, 100),
            )
        raise ValueError(f"Unknown tool: {name}")

    def list_conversations(self, limit: int) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.session_id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) AS message_count
                FROM chat_conversations AS c
                LEFT JOIN chat_messages AS m ON m.session_id = c.session_id
                GROUP BY c.session_id
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {"conversations": [dict(row) for row in rows]}

    def get_conversation(self, session_id: str, limit: int) -> dict[str, Any]:
        with self._connection() as connection:
            conversation = connection.execute(
                """
                SELECT session_id, title, created_at, updated_at
                FROM chat_conversations WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if conversation is None:
                raise ValueError("conversation not found")
            rows = connection.execute(
                """
                SELECT id, role, content, charts_json, model_key, model_id,
                       latency_ms, created_at
                FROM chat_messages WHERE session_id = ?
                ORDER BY id ASC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        messages = []
        for row in rows:
            item = dict(row)
            charts_json = item.pop("charts_json")
            item["charts"] = json.loads(charts_json) if charts_json else []
            messages.append(item)
        return {"conversation": dict(conversation), "messages": messages}

    def search_conversations(self, query: str, limit: int) -> dict[str, Any]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.session_id, c.title, m.id AS message_id, m.role,
                       m.content, m.model_key, m.model_id, m.latency_ms,
                       m.created_at
                FROM chat_messages AS m
                JOIN chat_conversations AS c ON c.session_id = m.session_id
                WHERE m.content LIKE ? ESCAPE '\\' COLLATE NOCASE
                ORDER BY m.id DESC LIMIT ?
                """,
                (pattern, limit),
            ).fetchall()
        return {"query": query, "matches": [dict(row) for row in rows]}


def response_payload(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_payload(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def run(database: Path) -> None:
    server = ChatHistoryServer(database)
    for raw_line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise ValueError("invalid tools/call parameters")
                data = server.call(name, arguments)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(data, ensure_ascii=False, indent=2),
                        }
                    ],
                    "structuredContent": data,
                    "isError": False,
                }
            else:
                payload = error_payload(request_id, -32601, "Method not found")
                print(json.dumps(payload, separators=(",", ":")), flush=True)
                continue
            if request_id is not None:
                print(
                    json.dumps(
                        response_payload(request_id, result), separators=(",", ":")
                    ),
                    flush=True,
                )
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
            request_id = request.get("id") if isinstance(request, dict) else None
            print(
                json.dumps(
                    error_payload(request_id, -32602, str(error)),
                    separators=(",", ":"),
                ),
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    run(args.database)


if __name__ == "__main__":
    main()
