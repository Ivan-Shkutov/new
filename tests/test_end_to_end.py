"""Real HTTPS loopback upstreams exercise API + storage + connector transport.

These fixtures validate our contracts, not compatibility with a production
Kubernetes/Astra/model installation. No network outside this machine is used.
"""
import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
import pytest

from pulse_app.app import create_app

PASSWORD = 'e2e-local-password-not-a-production-secret'
HEADERS = {'X-Pulse-Request': '1'}


@pytest.fixture
def upstream(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Pulse local integration fixture')])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_file, key_file = tmp_path / 'fixture-cert.pem', tmp_path / 'fixture-key.pem'
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    observations = {'requests': [], 'promql': [], 'llm': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload, code=200):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            observations['requests'].append(self.path)
            if self.headers.get('Authorization') != 'Bearer fixture-kubernetes-token':
                return self.respond({'error': 'unauthorized'}, 401)
            if self.path.startswith('/prom/api/v1/query'):
                observations['promql'].append(self.path)
                return self.respond({'status': 'success', 'data': {'resultType': 'vector', 'result': []}})
            if self.path.startswith('/api/v1/namespaces/default/pods/web/log'):
                body = b'password=do-not-send-to-model\nreadiness failed on /ready'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if self.path.startswith('/api/v1/pods?'):
                return self.respond({'metadata': {}, 'items': [{'kind': 'Pod', 'metadata': {'name': 'web', 'namespace': 'default', 'uid': 'pod-uid'}, 'status': {'phase': 'Pending'}, 'spec': {'containers': [{'name': 'web', 'env': [{'name': 'PASSWORD', 'value': 'hidden-value'}]}]}}]})
            return self.respond({'metadata': {}, 'items': []})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/v1/chat/completions':
                if self.headers.get('Authorization') != 'Bearer fixture-model-token':
                    return self.respond({'error': 'unauthorized'}, 401)
                observations['llm'].append(payload)
                return self.respond({'choices': [{'message': {'content': 'Гипотеза: readiness не проходит. Источник: логи web. Проверьте /ready.'}}]})
            if self.path == '/astra/api/v1/logs/search/monitorLogs':
                return self.respond({'data': {'items': [
                    {'labels.cluster': 'one', 'labels.k8s.namespace.name': 'default', 'labels.k8s.pod.name': 'web', 'message': 'our log'},
                    {'labels.cluster': 'two', 'labels.k8s.namespace.name': 'default', 'labels.k8s.pod.name': 'web', 'message': 'foreign log'},
                ]}})
            self.respond({'error': 'unknown'}, 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'https://127.0.0.1:{server.server_port}', cert_pem, observations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_https_cluster_to_grounded_investigation_and_restart(upstream, tmp_path, monkeypatch):
    url, ca, observations = upstream
    runtime = tmp_path / 'runtime'
    (runtime / 'secrets').mkdir(parents=True)
    (runtime / 'secrets/portal-password').write_text(PASSWORD)
    monkeypatch.delenv('PULSE_PASSWORD_FILE', raising=False)
    monkeypatch.delenv('PULSE_USERNAME', raising=False)
    config = json.dumps({'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'read',
                         'contexts': [{'name': 'read', 'context': {'cluster': 'one', 'user': 'read'}}],
                         'clusters': [{'name': 'one', 'cluster': {'server': url, 'certificate-authority-data': base64.b64encode(ca).decode()}}],
                         'users': [{'name': 'read', 'user': {'token': 'fixture-kubernetes-token'}}]})
    with TestClient(create_app(runtime, polling=False)) as client:
        client.auth = ('pulse', PASSWORD)
        cluster = client.post('/api/clusters', json={
            'id': 'one', 'name': 'Loopback test', 'kubeconfig': config,
            'metrics': {'url': url + '/prom', 'ca_pem': ca.decode(), 'token': 'fixture-kubernetes-token', 'labels': {'cluster': 'one'}},
            'astra': {'url': url + '/astra', 'ca_pem': ca.decode(), 'token': 'fixture-astra-token', 'cluster_value': 'one'},
        }, headers=HEADERS)
        assert cluster.status_code == 201, cluster.text
        refresh = client.post('/api/clusters/one/refresh', headers=HEADERS)
        assert refresh.status_code == 200, refresh.text
        assert refresh.json()['complete']
        assert refresh.json()['resources']['pods']['items'][0]['metadata']['name'] == 'web'
        assert 'hidden-value' not in refresh.text
        metrics = client.get('/api/clusters/one/metrics')
        assert metrics.status_code == 200
        assert all(row['empty'] for row in metrics.json().values())
        assert len(observations['promql']) == 7
        astra = client.get('/api/clusters/one/logs', params={'namespace': 'default', 'pod': 'web', 'source': 'astra'})
        assert [row['message'] for row in astra.json()['items']] == ['our log']
        uploaded = client.post('/api/documents', data={'cluster_id': 'one'}, files={'file': ('guide.md', b'readiness: check the /ready endpoint')}, headers=HEADERS)
        assert uploaded.status_code == 201
        settings = client.put('/api/settings', json={'llm': {'url': url + '/v1', 'ca_pem': ca.decode(), 'api_key': 'fixture-model-token', 'model': 'fixture'}}, headers=HEADERS)
        assert settings.status_code == 200
        created = client.post('/api/investigations', json={'cluster_id': 'one', 'question': 'Почему readiness не проходит?', 'namespace': 'default', 'pod': 'web'}, headers=HEADERS)
        assert created.status_code == 202
        jid = created.json()['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get('/api/investigations/' + jid).json()
            if job['status'] not in ('queued', 'running'):
                break
            time.sleep(.02)
        assert job['status'] == 'done', job
        sent = json.dumps(observations['llm'], ensure_ascii=False)
        assert 'readiness failed' in sent and 'guide.md' in sent
        assert 'do-not-send-to-model' not in sent and 'hidden-value' not in sent
        resolved = client.post('/api/investigations/' + jid + '/resolve', json={'solution': 'Исправлен endpoint /ready, проверка успешна.'}, headers=HEADERS)
        assert resolved.status_code == 200
    with TestClient(create_app(runtime, polling=False)) as client:
        client.auth = ('pulse', PASSWORD)
        assert client.get('/api/incidents').json()['items'][0]['id'] == jid
        assert client.get('/api/documents/search', params={'q': 'readiness', 'cluster_id': 'one'}).json()['items'][0]['name'] == 'guide.md'
        assert client.get('/api/investigations/' + jid).json()['status'] == 'done'
