"""Redeployment must preserve a usable runtime and reject false readiness."""
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from test_cli import cli
from test_api import client, runtime, add, MUTATION


def test_restore_keeps_https_readable_and_secrets_private(tmp_path):
    root=tmp_path/'runtime';cli.initialize(root)
    (root/'https').mkdir()
    config=root/'https/Caddyfile';config.write_text('localhost { reverse_proxy 127.0.0.1:8090 }')
    archive=cli.make_backup(root,tmp_path/'backups')
    cli.restore_backup(root,archive)
    assert config.stat().st_mode & 0o777 == 0o644
    assert (root/'registry.key').stat().st_mode & 0o777 == 0o600
    assert (root/'secrets/portal-password').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('path,body',[
    ('.env','PULSE_UID=-1\n'),('.env','PULSE_GID=invalid\n'),('.env','PULSE_PORT=80\n'),
    ('registry.key','invalid-key'),('secrets/portal-password','short'),
])
def test_bad_restore_is_rejected_before_replacing_runtime(tmp_path,path,body):
    current=tmp_path/'runtime';source=tmp_path/'source'
    cli.initialize(current);cli.initialize(source)
    original=(current/'registry.key').read_bytes()
    (source/path).write_text(body)
    archive=cli.make_backup(source,tmp_path/'backups')
    with pytest.raises(ValueError):cli.restore_backup(current,archive)
    assert (current/'registry.key').read_bytes()==original
    assert not list(tmp_path.glob('runtime.before-*'))


def test_empty_backup_checksum_is_a_safe_error(tmp_path):
    root=tmp_path/'runtime';cli.initialize(root)
    archive=cli.make_backup(root,tmp_path/'backups')
    Path(str(archive)+'.sha256').write_text('')
    with pytest.raises(ValueError,match='SHA256'):cli.restore_backup(root,archive)


class Reply(io.BytesIO):
    status=200


def services(monkeypatch,root,command):
    monkeypatch.setattr(sys,'argv',['pulse-services.py','--runtime',str(root),'--offline',command])
    return runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/pulse-services.py'),run_name='__main__')


def test_knowledge_start_fills_blank_fields_and_preserves_existing_values(tmp_path,monkeypatch):
    root=tmp_path/'runtime';cli.initialize(root)
    with (root/'.env').open('a') as f:f.write('PULSE_QDRANT_URL=\nPULSE_EMBEDDINGS_URL=\nPULSE_EMBEDDINGS_MODEL=custom:release\n')
    calls=[]
    monkeypatch.setattr(subprocess,'run',lambda args,**kw:calls.append(kw['env'].copy()))
    monkeypatch.setattr(cli.urllib.request,'urlopen',lambda *a,**kw:Reply(b'{}'))
    services(monkeypatch,root,'knowledge-start')
    settings=cli.load_env(root)
    assert settings['PULSE_QDRANT_URL']=='http://127.0.0.1:16333'
    assert settings['PULSE_EMBEDDINGS_URL']=='http://127.0.0.1:11435'
    assert settings['PULSE_EMBEDDINGS_MODEL']=='custom:release'
    assert calls[0]['PULSE_QDRANT_URL']==settings['PULSE_QDRANT_URL']
    assert (root/'.env').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('loaded,success',[('bge-m3:latest',False),('bge-m3:release',True)])
def test_knowledge_check_requires_the_configured_tag(tmp_path,monkeypatch,loaded,success):
    root=tmp_path/'runtime';cli.initialize(root)
    with (root/'.env').open('a') as f:f.write('PULSE_EMBEDDINGS_MODEL=bge-m3:release\n')
    monkeypatch.setattr(cli.urllib.request,'urlopen',lambda *a,**kw:Reply(json.dumps({'models':[{'name':loaded}]}).encode()))
    if success:services(monkeypatch,root,'knowledge-check')
    else:
        with pytest.raises(SystemExit,match='tag is not loaded'):services(monkeypatch,root,'knowledge-check')


def test_knowledge_check_rejects_digest_drift(tmp_path,monkeypatch):
    root=tmp_path/'runtime';cli.initialize(root)
    with (root/'.env').open('a') as f:f.write('PULSE_EMBEDDINGS_REVISION=pinned\n')
    monkeypatch.setattr(cli.urllib.request,'urlopen',lambda *a,**kw:Reply(json.dumps({'models':[{'name':'bge-m3:latest','digest':'changed'}]}).encode()))
    with pytest.raises(SystemExit,match='digest changed'):services(monkeypatch,root,'knowledge-check')


def test_failed_holmes_file_generation_can_be_retried(tmp_path,monkeypatch):
    from pulse_app import holmes_bundle as bundle
    original=bundle._write_bundle
    def fail(root,*args):
        root.mkdir();(root/'partial').write_text('incomplete');raise OSError('fixture storage failure')
    monkeypatch.setattr(bundle,'_write_bundle',fail)
    args=(tmp_path,'one',8090,5051,'holmes:test',{'url':'http://localhost:8002','model':'qwen'},'agent','api')
    with pytest.raises(OSError):bundle.write_bundle(*args)
    assert list((tmp_path/'holmes').iterdir())==[]
    monkeypatch.setattr(bundle,'_write_bundle',original)
    root=bundle.write_bundle(*args)
    key=(root/'.env').read_text()
    with pytest.raises(FileExistsError):bundle.write_bundle(*args)
    assert (root/'.env').read_text()==key


def test_holmes_bootstrap_cannot_overwrite_a_concurrent_metrics_change(client,runtime,monkeypatch):
    from pulse_app.store import Store
    add(client)
    client.put('/api/settings',json={'llm':{'url':'http://localhost:8002/v1','model':'qwen'}},headers=MUTATION)
    original=Store.settings
    def changing_settings(store):
        cfg=store.cluster('one')['config'];cfg['metrics']={'url':'http://new-source.test','mode':'dedicated'}
        store.execute('UPDATE clusters SET config=? WHERE id=?',(store.encrypt(cfg),'one'))
        return original(store)
    monkeypatch.setattr(Store,'settings',changing_settings)
    response=client.post('/api/clusters/one/holmes-bootstrap',json={},headers=MUTATION)
    assert response.status_code==409
    assert client.get('/api/clusters').json()['items'][0]['metrics']['url']=='http://new-source.test'
    assert not (runtime/'holmes/one').exists()


def test_failed_holmes_database_write_removes_unpublished_bundle(client,runtime,monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    from pulse_app.store import Store
    add(client)
    client.put('/api/settings',json={'llm':{'url':'http://localhost:8002/v1','model':'qwen'}},headers=MUTATION)
    original=Store.connect
    @contextmanager
    def failing_write(store):
        with original(store) as connection:
            class Proxy:
                def execute(self,sql,args=()):
                    if sql.startswith('UPDATE clusters SET config='):raise sqlite3.OperationalError('fixture write failure')
                    return connection.execute(sql,args)
            yield Proxy()
    monkeypatch.setattr(Store,'connect',failing_write)
    with pytest.raises(sqlite3.OperationalError):client.post('/api/clusters/one/holmes-bootstrap',json={},headers=MUTATION)
    assert not (runtime/'holmes/one').exists()
    assert client.get('/api/clusters').json()['items'][0]['holmes']['has_key'] is False


def test_health_check_bypasses_ambient_proxy_for_local_credentials(tmp_path,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    from pulse_app.build import identity
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            assert self.headers.get('Authorization','').startswith('Basic ')
            body=json.dumps(identity() if self.path=='/healthz' else {'clusters':[]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        root=tmp_path/'runtime';cli.initialize(root,server.server_port)
        monkeypatch.setenv('http_proxy','http://127.0.0.1:1');monkeypatch.setenv('no_proxy','')
        monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1');monkeypatch.setenv('NO_PROXY','')
        cli.health_check(root)
    finally:server.shutdown();server.server_close();thread.join()
