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


def test_deepseek_forced_tool_requests_disable_thinking():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        snapshot = {'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com/anthropic', 'api_key': 'test-key'}
        for phase, task_mode, tool_name in [('brief', 'discovery', 'discovery_update'), ('frontend', 'phase', 'save_files')]:
            client = FakeClient([response('text', 'end_turn')])
            operation = f'test-{phase}'
            with patch.object(web_app, 'STATE_DIR', root), patch.object(web_app, 'STATE_PATH', root / 'sessions.json'), patch.object(web_app, 'OUTPUT_DIR', root / 'generated'), patch.object(web_app, '_client', return_value=client):
                web_app._save_state({'sessions': [{'id': 'session', 'status': 'running', 'messages': [], 'phase_results': {}}]})
                web_app._operations[operation] = {'status': 'running'}
                try:
                    web_app._run_job(operation, 'session', phase, 'test', phase != 'brief', task_mode, snapshot)
                    assert client.calls[0]['tool_choice'] == {'type': 'tool', 'name': tool_name}
                    assert client.calls[0]['thinking'] == {'type': 'disabled'}
                finally:
                    del web_app._operations[operation]


def test_compatibility_does_not_change_other_providers_or_plain_requests():
    forced = {'tool_choice': {'type': 'tool', 'name': 'discovery_update'}}
    web_app._apply_tool_compatibility({'base_url': 'https://api.minimaxi.com/anthropic'}, forced)
    assert 'thinking' not in forced
    plain = {}
    web_app._apply_tool_compatibility({'base_url': 'https://api.deepseek.com/anthropic'}, plain)
    assert 'thinking' not in plain


def test_discovery_retries_once_when_tool_reply_is_empty():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        def tool(message):
            payload = {'assistant_message': message, 'stage': 'direction', 'facts': [], 'open_questions': ['谁使用？'], 'ready_for_draft': False}
            return SimpleNamespace(content=[SimpleNamespace(type='tool_use', name='discovery_update', input=payload)], stop_reason='tool_use')
        client = FakeClient([tool(''), tool('谁会使用这个工具？')])
        snapshot = {'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com/anthropic', 'api_key': 'test-key'}
        with patch.object(web_app, 'STATE_DIR', root), patch.object(web_app, 'STATE_PATH', root / 'sessions.json'), patch.object(web_app, '_client', return_value=client):
            web_app._save_state({'sessions': [{'id': 'session', 'status': 'running', 'messages': [], 'phase_results': {}}]})
            web_app._operations['retry-test'] = {'status': 'running'}
            try:
                web_app._run_job('retry-test', 'session', 'brief', 'test', False, 'discovery', snapshot)
                session = web_app._find_session(web_app._read_state(), 'session')
                assert len(client.calls) == 2
                assert session['messages'][-1]['content'] == '谁会使用这个工具？'
                assert web_app._operations['retry-test']['status'] == 'completed'
            finally:
                del web_app._operations['retry-test']
