#!/usr/bin/env python3
"""Exercise guided diagnostics using an actual API, browser and TLS Kubernetes.

Run only against a disposable Pulse runtime on this machine. The local model
and cluster are controlled HTTP fixtures; this does not certify production
model accuracy, Astra permissions or a live Kubernetes cluster.
"""
import argparse
import base64
import copy
import ipaddress
import json
import shutil
import ssl
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pulse_app.store import Store


SCENARIOS = ('brief', 'deep', 'causes', 'impact', 'plan', 'recovery')
HEADERS = {'X-Pulse-Request': '1'}
MODEL_TEXT = 'Сопоставьте назначение временных файлов с правилами их очистки.'
POD = {
    'metadata': {'name': 'bad', 'namespace': 'am-agent', 'uid': 'fixture-old-uid',
                 'labels': {'app': 'agent'}, 'ownerReferences': [
                     {'kind': 'ReplicaSet', 'name': 'rs-old', 'uid': 'fixture-rs-old', 'controller': True}]},
    'spec': {'nodeName': 'worker-2', 'containers': [{'name': 'agent', 'image': 'fixture/agent:1'}],
             'volumes': [{'name': 'data', 'emptyDir': {'sizeLimit': '1Gi'}}]},
    'status': {'phase': 'Failed', 'reason': 'Evicted',
               'message': 'Usage of EmptyDir volume "data" exceeds the limit "1Gi".'},
}


class Fixtures:
    def __init__(self):
        self.lock = threading.Lock()
        self.behavior = 'valid'
        self.models, self.kubernetes, self.errors = [], [], []
        self.slow_started = threading.Event()
        self.slow_release = threading.Event()

    def mode(self, value):
        with self.lock:
            self.behavior = value


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, body, status=200):
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A superseded session is allowed to stop reading.

        def do_GET(self):
            path = urlsplit(self.path).path
            with state.lock:
                state.kubernetes.append(('GET', path))
            if self.headers.get('Authorization') != 'Bearer fixture-reader':
                state.errors.append('Kubernetes request lost fixture authorization')
                return self.respond({}, 401)
            if path == '/api/v1/namespaces/am-agent/pods/bad':
                return self.respond(POD)
            if path == '/api/v1/nodes/worker-2':
                return self.respond({'metadata': {'name': 'worker-2', 'uid': 'fixture-node'},
                    'status': {'conditions': [{'type': 'Ready', 'status': 'True'},
                                              {'type': 'DiskPressure', 'status': 'False'}]}})
            if path.endswith('/replicasets/rs-old'):
                return self.respond({'metadata': {'name': 'rs-old', 'uid': 'fixture-rs-old',
                    'ownerReferences': [{'kind': 'Deployment', 'name': 'agent',
                                         'uid': 'fixture-deployment', 'controller': True}]},
                    'spec': {'selector': {'matchLabels': {'app': 'agent'}}}})
            if path.endswith('/deployments/agent'):
                return self.respond({'metadata': {'name': 'agent', 'uid': 'fixture-deployment', 'generation': 1},
                    'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'agent'}}},
                    'status': {'observedGeneration': 1, 'replicas': 1, 'readyReplicas': 1,
                               'availableReplicas': 1, 'updatedReplicas': 1}})
            if path.endswith('/replicasets'):
                return self.respond({'items': [{'metadata': {'name': 'rs-new', 'uid': 'fixture-rs-new',
                    'ownerReferences': [{'kind': 'Deployment', 'uid': 'fixture-deployment', 'controller': True}]}}]})
            if path == '/api/v1/namespaces/am-agent/pods':
                return self.respond({'items': [{'metadata': {'name': 'replacement', 'namespace': 'am-agent',
                    'uid': 'fixture-new-uid', 'labels': {'app': 'agent'},
                    'ownerReferences': [{'kind': 'ReplicaSet', 'uid': 'fixture-rs-new', 'controller': True}]},
                    'spec': {'containers': [{'name': 'agent'}]},
                    'status': {'phase': 'Running', 'podIP': '10.0.0.20',
                        'conditions': [{'type': 'Ready', 'status': 'True'}],
                        'containerStatuses': [{'name': 'agent', 'ready': True, 'restartCount': 0,
                                               'state': {'running': {}}}]}}]})
            if path.endswith('/services'):
                return self.respond({'items': [{'metadata': {'name': 'agent-service', 'namespace': 'am-agent'},
                    'spec': {'selector': {'app': 'agent'}}}]})
            if path.endswith('/endpoints/agent-service'):
                # Ready Pod alone must not produce a green recovery verdict.
                return self.respond({'subsets': []})
            if path.endswith('/log'):
                return self.respond({'message': 'Fixture Pod logs expired'}, 404)
            if path.endswith('/events') or path.endswith('/networkpolicies'):
                return self.respond({'items': []})
            return self.respond({'message': 'Unknown fixture path'}, 404)

        def do_POST(self):
            if self.path != '/v1/chat/completions':
                state.errors.append('Unexpected write to Kubernetes fixture')
                return self.respond({}, 405)
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with state.lock:
                state.models.append(copy.deepcopy(body))
                behavior = state.behavior
            if body.get('stream') is not False:
                state.errors.append('Local JSON completion unexpectedly requested SSE')
            if behavior == 'slow':
                state.slow_started.set()
                if not state.slow_release.wait(15):
                    state.errors.append('Test did not release the delayed model')
                    return self.respond({}, 504)
            if behavior == 'error':
                return self.respond({'error': {'message': 'Controlled fixture outage'}}, 503)
            text = ('Untrusted invalid draft: every node is destroyed.' if behavior == 'invalid' else
                    json.dumps({'hypotheses': [], 'checks': [{
                        'title': 'Проверьте очистку временного тома',
                        'detail': MODEL_TEXT,
                        'expected': 'Будет установлен источник роста файлов.', 'evidence_ids': ['E01']} ]}, ensure_ascii=False))
            return self.respond({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]})

        def forbidden_write(self):
            state.errors.append('A diagnostics scenario attempted a Kubernetes mutation')
            self.respond({}, 405)

        do_PUT = do_PATCH = do_DELETE = forbidden_write

    return Handler


def fixture_servers(state, folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Pulse loopback fixture')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (folder / 'ca.pem').write_bytes(pem)
    (folder / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(folder / 'ca.pem', folder / 'key.pem')
    handler = handler_for(state)
    kube = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    kube.socket = tls.wrap_socket(kube.socket, server_side=True)
    model = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threads = []
    for server in (kube, model):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    config = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'fixture',
              'contexts': [{'name': 'fixture', 'context': {'cluster': 'fixture', 'user': 'reader'}}],
              'clusters': [{'name': 'fixture', 'cluster': {
                  'server': f'https://127.0.0.1:{kube.server_port}',
                  'certificate-authority-data': base64.b64encode(pem).decode()}}],
              'users': [{'name': 'reader', 'user': {'token': 'fixture-reader'}}]}
    return kube, model, threads, config


def wait_result(page, scenario):
    expect(page.locator('#scenario-result')).to_have_attribute('aria-busy', 'false', timeout=30000)
    expect(page.locator('#scenario-result-body')).to_have_attribute('data-scenario-result', scenario)
    expect(page.locator('#scenario-result-body .scenario-summary')).to_have_count(1)
    if scenario != 'recovery':
        expect(page.locator('#scenario-result-body')).to_contain_text('EmptyDir')
    assert not page.locator('#scenario-sources').evaluate('(element) => element.open')
    assert page.locator('#page-diagnostics textarea:visible').count() == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18102')
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--screenshots', type=Path, default=Path('/tmp/pulse-0150-scenarios'))
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    state = Fixtures()
    cid = 'scenario-ui-' + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix='pulse-scenario-fixture-') as temp:
        kube, model, threads, config = fixture_servers(state, Path(temp))
        browser = context = None
        page = None
        errors, requests, sessions, last_job = [], [], [], {}
        before = None
        playwright = sync_playwright().start()
        try:
            chromium = shutil.which('chromium') or shutil.which('chromium-browser')
            browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=['--no-sandbox'])
            context = browser.new_context(http_credentials={'username': 'pulse', 'password':
                (args.runtime / 'secrets/portal-password').read_text().strip()}, viewport={'width': 1440, 'height': 1000})
            api = context.request
            before = api.get(args.base_url + '/api/settings').json()['llm']
            assert not before['has_key'], 'Use a disposable runtime without a real model key'
            history = api.get(args.base_url + '/api/investigations').json()
            assert api.put(args.base_url + '/api/settings', headers=HEADERS, data={'llm': {
                'url': f'http://127.0.0.1:{model.server_port}/v1', 'model': 'qwen-fixture'}}).ok
            response = api.post(args.base_url + '/api/clusters', headers=HEADERS,
                data={'id': cid, 'name': 'Scenario UI fixture', 'enabled': False, 'kubeconfig': json.dumps(config)})
            assert response.status == 201, response.text()
            snapshot = {'observed_at': datetime.now(timezone.utc).isoformat(), 'complete': True,
                        'resources': {'pods': {'items': [POD], 'error': None, 'truncated': False}}}
            Store(args.runtime).execute('INSERT INTO snapshots VALUES (?,?)', (cid, json.dumps(snapshot)))
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))

            def response_recorded(response):
                if response.url.endswith('/api/sessions') and response.status == 201:
                    sessions.append(response.json()['id'])
                if response.url.endswith('/scenarios') and response.request.method == 'POST':
                    requests.append({'status': response.status, 'body': response.request.post_data_json})
                if '/jobs/' in response.url and response.status == 200:
                    job = response.json()
                    last_job.clear()
                    last_job.update({key: job.get(key) for key in ('scenario', 'status', 'progress')})
                    last_job['model_status'] = (job.get('scenario_result') or {}).get('model', {}).get('status')

            page.on('response', response_recorded)
            page.goto(args.base_url)
            page.locator('#cluster-select').select_option(cid)
            page.get_by_role('button', name='Диагностика: bad', exact=True).click()
            expect(page.locator('#page-diagnostics')).to_be_visible()
            expect(page.locator('#scenario-buttons [data-scenario]')).to_have_count(6)
            wait_result(page, 'brief')
            page.screenshot(path=str(args.screenshots / 'brief.png'), full_page=True)
            assert len(state.models) == 1
            for scenario in SCENARIOS[1:]:
                page.locator(f'#scenario-buttons [data-scenario="{scenario}"]').click()
                wait_result(page, scenario)
                if scenario in ('deep', 'plan', 'recovery'):
                    page.screenshot(path=str(args.screenshots / f'{scenario}.png'), full_page=True)
                if scenario == 'plan':
                    expect(page.locator('#scenario-result-body .scenario-command code').first).to_be_visible()
                    expect(page.locator('#scenario-result-body')).to_contain_text('Pulse команды не запускает')
                if scenario == 'recovery':
                    assert page.locator('.scenario-recovery-state').get_attribute('data-recovery-state') != 'verified'
            # The sixth action uses observed state and needs no completion.
            assert len(state.models) == 5
            assert all(r['status'] == 202 for r in requests), requests
            assert [r['body']['scenario'] for r in requests] == list(SCENARIOS)
            assert all(not any(k in r['body'] for k in ('question', 'prompt', 'previous_id')) for r in requests)
            assert all(not any(m['role'] == 'assistant' for m in r['messages']) for r in state.models)
            assert all(len(r['messages']) == 2 for r in state.models)
            # A malformed JSON response cannot become visible model prose.
            state.mode('invalid')
            page.locator('#scenario-buttons [data-scenario="brief"]').click()
            wait_result(page, 'brief')
            expect(page.locator('#scenario-status')).to_contain_text('ИИ не завершил')
            assert 'every node is destroyed' not in page.locator('#scenario-result-body').inner_text()
            page.locator('#scenario-sources > summary').click()
            expect(page.locator('#scenario-sources')).to_contain_text('не прошёл проверку')
            state.mode('error')
            page.locator('#scenario-buttons [data-scenario="causes"]').click()
            wait_result(page, 'causes')
            expect(page.locator('#scenario-status')).to_contain_text('ИИ не завершил')
            expect(page.locator('#scenario-result-body')).to_contain_text('факты Pulse')
            page.screenshot(path=str(args.screenshots / 'model-error.png'), full_page=True)
            # A delayed old action must not overwrite the newer button.
            state.mode('slow')
            page.locator('#scenario-buttons [data-scenario="deep"]').click()
            assert state.slow_started.wait(5), 'Slow model fixture was not reached'
            old_sid = sessions[-1]
            state.mode('valid')
            page.locator('#scenario-buttons [data-scenario="impact"]').click()
            wait_result(page, 'impact')
            state.slow_release.set()
            page.wait_for_timeout(1200)  # Let the superseded poll/response reach the browser.
            expect(page.locator('#scenario-result-body')).to_have_attribute('data-scenario-result', 'impact')
            assert sessions[-1] != old_sid
            # Provider changes clear results before creating a fresh session.
            page.locator('#scenario-provider').select_option('local')
            expect(page.locator('#scenario-result-body .scenario-summary')).to_have_count(0)
            page.locator('#scenario-buttons [data-scenario="brief"]').click()
            wait_result(page, 'brief')
            assert sessions[-1] != old_sid
            # Leaving and reopening the page cannot restore a previous answer.
            page.locator('#navigation a[data-page="overview"]').click()
            page.locator('#navigation a[data-page="diagnostics"]').click()
            expect(page.locator('#scenario-result-body .scenario-summary')).to_have_count(0)
            expect(page.locator('#scenario-target')).to_contain_text('Выберите объект')
            page.locator('#navigation a[data-page="resources"]').click()
            expect(page.locator('#resource-table')).to_contain_text('bad')
            page.get_by_role('button', name='Диагностика', exact=True).click()
            wait_result(page, 'brief')
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile viewport overflows horizontally'
            page.screenshot(path=str(args.screenshots / 'mobile-brief.png'), full_page=True)
            assert api.get(args.base_url + '/api/investigations').json() == history
            assert not errors, errors
            assert not state.errors, state.errors
            assert state.kubernetes and all(method == 'GET' for method, _ in state.kubernetes)
            assert not any('/exec' in path or '/secrets' in path or '/attach' in path for _, path in state.kubernetes)
            print('PASS: actual browser/API/TLS Kubernetes/HTTP JSON-model fixtures; six fixed actions, scoped UID, fresh read-only checks, compact replaceable result, collapsed evidence, no prompt/history, archive unchanged, invalid/error model fallback, superseded action discarded, provider/reopen clear state, overview and Resources entry points; recovery is not falsely green.')
            print('Screenshots: ' + str(args.screenshots))
        except Exception:
            # Diagnostic output contains only fixture counters, paths and job
            # state; never print request headers, model messages or credentials.
            print('Fixture diagnostics: ' + json.dumps({'model_calls': len(state.models),
                'kubernetes_calls': len(state.kubernetes), 'last_kubernetes_paths': state.kubernetes[-5:],
                'fixture_errors': state.errors, 'browser_errors': errors, 'last_job': last_job,
                'actions': [r['body']['scenario'] for r in requests]}, ensure_ascii=False), flush=True)
            if page is not None and not page.is_closed():
                page.screenshot(path=str(args.screenshots / 'failure.png'), full_page=True)
            raise
        finally:
            state.slow_release.set()
            if context is not None:
                context.request.delete(args.base_url + '/api/clusters/' + cid, headers=HEADERS)
                if before is not None:
                    context.request.put(args.base_url + '/api/settings', headers=HEADERS,
                        data={'llm': {'url': before['url'], 'model': before['model']}})
            if browser is not None:
                browser.close()
            for server in (kube, model):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)
            playwright.stop()


if __name__ == '__main__':
    main()
