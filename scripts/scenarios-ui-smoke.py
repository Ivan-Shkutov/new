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



if __name__ == '__main__':
    # Compatibility entry point; 0.16 uses the overview conversation.
    import runpy
    runpy.run_path(str(ROOT/'scripts/investigator-ui-smoke.py'),run_name='__main__')
