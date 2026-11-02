from types import SimpleNamespace
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import web_app
from web_app import _complete_text_response


def response(text, stop_reason):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason)


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


class CompletionTests(unittest.TestCase):
    def test_continues_truncated_answer(self):
        client = FakeClient([response("后续内容。", "end_turn")])
        initial = response("### 2. ", "max_tokens")

        result = _complete_text_response(client, {"model": "test", "messages": [{"role": "user", "content": "方案"}]}, initial)

        self.assertEqual(result, "### 2. 后续内容。")
        self.assertEqual(client.calls[0]["messages"][-2]["content"], "### 2. ")
        self.assertEqual(client.calls[0]["messages"][-1]["role"], "user")

    def test_does_not_return_partial_answer_after_continuation_limit(self):
        client = FakeClient([response("续", "max_tokens") for _ in range(5)])
        with self.assertRaisesRegex(ValueError, "尚未完整"):
            _complete_text_response(client, {"messages": [{"role": "user", "content": "方案"}]}, response("开头", "max_tokens"))

    def test_repair_updates_existing_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "test-session"
            with patch.object(web_app, "STATE_DIR", root), patch.object(web_app, "STATE_PATH", root / "sessions.json"), patch.object(web_app, "OUTPUT_DIR", root / "generated"), patch.object(web_app, "_client", return_value=FakeClient([response("后续内容。", "end_turn")])), patch.object(web_app, "_model", return_value="test"), patch.object(web_app, "_phase_context_for_id", return_value="架构方案"):
                web_app._save_state({"sessions": [{"id": session_id, "status": "running", "messages": [{"role": "assistant", "content": "### 2. "}], "phase_results": {}}]})
                web_app._operations["test-operation"] = {"status": "running"}
                try:
                    web_app._continue_job("test-operation", session_id, "architecture", "### 2. ", "交通 App")
                    session = web_app._find_session(web_app._read_state(), session_id)
                    self.assertEqual(session["messages"][-1]["content"], "### 2. 后续内容。")
                    self.assertEqual(session["status"], "ready")
                    self.assertEqual(web_app._operations["test-operation"]["status"], "completed")
                finally:
                    del web_app._operations["test-operation"]


if __name__ == "__main__":
    unittest.main()
