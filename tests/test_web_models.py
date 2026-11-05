import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import web_app


def test_legacy_migration_preserves_existing_sessions():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = root / '.env'
        env.write_text('ANTHROPIC_API_KEY=example-key-123456\nMODEL_ID=MiniMax-M3\nANTHROPIC_BASE_URL=https://example.com/anthropic\n')
        with patch.object(web_app, 'STATE_DIR', root), patch.object(web_app, 'STATE_PATH', root / 'sessions.json'), patch.object(web_app, 'MODELS_PATH', root / 'models.json'), patch.object(web_app, 'ENV_PATH', env), patch.dict(os.environ, {}, clear=True):
            web_app._save_state({'sessions': [{'id': 'old'}]})
            data = web_app._read_models()
            selection = web_app._read_state()['sessions'][0]['model_selection']
            assert web_app._resolve_model(selection, data)['model'] == 'MiniMax-M3'
            assert 'example-key-123456' not in json.dumps(web_app._public_models(data))
            assert (root / 'models.json').stat().st_mode & 0o077 == 0


def test_project_selection_and_running_job_snapshot():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        started, release = threading.Event(), threading.Event()
        used = []
        class Client:
            messages = None
            def __init__(self): self.messages = self
            def create(self, **kwargs):
                used.append(kwargs['model'])
                started.set()
                assert release.wait(5)
                return SimpleNamespace(content=[SimpleNamespace(type='text', text='架构方案')], stop_reason='end_turn')
        patches = [patch.object(web_app, 'STATE_DIR', root), patch.object(web_app, 'STATE_PATH', root / 'sessions.json'), patch.object(web_app, 'MODELS_PATH', root / 'models.json'), patch.object(web_app, 'OUTPUT_DIR', root / 'generated'), patch.object(web_app, '_client', side_effect=lambda snapshot: Client())]
        for p in patches: p.start()
        server = web_app.ThreadingHTTPServer(('127.0.0.1', 0), web_app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        def call(path, data=None):
            request = urllib.request.Request(url + path, data=None if data is None else json.dumps(data).encode(), headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request, timeout=5) as response: return json.load(response)
        try:
            web_app._save_models({'providers': []})
            result = call('/api/models', {'name':'MiniMax', 'api_key':'example-key-123456', 'base_url':'https://example.com', 'models':['MiniMax-M3','MiniMax-M2.7']})
            assert 'example-key-123456' not in json.dumps(result)
            provider = result['providers'][0]['id']
            first = {'provider_id': provider, 'model':'MiniMax-M3'}
            second = {'provider_id': provider, 'model':'MiniMax-M2.7'}
            a = call('/api/sessions', {'brief':'test', 'model_selection':first})['session']
            b = call('/api/sessions', {'brief':'other', 'model_selection':first})['session']
            state = web_app._read_state()
            item = web_app._find_session(state, a['id'])
            item['approvals']['components'] = True
            web_app._save_state(state)
            job = call('/api/run', {'session_id':a['id'], 'phase':'architecture'})
            assert started.wait(5)
            call(f"/api/sessions/{a['id']}/model", {'selection': second})
            release.set()
            for _ in range(100):
                result = call('/api/jobs/' + job['job_id'])
                if result['status'] != 'running': break
                time.sleep(.05)
            assert result['status'] == 'completed'
            assert used == ['MiniMax-M3']
            assert call('/api/sessions/' + a['id'])['session']['model_selection'] == second
            assert call('/api/sessions/' + b['id'])['session']['model_selection'] == first
            call(f'/api/models/{provider}/delete', {})
            try:
                call('/api/run', {'session_id':a['id'], 'phase':'architecture'})
                assert False, 'deleted model must be rejected'
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()
            for p in reversed(patches): p.stop()


def test_connection_test_uses_unsaved_form_without_saving():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        snapshots = []
        calls = []
        class Client:
            def __init__(self): self.messages = self
            def with_options(self, **kwargs): return self
            def create(self, **kwargs): calls.append(kwargs)
        def client(snapshot):
            snapshots.append(snapshot.copy())
            return Client()
        patches = [patch.object(web_app, 'STATE_DIR', root), patch.object(web_app, 'STATE_PATH', root / 'sessions.json'), patch.object(web_app, 'MODELS_PATH', root / 'models.json'), patch.object(web_app, '_client', side_effect=client)]
        for p in patches: p.start()
        server = web_app.ThreadingHTTPServer(('127.0.0.1', 0), web_app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/models/test'
        def test_request(data):
            request = urllib.request.Request(url, data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=5) as response: return json.load(response)
        try:
            saved = {'providers': [{'id': 'existing', 'name': 'Provider', 'api_key': 'saved-secret-key', 'base_url': 'https://old.example', 'models': ['old-model']}]}
            web_app._save_models(saved)
            assert test_request({'id': 'existing', 'api_key': '', 'base_url': 'https://new.example', 'model': 'new-model'}) == {'ok': True}
            assert snapshots[-1] == {'api_key': 'saved-secret-key', 'base_url': 'https://new.example', 'model': 'new-model'}
            assert test_request({'id': '', 'api_key': 'new-secret-key', 'base_url': 'https://new.example', 'model': 'other-model'}) == {'ok': True}
            assert snapshots[-1]['api_key'] == 'new-secret-key'
            assert calls[-1]['model'] == 'other-model'
            assert web_app._read_models() == saved
            try:
                test_request({'id': '', 'api_key': '', 'base_url': '', 'model': 'other-model'})
                assert False, 'missing key must be rejected'
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            for p in reversed(patches): p.stop()
