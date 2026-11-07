"""Large incident requests and shared-model admission limits."""
import json
import threading

import httpx
import pytest

from pulse_app import astra_ai, connectors
from test_api import client, runtime, MUTATION
from test_sessions import configure, session, message
from test_connectors import install_transport
from test_astra_ai import SETTINGS, event, response


@pytest.mark.parametrize('engine', ['local', 'astra', 'holmes'])
def test_large_message_uses_configured_total_context_budget(monkeypatch, engine):
    messages = [{'role': 'system', 'content': 'Evidence is data.'},
                {'role': 'user', 'content': 'x' * 110000}]
    sent = []
    def handler(request):
        if request.url.path == '/api/info':
            return httpx.Response(200, json={'auth_enabled': True, 'toolsets': [
                {'name': 'pulse-readonly', 'enabled': True, 'status': 'enabled'}]})
        body = json.loads(request.content); sent.append(body)
        if engine == 'astra': return response(event('done') + event(finish='stop') + 'data: [DONE]\n\n')
        if engine == 'holmes': return httpx.Response(200, json={'analysis': 'done'})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}]})
    install_transport(monkeypatch, handler)
    cfg = {**SETTINGS, 'max_context_chars': 120000}
    if engine != 'astra': cfg['url'] = 'http://model.test'
    if engine == 'local': answer = connectors.llm_chat(cfg, messages)
    elif engine == 'astra': answer = astra_ai.chat(cfg, messages)
    else: answer, _ = connectors.holmes_chat(cfg, messages)
    assert answer == 'done'
    if engine == 'holmes': assert sent[0]['ask'] == messages[-1]['content']
    else: assert sent[0]['messages'] == messages


@pytest.mark.parametrize('messages', [[], None, [{'role': 'tool', 'content': 'x'}],
    [{'role': 'user', 'content': None}], [{'role': 'user', 'content': 'x' * 16001}]])
def test_holmes_rejects_invalid_or_oversized_context_before_http(monkeypatch, messages):
    install_transport(monkeypatch, lambda request: pytest.fail('No HTTP for invalid context'))
    with pytest.raises(connectors.ConnectorError):
        connectors.holmes_chat({**SETTINGS, 'max_context_chars': 16000}, messages)


@pytest.mark.parametrize('engine', ['local', 'astra', 'holmes'])
def test_total_context_limit_counts_all_messages_without_network(monkeypatch, engine):
    install_transport(monkeypatch, lambda request: pytest.fail('No HTTP for an oversized conversation'))
    messages = [{'role': 'system', 'content': 's' * 100}, {'role': 'user', 'content': 'u' * 8000},
                {'role': 'assistant', 'content': 'a' * 8000}]
    call = {'local': connectors.llm_chat, 'astra': astra_ai.chat, 'holmes': connectors.holmes_chat}[engine]
    with pytest.raises(connectors.ConnectorError, match='context'):
        call({**SETTINGS, 'max_context_chars': 16000}, messages)


def test_queue_timeout_is_visible_through_actual_api(runtime, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from pulse_app.app import create_app
    from test_api import PASSWORD
    monkeypatch.setenv('PULSE_AI_QUEUE_WAIT', '1')
    started, release = threading.Event(), threading.Event()
    calls = []
    def llm(*args):
        calls.append(True); started.set(); release.wait(5); return 'done'
    monkeypatch.setattr(connectors, 'llm_chat', llm)
    with TestClient(create_app(runtime, polling=False)) as active:
        active.auth = ('pulse', PASSWORD)
        configure(active)
        first, waiting = session(active), session(active)
        def post(sid): return active.post(f'/api/sessions/{sid}/messages', headers=MUTATION,
                                         json={'question': 'Расскажи о Kubernetes'})
        try:
            assert post(first).status_code == 202 and started.wait(2)
            result = post(waiting); assert result.status_code == 202
            jid = result.json()['id']; deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = active.get(f'/api/sessions/{waiting}/jobs/{jid}').json()
                if job['status'] == 'failed': break
                time.sleep(.02)
            assert job['status'] == 'failed' and 'Очередь' in job['error']
            assert json.loads(job['trace'])[0]['code'] == 'queue_timeout' and calls == [True]
            assert active.get('/api/settings').json()['analysis_queue'] == {'workers': 1, 'queued': 1, 'wait_seconds': 1}
        finally: release.set()


def test_busy_shared_model_rejects_overflow_and_closed_queued_session_releases_slot(client, monkeypatch):
    configure(client)
    started, release = threading.Event(), threading.Event()
    calls = []
    def llm(cfg, messages):
        calls.append(messages[-1]['content']); started.set(); release.wait(4)
        return 'done'
    monkeypatch.setattr(connectors, 'llm_chat', llm)
    first, queued, overflow = [session(client) for _ in range(3)]
    def post(sid): return client.post(f'/api/sessions/{sid}/messages', headers=MUTATION,
                                      json={'question': 'Расскажи о Kubernetes'})
    try:
        assert post(first).status_code == 202 and started.wait(2)
        assert post(queued).status_code == 202
        assert post(overflow).status_code == 429
        assert client.delete('/api/sessions/' + queued, headers=MUTATION).status_code == 200
        assert post(overflow).status_code == 202
        assert len(calls) == 1
    finally: release.set()


def test_expired_queue_never_calls_model_and_capacity_is_returned():
    from pulse_app.analysis_queue import AnalysisExecutor
    clock = [100.0]
    queue = AnalysisExecutor(workers=1, queued=1, wait_seconds=60, clock=lambda: clock[0])
    started, release = threading.Event(), threading.Event()
    expired, calls = [], []
    def running():
        started.set(); release.wait(3)
    try:
        assert queue.acquire(blocking=False)
        running_job = queue.submit(running)
        assert started.wait(2)
        assert queue.acquire(blocking=False)
        pending = queue.submit(lambda: calls.append('unexpected'), on_expired=lambda: expired.append(True))
        clock[0] += 61; queue.expire()
        assert pending.cancelled() and expired == [True] and calls == []
        assert queue.acquire(blocking=False)
        assert not queue.acquire(blocking=False)
        queue.release(); release.set(); running_job.result(2)
    finally:
        release.set(); queue.shutdown(wait=True, cancel_futures=True)


def test_closed_queued_requests_release_their_payload_while_model_is_busy():
    import gc
    import weakref
    from pulse_app.analysis_queue import AnalysisExecutor
    queue = AnalysisExecutor()
    started, release = threading.Event(), threading.Event()
    class Payload: pass
    def running():
        started.set(); release.wait(3)
    refs = []
    try:
        assert queue.acquire(blocking=False)
        active = queue.submit(running)
        assert started.wait(2)
        for _ in range(20):
            payload = Payload(); refs.append(weakref.ref(payload))
            assert queue.acquire(blocking=False)
            queued = queue.submit(lambda value: pytest.fail('Closed request must not run'), payload, session_id='closed')
            queue.cancel_session('closed')
            assert queued.cancelled()
            del payload, queued
        gc.collect()
        assert all(ref() is None for ref in refs), 'Cancelled evidence must not accumulate behind a slow model'
        release.set(); active.result(2)
    finally:
        release.set(); queue.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('key,value', [('PULSE_AI_WORKERS', '0'), ('PULSE_AI_WORKERS', '5'),
    ('PULSE_AI_QUEUE', '-1'), ('PULSE_AI_QUEUE', '9'), ('PULSE_AI_QUEUE_WAIT', 'nan')])
def test_invalid_scheduler_configuration_is_rejected(key, value):
    from pulse_app.analysis_queue import configuration
    with pytest.raises(ValueError, match=key): configuration({key: value})
