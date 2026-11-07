"""Operator credentials stay on the requested origin, even with redirects/proxies."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('script', ['pulse-doctor.py', 'live-acceptance.py'])
def test_admin_credentials_are_not_followed_to_redirect_target(tmp_path, script):
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            paths.append(self.path)
            if self.path != '/outside':
                self.send_response(302); self.send_header('Location', '/outside'); self.end_headers()
            else:
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"status":"ok","version":"fixture"}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    secrets = tmp_path/'secrets'; secrets.mkdir(); password = secrets/'portal-password'
    password.write_text('test-only-admin-password')
    (tmp_path/'.env').write_text(f'PULSE_PORT={server.server_port}\n')
    args = ['--runtime', str(tmp_path)] if script == 'pulse-doctor.py' else [
        '--url', f'http://127.0.0.1:{server.server_port}', '--password-file', str(password)]
    try:
        result = subprocess.run([sys.executable, str(ROOT/'scripts'/script), *args],
                                capture_output=True, timeout=10)
        assert result.returncode != 0 and paths and '/outside' not in paths
        assert password.read_bytes() not in result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_acceptance_uses_local_origin_despite_poisoned_ambient_proxy(tmp_path):
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            paths.append(self.path)
            data = {'/healthz': {'status': 'ok', 'version': 'fixture'},
                    '/api/clusters': {'items': []},
                    '/api/knowledge/status': {'model': 'fixture', 'documents': 0}}[self.path]
            body = json.dumps(data).encode(); self.send_response(200); self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    password = tmp_path/'password'; password.write_text('test-only-admin-password')
    env = {**os.environ, 'HTTP_PROXY': 'http://127.0.0.1:9', 'http_proxy': 'http://127.0.0.1:9', 'NO_PROXY': '', 'no_proxy': ''}
    try:
        result = subprocess.run([sys.executable, str(ROOT/'scripts/live-acceptance.py'),
            '--url', f'http://127.0.0.1:{server.server_port}', '--password-file', str(password)],
            env=env, capture_output=True, timeout=10)
        assert result.returncode == 0 and len(paths) == 3
        assert password.read_bytes() not in result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
