"""API workflow/security checks; upstreams replaced by deterministic fixtures."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pulse_app.app import create_app
from pulse_app import connectors
from pulse_app.store import Store

PASSWORD = 'test-only-password-0123456789'
TOKEN = 'kube-secret-token-not-for-browser'
MUTATION = {'X-Pulse-Request': '1'}


def kubeconfig(token=TOKEN):
    return json.dumps({
        'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'test',
        'contexts': [{'name': 'test', 'context': {'cluster': 'test', 'user': 'reader'}}],
        'clusters': [{'name': 'test', 'cluster': {'server': 'https://kubernetes.example.test'}}],
        'users': [{'name': 'reader', 'user': {'token': token}}],
    })


def add(client, cid='one', **extra):
    data = {'id': cid, 'name': cid.title(), 'kubeconfig': kubeconfig(), **extra}
    response = client.post('/api/clusters', json=data, headers=MUTATION)
    assert response.status_code == 201, response.text
    return response.json()


def fresh_snapshot():
    return {'observed_at': datetime.now(timezone.utc).isoformat(), 'errors': [], 'complete': True,
            'resources': {kind: {'items': [{'metadata': {'name': 'test', 'namespace': 'default'}}],
                                 'error': None, 'truncated': False}
                          for kind in ('nodes', 'pods', 'namespaces')}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('PULSE_PASSWORD_FILE', raising=False)
    monkeypatch.delenv('PULSE_USERNAME', raising=False)
    (tmp_path / 'secrets').mkdir()
    (tmp_path / 'secrets/portal-password').write_text(PASSWORD)
    return tmp_path


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime, polling=False)) as c:
        c.auth = ('pulse', PASSWORD)
        yield c


def test_health_is_public_but_api_requires_auth(client):
    assert client.get('/healthz', auth=None).json()['status'] == 'ok'
    response = client.get('/api/clusters', auth=None)
    assert response.status_code == 401
    assert response.headers['www-authenticate'].startswith('Basic')
    assert client.get('/api/clusters', auth=('pulse', 'wrong')).status_code == 401


def test_missing_password_refuses_api(tmp_path, monkeypatch):
    monkeypatch.delenv('PULSE_PASSWORD_FILE', raising=False)
    with TestClient(create_app(tmp_path, polling=False)) as c:
        assert c.get('/api/clusters').status_code == 503


def test_weak_password_refuses_api(runtime):
    (runtime / 'secrets/portal-password').write_text('short')
    with TestClient(create_app(runtime, polling=False)) as c:
        assert c.get('/api/clusters', auth=('pulse', 'short')).status_code == 503


def test_mutations_require_header_and_same_origin(client):
    payload = {'llm': {'url': 'http://localhost:8002/v1', 'model': 'test'}}
    assert client.put('/api/settings', json=payload).status_code == 403
    assert client.put('/api/settings', json=payload, headers={**MUTATION, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.put('/api/settings', json=payload, headers={**MUTATION, 'Origin': 'http://testserver'}).status_code == 200


def test_security_headers_and_static_whitelist(client):
    response = client.get('/api/clusters')
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'no-store'
    assert "object-src 'none'" in response.headers['content-security-policy']
    assert client.get('/static/registry.key').status_code == 404


def test_cluster_crud_and_encrypted_credentials(client, runtime):
    secret = 'metrics-secret-not-for-browser'
    created = add(client, metrics={'url': 'https://metrics.example.test', 'token': secret})
    assert created['has_metrics']
    assert TOKEN not in json.dumps(created)
    assert secret not in client.get('/api/clusters').text
    with sqlite3.connect(runtime / 'pulse.sqlite3') as c:
        value = c.execute('SELECT config FROM clusters').fetchone()[0]
    assert TOKEN not in value and secret not in value
    update = client.put('/api/clusters/one', json={'name': 'Renamed', 'enabled': False}, headers=MUTATION)
    assert update.status_code == 200
    assert update.json()['name'] == 'Renamed'
    assert update.json()['enabled'] is False
    assert client.delete('/api/clusters/one', headers=MUTATION).status_code == 200
    assert client.get('/api/clusters').json() == {'items': []}
    assert client.get('/api/clusters/one/snapshot').status_code == 404


def test_duplicate_and_invalid_cluster_id(client):
    add(client)
    assert client.post('/api/clusters', json={'id': 'one', 'name': 'Duplicate', 'kubeconfig': kubeconfig()}, headers=MUTATION).status_code == 409
    assert client.post('/api/clusters', json={'id': '../bad', 'name': 'Bad', 'kubeconfig': kubeconfig()}, headers=MUTATION).status_code == 422


def test_invalid_or_executable_kubeconfig_rejected(client):
    for config in ('not yaml: [', '{}'):
        assert client.post('/api/clusters', json={'id': 'bad', 'name': 'Bad', 'kubeconfig': config}, headers=MUTATION).status_code == 422
    config = json.loads(kubeconfig())
    config['users'][0]['user']['exec'] = {'command': 'malicious-command'}
    assert client.post('/api/clusters', json={'id': 'bad', 'name': 'Bad', 'kubeconfig': json.dumps(config)}, headers=MUTATION).status_code == 422


def test_snapshot_lifecycle_and_paused_refresh(client, monkeypatch):
    add(client)
    assert client.get('/api/overview').json()['clusters'][0]['status'] == 'waiting'
    monkeypatch.setattr(connectors, 'collect_cluster', lambda config: fresh_snapshot())
    response = client.post('/api/clusters/one/refresh', headers=MUTATION)
    assert response.status_code == 200
    overview = client.get('/api/overview').json()['clusters'][0]
    assert overview['status'] == 'ok'
    assert overview['counts'] == {'nodes': 1, 'pods': 1, 'namespaces': 1}
    client.put('/api/clusters/one', json={'enabled': False}, headers=MUTATION)
    assert client.post('/api/clusters/one/refresh', headers=MUTATION).status_code == 409
    assert client.get('/api/overview').json()['clusters'][0]['status'] == 'paused'


def test_stale_snapshot_is_never_healthy(client, monkeypatch):
    add(client)
    snap = fresh_snapshot()
    snap['observed_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(connectors, 'collect_cluster', lambda config: snap)
    client.post('/api/clusters/one/refresh', headers=MUTATION)
    assert client.get('/api/overview').json()['clusters'][0]['status'] == 'stale'


def test_failed_refresh_preserves_last_observation_and_redacts_error(client, monkeypatch):
    add(client)
    snap = fresh_snapshot()
    monkeypatch.setattr(connectors, 'collect_cluster', lambda config: snap)
    client.post('/api/clusters/one/refresh', headers=MUTATION)
    def fail(config):
        raise RuntimeError('sensitive transport output ' + TOKEN)
    monkeypatch.setattr(connectors, 'collect_cluster', fail)
    response = client.post('/api/clusters/one/refresh', headers=MUTATION)
    assert response.status_code == 200
    assert response.json()['resources'] == snap['resources']
    assert response.json()['observed_at'] == snap['observed_at']
    assert not response.json()['complete']
    assert TOKEN not in response.text
    assert client.get('/api/overview').json()['clusters'][0]['status'] == 'degraded'


def test_llm_secret_settings_round_trip_without_exposure(client, runtime):
    secret = 'model-key-do-not-display'
    response = client.put('/api/settings', json={'llm': {'url':'http://127.0.0.1:8002/v1', 'model':'local', 'api_key':secret}}, headers=MUTATION)
    assert response.status_code == 200 and response.json()['llm']['has_key']
    assert secret not in response.text
    response = client.put('/api/settings', json={'llm': {'url':'http://127.0.0.1:8002/v1', 'model':'local', 'api_key':''}}, headers=MUTATION)
    assert response.json()['llm']['has_key']
    with sqlite3.connect(runtime / 'pulse.sqlite3') as c:
        assert secret not in c.execute('SELECT value FROM settings').fetchone()[0]


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://user:pass@example.test', 'http://example.test?token=secret'])
def test_invalid_upstream_urls_rejected(client, url):
    assert client.put('/api/settings', json={'llm': {'url':url, 'model':'x'}}, headers=MUTATION).status_code == 422


def test_llm_probe_uses_real_connector_contract(client, monkeypatch):
    def fake(settings, messages):
        assert messages[0]['role'] == 'user'
        return 'Модель отвечает'
    monkeypatch.setattr(connectors, 'llm_chat', fake)
    response = client.post('/api/settings/test', headers=MUTATION)
    assert response.json() == {'ok':True, 'answer':'Модель отвечает'}


def upload(client, text, cid='', name='guide.md'):
    response = client.post('/api/documents', data={'cluster_id':cid}, files={'file':(name,text.encode(),'text/markdown')}, headers=MUTATION)
    assert response.status_code == 201, response.text
    return response.json()


def test_document_search_scope_global_and_delete(client):
    add(client, 'one')
    add(client, 'two')
    common = upload(client, 'Проверка prometheus общая инструкция')
    own = upload(client, 'prometheus инструкция первого кластера', 'one')
    other = upload(client, 'prometheus инструкция второго кластера', 'two')
    result = client.get('/api/documents/search', params={'q':'prometheus','cluster_id':'one'}).json()['items']
    assert {row['id'] for row in result} == {common['id'], own['id']}
    assert other['id'] not in {r['id'] for r in client.get('/api/documents', params={'cluster_id':'one'}).json()['items']}
    assert client.delete('/api/documents/' + own['id'], headers=MUTATION).status_code == 200
    result = client.get('/api/documents/search', params={'q':'prometheus','cluster_id':'one'}).json()['items']
    assert {row['id'] for row in result} == {common['id']}
    assert client.get('/api/documents/search', params={'q':'x','cluster_id':'missing'}).status_code == 404


def test_document_malformed_and_unsupported_uploads(client):
    for name, content, code in [('bad.pdf', b'not a pdf', 422), ('bad.docx', b'not a zip', 422), ('bad.exe', b'bytes', 415), ('empty.md', b'  ', 422), ('bad.txt', b'\xff\xfe', 422)]:
        response = client.post('/api/documents', files={'file':(name,content)}, headers=MUTATION)
        assert response.status_code == code, response.text
    assert client.get('/api/documents').json()['items'] == []


def test_search_metacharacters_do_not_break_fts(client):
    upload(client, 'pod readiness service')
    for query in ('" OR *', '"; DROP TABLE documents;--', '😀', 'readiness AND NEAR(foo)'):
        assert client.get('/api/documents/search', params={'q':query}).status_code == 200
    assert len(client.get('/api/documents').json()['items']) == 1


def test_delete_cluster_removes_only_its_documents_and_history(client):
    add(client, 'one')
    add(client, 'two')
    upload(client, 'distinctivephrase one', 'one')
    common = upload(client, 'distinctivephrase global')
    other = upload(client, 'distinctivephrase two', 'two')
    assert client.delete('/api/clusters/one', headers=MUTATION).status_code == 200
    assert {r['id'] for r in client.get('/api/documents/search', params={'q':'distinctivephrase','cluster_id':'two'}).json()['items']} == {common['id'],other['id']}


def configure_llm(client):
    return client.put('/api/settings', json={'llm':{'url':'http://127.0.0.1:8002/v1','model':'test'}}, headers=MUTATION)


def wait_job(client, jid):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        job = client.get('/api/investigations/' + jid).json()
        if job['status'] not in ('queued','running'):
            return job
        time.sleep(0.01)
    pytest.fail('Mocked investigation did not complete in 3 seconds')


def test_investigation_and_verified_incident_workflow(client, monkeypatch):
    add(client)
    configure_llm(client)
    monkeypatch.setattr(connectors, 'collect_cluster', lambda config: fresh_snapshot())
    client.post('/api/clusters/one/refresh', headers=MUTATION)
    upload(client, 'readiness failed: проверьте endpoint проверки', 'one')
    def model(settings, messages):
        assert 'readiness' in messages[1]['content']
        assert 'Кластер: one' in messages[1]['content']
        return 'Гипотеза: readiness probe не отвечает. Проверьте endpoint.'
    monkeypatch.setattr(connectors, 'llm_chat', model)
    response = client.post('/api/investigations', json={'cluster_id':'one','question':'Почему readiness failed?'}, headers=MUTATION)
    assert response.status_code == 202
    jid = response.json()['id']
    job = wait_job(client, jid)
    assert job['status'] == 'done' and 'Гипотеза' in job['answer']
    assert client.post('/api/investigations/' + jid + '/resolve', json={'solution':'Исправлена настройка endpoint. Проверка readiness прошла.'}, headers=MUTATION).status_code == 200
    assert client.get('/api/incidents', params={'cluster_id':'one'}).json()['items'][0]['id'] == jid


def test_investigation_requires_model_and_valid_cluster(client):
    add(client)
    assert client.post('/api/investigations', json={'cluster_id':'one','question':'Почему ошибка?'}, headers=MUTATION).status_code == 409
    assert client.post('/api/investigations', json={'cluster_id':'missing','question':'Почему ошибка?'}, headers=MUTATION).status_code == 404


def test_failed_investigation_does_not_leak_internal_exception(client, monkeypatch):
    add(client)
    configure_llm(client)
    def fail(*args):
        raise RuntimeError('token=' + TOKEN)
    monkeypatch.setattr(connectors, 'llm_chat', fail)
    response = client.post('/api/investigations', json={'cluster_id':'one','question':'Почему ошибка?'}, headers=MUTATION)
    job = wait_job(client, response.json()['id'])
    assert job['status'] == 'failed'
    assert TOKEN not in json.dumps(job)
    assert client.post('/api/investigations/' + job['id'] + '/resolve', json={'solution':'Неподтверждённая запись'}, headers=MUTATION).status_code == 409


def test_active_investigation_prevents_cluster_removal(client, monkeypatch):
    add(client)
    configure_llm(client)
    entered, release = threading.Event(), threading.Event()
    def model(*args):
        entered.set()
        assert release.wait(3)
        return 'Ответ'
    monkeypatch.setattr(connectors, 'llm_chat', model)
    try:
        response = client.post('/api/investigations', json={'cluster_id':'one','question':'Почему ошибка?'}, headers=MUTATION)
        assert response.status_code == 202 and entered.wait(2)
        assert client.delete('/api/clusters/one', headers=MUTATION).status_code == 409
    finally:
        release.set()
    assert wait_job(client, response.json()['id'])['status'] == 'done'


def test_restart_marks_incomplete_jobs_without_replaying(runtime):
    store = Store(runtime)
    store.execute('INSERT INTO investigations(id,cluster_id,question,status,created_at) VALUES (?,?,?,?,?)', ('interrupted-job','one','Question','running',datetime.now(timezone.utc).isoformat()))
    with TestClient(create_app(runtime, polling=False)) as c:
        job = c.get('/api/investigations/interrupted-job', auth=('pulse',PASSWORD)).json()
        assert job['status'] == 'interrupted'
        assert job['answer'] == ''


def test_missing_registry_key_never_regenerates_over_existing_database(runtime):
    Store(runtime)
    (runtime / 'registry.key').unlink()
    with pytest.raises(RuntimeError, match='registry.key missing'):
        Store(runtime)
    assert not (runtime / 'registry.key').exists()


def test_runtime_prevents_second_process(client, runtime):
    with pytest.raises(RuntimeError, match='Only one Pulse process'):
        with TestClient(create_app(runtime, polling=False)):
            pass


def test_malformed_ipv6_upstream_url_is_validation_error(client):
    response = client.put('/api/settings', json={'llm': {'url': 'http://[invalid', 'model': 'test'}}, headers=MUTATION)
    assert response.status_code == 422


def test_investigation_queue_is_bounded(client, monkeypatch):
    add(client)
    configure_llm(client)
    release = threading.Event()
    def model(*args):
        assert release.wait(5)
        return 'Готово'
    monkeypatch.setattr(connectors, 'llm_chat', model)
    submitted = []
    try:
        assert client.get('/api/settings').json()['analysis_queue']=={'workers':1,'queued':1,'wait_seconds':60}
        for _ in range(2):
            response = client.post('/api/investigations', json={'cluster_id':'one','question':'Проверьте состояние'}, headers=MUTATION)
            assert response.status_code == 202
            submitted.append(response.json()['id'])
        assert client.post('/api/investigations', json={'cluster_id':'one','question':'Лишний запрос'}, headers=MUTATION).status_code == 429
    finally:
        release.set()
    for jid in submitted:
        assert wait_job(client, jid)['status'] == 'done'
    assert client.post('/api/investigations', json={'cluster_id':'one','question':'Место освободилось'}, headers=MUTATION).status_code == 202


@pytest.mark.parametrize('value', [
    {'url':'http://localhost:65536'}, {'url':'http://localhost:abc'},
    {'url':'http://localhost:0'}, {'url':'http://user@localhost'},
    {'url':'http://localhost\n.evil.example'}, {'model':{}},
    {'api_key':['do-not-echo-this-secret']}, {'token': 'secret\nheader'},
    {'ca_pem':{}}, {'username':None}, {'labels':[]},
    {'labels':{'cluster':'one','bad label':'value'}}, {'labels':{'cluster':42}},
    {'mode':False}, {'cluster_field':'bad field'},
])
def test_source_field_shapes_are_validated_without_echoing_input(client, value):
    response = client.put('/api/settings', json={'llm':value}, headers=MUTATION)
    assert response.status_code == 422
    assert 'do-not-echo-this-secret' not in response.text
    assert 'secret\\nheader' not in response.text


def test_pydantic_validation_does_not_echo_kubeconfig_or_credentials(client):
    response = client.post('/api/clusters', json={'id':'bad id','name':'Test','kubeconfig':kubeconfig()}, headers=MUTATION)
    assert response.status_code == 422
    assert TOKEN not in response.text
    response = client.put('/api/settings', json={'llm':'do-not-echo-this-secret'}, headers=MUTATION)
    assert response.status_code == 422
    assert 'do-not-echo-this-secret' not in response.text


def test_model_endpoint_change_does_not_forward_old_credentials(client, runtime):
    client.put('/api/settings', json={'llm': {'url':'https://model-a.test/v1','model':'one','api_key':'private-key'}}, headers=MUTATION)
    response = client.put('/api/settings', json={'llm': {'url':'https://model-b.test/v1','model':'two'}}, headers=MUTATION)
    assert response.status_code == 200
    assert not response.json()['llm']['has_key']


def test_explicit_credential_clear_on_same_endpoint(client):
    client.put('/api/settings', json={'llm': {'url':'https://model.test/v1','model':'one','api_key':'private-key'}}, headers=MUTATION)
    response = client.put('/api/settings', json={'llm': {'clear_credentials': True}}, headers=MUTATION)
    assert response.status_code == 200
    assert not response.json()['llm']['has_key']
    assert response.json()['llm']['model'] == 'one'


def test_metrics_endpoint_change_clears_old_auth_and_ca(client, runtime, monkeypatch):
    add(client, metrics={'url':'https://a.test','token':'private-token','username':'user','password':'private-password','ca_pem':'old CA','labels':{'cluster':'one'}})
    response = client.put('/api/clusters/one', json={'metrics':{'url':'https://b.test'}}, headers=MUTATION)
    assert response.status_code == 200
    captured = []
    def query(settings, metric):
        captured.append(settings)
        return {'data':{'result':[]}}
    monkeypatch.setattr(connectors, 'query_metrics', query)
    assert client.get('/api/clusters/one/metrics').status_code == 200
    assert captured
    assert all(not any(k in cfg for k in ('token','username','password','ca_pem')) for cfg in captured)
    assert captured[0]['labels'] == {'cluster':'one'}
