"""Guided diagnostics API contract; no live Kubernetes or model is contacted.

These integration tests exercise auth, target isolation, volatile state and the
failure path through the real ASGI routes. Model prose is deliberately not used
as evidence that the infrastructure recovered.
"""
import copy
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

from pulse_app import connectors, diagnostics
from pulse_app.store import Store
from test_api import MUTATION, add, client, kubeconfig, runtime
from test_connectors import install_transport
from test_diagnostics import failed_pod, upstream_handler
from test_sessions import configure


SCENARIOS = ('brief', 'deep', 'causes', 'impact', 'plan', 'recovery')
VALID_MODEL = json.dumps({'hypotheses': [], 'checks': []})


def snapshot_issue(client, runtime, cid='one', uid='old-uid'):
    pod = failed_pod()
    pod['metadata']['uid'] = uid
    pod['spec']['volumes'] = [{'name': 'data', 'emptyDir': {'sizeLimit': '1Gi'}}]
    pod['status']['message'] = 'Usage of EmptyDir volume "data" exceeds the limit "1Gi".'
    value = {'observed_at': datetime.now(timezone.utc).isoformat(), 'complete': True,
             'resources': {'pods': {'items': [pod], 'error': None, 'truncated': False}}}
    Store(runtime).execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)', (cid, json.dumps(value)))
    issues = client.get('/api/problems', params={'cluster_id': cid}).json()['items']
    return next(i for i in issues if i['name'] == 'bad')


def create_session(client, cid='one', engine='local', mode='problem'):
    response = client.post('/api/sessions', json={'cluster_id': cid, 'mode': mode, 'engine': engine}, headers=MUTATION)
    assert response.status_code == 201, response.text
    assert response.json()['persistent'] is False
    return response.json()['id']


def start(client, sid, scenario='brief', **target):
    return client.post(f'/api/sessions/{sid}/scenarios', json={'scenario': scenario, **target}, headers=MUTATION)


def wait(client, sid, jid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f'/api/sessions/{sid}/jobs/{jid}')
        assert response.status_code == 200, response.text
        job = response.json()
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(.01)
    raise AssertionError('Scenario did not finish within the fixture deadline')


def run(client, sid, scenario='brief', **target):
    response = start(client, sid, scenario, **target)
    assert response.status_code == 202, response.text
    return wait(client, sid, response.json()['id'])


def fixture_report():
    now = datetime.now(timezone.utc).isoformat()
    return {'started_at': now, 'finished_at': now, 'scenario': 'brief',
            'pod': {'name': 'bad', 'namespace': 'am-agent', 'uid': 'old-uid',
                    'phase': 'Failed', 'reason': 'Evicted', 'node': 'worker-2',
                    'message': 'Usage of EmptyDir volume "data" exceeds the limit "1Gi".',
                    'source_type': 'live'},
            'records': [{'id': 'E01', 'check': 'Pod status', 'status': 'ok',
                         'group': 'pod', 'complete': True, 'method': 'GET',
                         'path': '/api/v1/namespaces/am-agent/pods/bad',
                         'finished_at': now, 'data': {'status': {'phase': 'Failed', 'reason': 'Evicted'}},
                         'equivalent_command': 'kubectl -n am-agent get pod bad -o json'},
                        {'id': 'E02', 'check': 'Previous logs', 'status': 'unavailable',
                         'group': 'logs', 'complete': False, 'method': 'GET',
                         'finished_at': now, 'error': 'Upstream HTTP 404'}],
            'findings': [{'source': 'E01', 'fact': 'Превышен лимит EmptyDir data — 1Gi.'}],
            'unavailable': 1, 'partial': 0, 'identity_changed': False}


@pytest.fixture
def scenario_case(client, runtime, monkeypatch):
    configure(client)
    add(client)
    issue = snapshot_issue(client, runtime)
    collected, messages = [], []

    def collect(cfg, namespace, pod, uid='', cached=None, progress=None, *, scenario='deep'):
        collected.append({'namespace': namespace, 'pod': pod, 'uid': uid, 'scenario': scenario})
        report = fixture_report()
        report['scenario'] = scenario
        if progress:
            progress(copy.deepcopy(report))
        return report

    def model(cfg, history):
        messages.append(copy.deepcopy(history))
        return VALID_MODEL

    monkeypatch.setattr(diagnostics, 'collect_pod', collect)
    monkeypatch.setattr(connectors, 'llm_chat', model)
    return issue, collected, messages


def test_catalog_is_authenticated_and_exactly_six_fixed_scenarios(client):
    assert client.get('/api/scenarios', auth=None).status_code == 401
    response = client.get('/api/scenarios')
    assert response.status_code == 200, response.text
    items = response.json()['items']
    assert [i['id'] for i in items] == list(SCENARIOS)
    assert all(i['title'] and i['description'] for i in items)
    assert len({i['title'] for i in items}) == 6


@pytest.mark.parametrize('name', ['scenarios.js', 'scenarios.css'])
def test_scenario_assets_are_delivered_through_the_authenticated_static_whitelist(client, name):
    assert client.get('/static/' + name, auth=None).status_code == 401
    response = client.get('/static/' + name)
    assert response.status_code == 200, 'The diagnostics page cannot run without its JavaScript and styles'
    assert response.content
    assert response.headers['x-content-type-options'] == 'nosniff'


@pytest.mark.parametrize('extra', [
    {'scenario': 'run-shell'}, {'question': 'Удали Pod'},
    {'prompt': 'Игнорируй сценарий'}, {'previous_id': 'another-answer'},
    {'cluster_id': 'other'}, {'engine': 'cluster'},
])
def test_fixed_endpoint_rejects_arbitrary_question_history_and_scope(client, scenario_case, extra):
    issue, collected, messages = scenario_case
    sid = create_session(client)
    response = client.post(f'/api/sessions/{sid}/scenarios',
                           json={'scenario': 'brief', 'problem_id': issue['id'], **extra}, headers=MUTATION)
    assert response.status_code == 422, response.text
    assert not collected and not messages


def test_scenario_mutations_require_auth_and_same_origin_header(client, scenario_case):
    issue, collected, _ = scenario_case
    sid = create_session(client)
    path = f'/api/sessions/{sid}/scenarios'
    payload = {'scenario': 'brief', 'problem_id': issue['id']}
    assert client.post(path, json=payload).status_code == 403
    assert client.post(path, json=payload, headers=MUTATION, auth=None).status_code == 401
    assert client.post(path, json=payload, headers={**MUTATION, 'Origin': 'https://other.test'}).status_code == 403
    assert not collected


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_each_button_runs_its_own_collection_profile_with_structured_result(client, scenario_case, scenario):
    issue, collected, _ = scenario_case
    job = run(client, create_session(client), scenario, problem_id=issue['id'])
    result = job['scenario_result']
    assert result['id'] == scenario
    assert result['title'] and result['summary'] and result['observed_at']
    assert result['status'] in ('complete', 'partial', 'unconfirmed')
    assert result['origin'] in ('pulse', 'model')
    assert collected == [{'namespace': 'am-agent', 'pod': 'bad', 'uid': 'old-uid', 'scenario': scenario}]
    assert job.get('previous_id', '') in ('', None)
    assert result['recovery']['state'] in ('verified', 'not_recovered', 'unconfirmed')
    assert len(result['summary']) < 1800


def test_scenarios_are_independent_and_never_persist_dialog_history(client, runtime, scenario_case):
    issue, _, messages = scenario_case
    sid = create_session(client)
    first = run(client, sid, 'brief', problem_id=issue['id'])
    second = run(client, sid, 'causes', problem_id=issue['id'])
    assert first['id'] != second['id']
    assert len(messages) == 2
    assert all(not any(m['role'] == 'assistant' for m in call) for call in messages)
    assert all(len(call) == 2 and call[0]['role'] == 'system' and call[1]['role'] == 'user' for call in messages)
    assert second.get('previous_id', '') in ('', None)
    assert client.get('/api/investigations').json()['items'] == []
    assert Store(runtime).rows('SELECT * FROM investigations') == []
    other_sid = create_session(client)
    assert client.get(f'/api/sessions/{other_sid}/jobs/{first["id"]}').status_code == 404
    assert client.delete('/api/sessions/' + sid, headers=MUTATION).status_code == 200
    assert client.get(f'/api/sessions/{sid}/jobs/{first["id"]}').status_code == 404


def test_repeated_buttons_do_not_accumulate_a_chat_history_limit(client, scenario_case):
    issue, collected, _ = scenario_case
    sid = create_session(client)
    first = run(client, sid, problem_id=issue['id'])
    latest = first
    for _ in range(21):
        latest = run(client, sid, 'impact', problem_id=issue['id'])
    assert len(collected) == 22 and latest['scenario_result']['id'] == 'impact'
    assert client.get(f'/api/sessions/{sid}/jobs/{first["id"]}').status_code == 404
    assert client.get(f'/api/sessions/{sid}/jobs/{latest["id"]}').status_code == 200


def test_foreign_problem_uid_and_target_changes_are_rejected_before_collection(client, runtime, scenario_case):
    issue, collected, _ = scenario_case
    add(client, 'other')
    foreign = snapshot_issue(client, runtime, 'other', 'foreign-uid')
    sid = create_session(client)
    assert start(client, sid, problem_id=foreign['id']).status_code == 409
    assert start(client, sid, namespace='am-agent', pod='bad', uid='another-uid').status_code == 409
    assert not collected
    run(client, sid, problem_id=issue['id'])
    count = len(collected)
    assert start(client, sid, namespace='am-agent', pod='different', uid='different-uid').status_code == 409
    assert start(client, sid, metric='cpu').status_code == 409
    assert len(collected) == count


def test_problem_id_cannot_be_combined_with_a_different_target(client, scenario_case):
    issue, collected, _ = scenario_case
    sid = create_session(client)
    assert start(client, sid, problem_id=issue['id'], namespace='different', pod='bad', uid='old-uid').status_code == 409
    assert start(client, sid, problem_id=issue['id'], namespace='am-agent', pod='bad', uid='different-uid').status_code == 409
    assert not collected


@pytest.mark.parametrize('target', [
    {'namespace': '../other', 'pod': 'bad', 'uid': 'old-uid'},
    {'namespace': 'am-agent', 'pod': 'bad/exec', 'uid': 'old-uid'},
    {'namespace': 'am-agent', 'pod': 'bad', 'uid': '../uid'},
    {'namespace': 'am-agent', 'pod': 'bad'},
    {'metric': 'shell'}, {},
])
def test_target_must_be_explicit_and_valid(client, scenario_case, target):
    _, collected, _ = scenario_case
    assert start(client, create_session(client), **target).status_code == 422
    assert not collected


def test_bound_problem_can_be_rechecked_after_it_disappears_from_snapshot(client, runtime, scenario_case):
    issue, collected, _ = scenario_case
    sid = create_session(client)
    run(client, sid, problem_id=issue['id'])
    Store(runtime).execute('UPDATE snapshots SET value=? WHERE cluster_id=?',
                           (json.dumps({'complete': True, 'resources': {'pods': {'items': []}}}), 'one'))
    job = run(client, sid, 'recovery', problem_id=issue['id'])
    assert job['scenario_result']['id'] == 'recovery'
    assert collected[-1]['uid'] == 'old-uid'


def test_deleted_pod_uses_measured_session_lineage_to_check_fresh_recovery(client, runtime, monkeypatch):
    from test_diagnostic_profiles import strict_upstream
    configure(client)
    add(client)
    issue = snapshot_issue(client, runtime)
    removed = False

    def handler(request):
        if removed and request.url.path == '/api/v1/namespaces/am-agent/pods/bad':
            return httpx.Response(404, json={})
        return strict_upstream(request)

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(connectors, 'llm_chat', lambda *args: VALID_MODEL)
    sid = create_session(client)
    first = run(client, sid, problem_id=issue['id'])
    assert json.loads(first['evidence'])['pod']['source_type'] == 'live'
    removed = True
    Store(runtime).execute('UPDATE snapshots SET value=? WHERE cluster_id=?',
                           (json.dumps({'complete': True, 'resources': {'pods': {'items': []}}}), 'one'))
    job = run(client, sid, 'recovery', problem_id=issue['id'])
    report = json.loads(job['evidence'])
    assert report['pod']['uid'] == 'old-uid' and report['pod']['source_type'] == 'cached'
    assert report['owners_complete'] is True and report['replacements']['complete'] is True
    assert job['scenario_result']['recovery']['state'] == 'verified'
    assert job['scenario_result']['model']['status'] == 'not_used'


def test_changed_kube_connection_requires_a_new_bound_session(client, scenario_case):
    issue, collected, _ = scenario_case
    sid = create_session(client)
    run(client, sid, problem_id=issue['id'])
    changed = client.put('/api/clusters/one', json={'kubeconfig': kubeconfig()}, headers=MUTATION)
    assert changed.status_code == 200
    assert start(client, sid, 'causes', problem_id=issue['id']).status_code == 409
    assert len(collected) == 1


def change_metrics_source(runtime):
    store = Store(runtime)
    cfg = store.cluster('one')['config']
    # A source edit intentionally leaves the kubeconfig generation unchanged.
    cfg['metrics'] = {'url': 'http://new-source.test', 'mode': 'dedicated'}
    store.execute('UPDATE clusters SET config=? WHERE id=?', (store.encrypt(cfg), 'one'))


def test_source_edit_without_new_kubeconfig_invalidates_bound_session(client, runtime, scenario_case):
    issue, collected, _ = scenario_case
    sid = create_session(client)
    run(client, sid, problem_id=issue['id'])
    change_metrics_source(runtime)
    assert start(client, sid, 'causes', problem_id=issue['id']).status_code == 409
    assert len(collected) == 1


def test_recovery_cannot_publish_success_after_source_edit_at_end_of_collection(client, runtime, scenario_case, monkeypatch):
    from test_workflows import recovered
    issue, _, _ = scenario_case

    def collect(*args, progress=None, **kwargs):
        callback = progress or args[5]
        report = recovered()
        for record in report['records']:
            record['check'] = 'Controlled recovery check'
        callback(copy.deepcopy(report))
        change_metrics_source(runtime)
        return report

    monkeypatch.setattr(diagnostics, 'collect_pod', collect)
    job = run(client, create_session(client), 'recovery', problem_id=issue['id'])
    assert job['status'] == 'failed'
    assert job['scenario_result']['status'] == 'unconfirmed'
    assert job['scenario_result']['recovery']['state'] == 'unconfirmed'
    assert json.loads(job['evidence'])['records']
    assert 'изменились' in job['scenario_result']['summary']


def test_collection_failure_keeps_partial_journal_and_canonical_cause(client, scenario_case, monkeypatch):
    issue, _, _ = scenario_case

    def collect(*args, **kwargs):
        args[5](fixture_report())
        raise connectors.ConnectorError('Interrupted collection')

    monkeypatch.setattr(diagnostics, 'collect_pod', collect)
    job = run(client, create_session(client), problem_id=issue['id'])
    assert job['status'] == 'failed'
    assert json.loads(job['evidence'])['records'][0]['id'] == 'E01'
    assert 'EmptyDir' in job['scenario_result']['summary']


def test_formatter_failure_finishes_job_and_keeps_journal(client, scenario_case, monkeypatch):
    from pulse_app import workflows
    issue, _, _ = scenario_case

    def broken(*args, **kwargs):
        raise ValueError('Controlled malformed source')

    monkeypatch.setattr(workflows, 'build_result', broken)
    job = run(client, create_session(client), problem_id=issue['id'])
    assert job['status'] == 'failed'
    assert job['scenario_result']['status'] == 'unconfirmed'
    assert json.loads(job['evidence'])['records']


def test_source_edit_while_model_runs_cannot_present_old_result_as_current(client, runtime, scenario_case, monkeypatch):
    issue, _, _ = scenario_case

    def model(*args):
        change_metrics_source(runtime)
        return VALID_MODEL

    monkeypatch.setattr(connectors, 'llm_chat', model)
    job = run(client, create_session(client), problem_id=issue['id'])
    assert job['status'] == 'failed'
    assert job['scenario_result']['status'] == 'unconfirmed'
    assert job['scenario_result']['rows'] == []
    assert json.loads(job['evidence'])['stale_sources'] is True


@pytest.mark.parametrize('answer,expected_status', [
    ('Not JSON; the whole service recovered.', 'invalid'),
    ('{"hypotheses":[],"checks":[],"severity":"healthy"}', 'invalid'),
    ('{"hypotheses":[{"text":"Диск полон","evidence_ids":["E99"]}],"checks":[]}', 'invalid'),
])
def test_invalid_model_output_cannot_replace_verified_cause(client, scenario_case, monkeypatch, answer, expected_status):
    issue, _, _ = scenario_case
    monkeypatch.setattr(connectors, 'llm_chat', lambda *args: answer)
    job = run(client, create_session(client), problem_id=issue['id'])
    result = job['scenario_result']
    assert result['model']['status'] == expected_status
    assert result['origin'] == 'pulse'
    canonical = result['summary'] + json.dumps(result['rows'], ensure_ascii=False)
    assert 'EmptyDir' in canonical and '1Gi' in canonical
    assert 'E99' not in json.dumps(result['rows'])
    assert 'whole service recovered' not in result['summary']
    assert json.loads(job['evidence'])['records']


def test_model_transport_error_keeps_canonical_result_and_evidence(client, scenario_case, monkeypatch):
    issue, _, _ = scenario_case

    def unavailable(*args):
        raise connectors.ConnectorError('Local model HTTP 503')

    monkeypatch.setattr(connectors, 'llm_chat', unavailable)
    job = run(client, create_session(client), problem_id=issue['id'])
    assert job['scenario_result']['model']['status'] == 'unavailable'
    assert job['scenario_result']['origin'] == 'pulse'
    canonical = job['scenario_result']['summary'] + json.dumps(job['scenario_result']['rows'], ensure_ascii=False)
    assert 'EmptyDir' in canonical and '1Gi' in canonical
    assert json.loads(job['evidence'])['records'][0]['id'] == 'E01'


def test_unconfigured_model_does_not_block_canonical_diagnostics(client, runtime, monkeypatch):
    add(client)
    issue = snapshot_issue(client, runtime)
    monkeypatch.setattr(diagnostics, 'collect_pod', lambda *args, **kwargs: fixture_report())
    job = run(client, create_session(client), problem_id=issue['id'])
    result = job['scenario_result']
    assert result['origin'] == 'pulse'
    assert result['model']['status'] == 'unavailable'
    canonical = result['summary'] + json.dumps(result['rows'], ensure_ascii=False)
    assert 'EmptyDir' in canonical and '1Gi' in canonical
    assert json.loads(job['evidence'])['records']


def test_recovery_without_model_is_not_blocked_and_cached_eviction_is_not_success(client, runtime, monkeypatch):
    add(client)
    issue = snapshot_issue(client, runtime)
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == '/api/v1/namespaces/am-agent/pods/bad':
            return httpx.Response(404, json={})
        return upstream_handler(request)

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(connectors, 'llm_chat', lambda *a: pytest.fail('Recovery must use measured state, without a model'))
    job = run(client, create_session(client), 'recovery', problem_id=issue['id'])
    result = job['scenario_result']
    assert result['recovery']['state'] != 'verified'
    report = json.loads(job['evidence'])
    assert report['pod']['source_type'] == 'cached'
    assert seen and all(request.method == 'GET' for request in seen)


def test_new_live_uid_is_not_used_as_evidence_for_old_problem(client, runtime, monkeypatch):
    add(client)
    issue = snapshot_issue(client, runtime)

    def handler(request):
        if request.url.path == '/api/v1/namespaces/am-agent/pods/bad':
            changed = failed_pod()
            changed['metadata']['uid'] = 'new-uid'
            changed['status']['message'] = 'UNRELATED-NEW-POD-FAILURE'
            return httpx.Response(200, json=changed)
        return upstream_handler(request)

    install_transport(monkeypatch, handler)
    job = run(client, create_session(client), 'recovery', problem_id=issue['id'])
    report = json.loads(job['evidence'])
    assert report['identity_changed'] is True
    assert job['scenario_result']['recovery']['state'] != 'verified'
    assert json.loads(job['context'])['uid'] == 'old-uid'
    assert 'UNRELATED-NEW-POD-FAILURE' not in json.dumps(report)
    assert 'UNRELATED-NEW-POD-FAILURE' not in json.dumps(job['scenario_result'])


def test_local_provider_is_explicit_no_silent_astra_or_holmes_fallback(client, scenario_case, monkeypatch):
    import pulse_app.astra_ai as astra
    issue, _, messages = scenario_case
    client.put('/api/clusters/one', json={'astra_ai': {'enabled': True,
        'url': 'http://astra.test/api/chat/completions', 'model': 'qwen', 'api_key': 'fixture'}}, headers=MUTATION)
    monkeypatch.setattr(astra, 'chat', lambda *a: pytest.fail('The selected local provider must stay local'))
    monkeypatch.setattr(connectors, 'holmes_chat', lambda *a: pytest.fail('No implicit Holmes fallback'))
    result = run(client, create_session(client, engine='local'), problem_id=issue['id'])
    assert result['scenario_result']['model']['status'] == 'complete'
    assert len(messages) == 1


def test_cluster_holmes_uses_fixed_json_contract_and_keeps_readonly_tool_trace(client, scenario_case, monkeypatch):
    issue, _, local_messages = scenario_case
    response = client.put('/api/clusters/one', json={'holmes': {
        'enabled': True, 'url': 'http://holmes.test:5051', 'model': 'pulse-local'}}, headers=MUTATION)
    assert response.status_code == 200, response.text
    seen = []

    def holmes(config, messages):
        assert config['model'] == 'pulse-local'
        seen.append(copy.deepcopy(messages))
        return VALID_MODEL, [{'tool': 'pulse-readonly', 'status': 'success'}]

    monkeypatch.setattr(connectors, 'holmes_chat', holmes)
    monkeypatch.setattr(connectors, 'llm_chat', lambda *a: pytest.fail('No silent local fallback from Holmes'))
    sid = create_session(client, engine='cluster')
    for scenario in ('brief', 'causes'):
        job = run(client, sid, scenario, problem_id=issue['id'])
        assert job['scenario_result']['model']['status'] == 'complete'
        assert {'tool': 'pulse-readonly', 'status': 'success'} in json.loads(job['trace'])
        assert {'tool': 'engine', 'status': 'HolmesGPT'} in json.loads(job['trace'])
    assert len(seen) == 2 and not local_messages
    assert all(len(messages) == 2 and all(m['role'] != 'assistant' for m in messages) for messages in seen)
    assert all('hypotheses' in messages[0]['content'] and 'checks' in messages[0]['content'] for messages in seen)


def test_closed_scenario_drops_late_model_result_and_preserves_archive(client, runtime, scenario_case, monkeypatch):
    issue, _, _ = scenario_case
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def delayed(*args):
        entered.set()
        assert release.wait(3), 'Test did not release the controlled model response'
        returned.set()
        return VALID_MODEL

    monkeypatch.setattr(connectors, 'llm_chat', delayed)
    sid = create_session(client)
    try:
        response = start(client, sid, problem_id=issue['id'])
        assert response.status_code == 202, response.text
        assert entered.wait(2)
        jid = response.json()['id']
        assert client.delete('/api/sessions/' + sid, headers=MUTATION).status_code == 200
    finally:
        release.set()
    assert returned.wait(2)
    assert client.get(f'/api/sessions/{sid}/jobs/{jid}').status_code == 404
    assert Store(runtime).rows('SELECT * FROM investigations') == []
    assert client.get('/api/investigations').json()['items'] == []


def test_plan_uses_only_readonly_http_and_does_not_execute_shell(client, runtime, monkeypatch):
    configure(client)
    add(client)
    issue = snapshot_issue(client, runtime)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == 'GET', 'Kubernetes writes are forbidden in every scenario'
        return upstream_handler(request)

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(connectors, 'llm_chat', lambda *a: VALID_MODEL)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('No shell commands may execute'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('No subprocess may execute'))
    monkeypatch.setattr(os, 'system', lambda *a: pytest.fail('No shell commands may execute'))
    job = run(client, create_session(client), 'plan', problem_id=issue['id'])
    assert seen and all(r.method == 'GET' for r in seen)
    assert not any('/exec' in r.url.path or '/attach' in r.url.path or '/secrets' in r.url.path for r in seen)
    assert job['scenario_result']['steps']
    assert json.loads(job['evidence'])['records']


def test_node_problem_uses_live_resource_report_through_scenario_api(client, runtime, monkeypatch):
    configure(client)
    add(client)
    node = {'metadata': {'name': 'worker', 'uid': 'node-uid'},
            'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}}
    snap = {'observed_at': datetime.now(timezone.utc).isoformat(), 'complete': True,
            'resources': {'nodes': {'items': [node]}}}
    Store(runtime).execute('INSERT INTO snapshots VALUES (?,?)', ('one', json.dumps(snap)))
    issue = next(item for item in client.get('/api/problems?cluster_id=one').json()['items']
                 if item['kind'] == 'nodes')
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == 'GET'
        return httpx.Response(200, json=node if request.url.path.endswith('/nodes/worker') else {'items': []})

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(connectors, 'llm_chat', lambda *args: VALID_MODEL)
    job = run(client, create_session(client), problem_id=issue['id'])
    report = json.loads(job['evidence'])
    assert job['status'] == 'done'
    assert report['resource']['uid'] == 'node-uid' and 'pod' not in report
    assert 'Ready=False' in job['scenario_result']['summary']
    assert seen and all(r.method == 'GET' for r in seen)


def test_metric_target_has_fresh_scoped_measurement_through_scenario_api(client, runtime, monkeypatch):
    configure(client)
    add(client, metrics={'url': 'http://metrics.test', 'mode': 'dedicated'})
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == 'GET'
        if request.url.path == '/api/v1/nodes':
            return httpx.Response(200, json={'items': [{'metadata': {'name': 'worker', 'uid': 'node-uid'}}]})
        assert request.url.host == 'metrics.test' and request.url.path == '/api/v1/query'
        is_nodes = 'by(instance)' in request.url.params['query'].split('100 * ')[-1]
        return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'vector',
            'result': [{'metric': {'instance': 'worker:9100'} if is_nodes else {},
                        'value': [time.time(), '37']}]}})

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(connectors, 'llm_chat', lambda *args: VALID_MODEL)
    job = run(client, create_session(client), metric='cpu')
    report = json.loads(job['evidence'])
    assert job['status'] == 'done' and report['metrics']['percent'] == 37
    assert report['metrics']['coverage']['ok'] is True
    assert '37%' in job['scenario_result']['summary']
    assert len(seen) == 3
