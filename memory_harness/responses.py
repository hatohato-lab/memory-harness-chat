"""Answers written by the chat, handed to the kernel one request key at a time."""

from __future__ import annotations

from pathlib import Path

from .common import HarnessError, read_json


class ResponseFileProvider:
    name = "chat-responses"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.responses: dict = {}
        # A missing file is not an error here: every unanswered key is then
        # recorded by analyze()/review() as its own visible error instead.
        if self.path.is_file():
            document = read_json(self.path)
            responses = document.get("responses") if isinstance(document, dict) else None
            if not isinstance(responses, dict):
                raise HarnessError(f"responsesがキーから応答への辞書ではありません: {self.path}")
            self.responses = responses

    def respond(self, key: str, payload: dict, schema: dict):
        if key not in self.responses:
            raise HarnessError(f"応答がありません: {key}")
        return self.responses[key]
