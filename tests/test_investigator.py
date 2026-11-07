"""Real app/adapter contracts for incident conversations; production systems are not contacted."""
import io
import json
from pathlib import Path
import tarfile
import zipfile

import httpx
import pytest

from pulse_app import connectors, diagnostics
from pulse_app.library import import_path
from pulse_app.store import Store
from test_api import client, runtime, add, MUTATION
from test_sessions import configure, session, message
from test_analysis_fix import report
from test_diagnostics import failed_pod
from test_connectors import install_transport


def incident(client, runtime):
    pod=failed_pod();pod['metadata']['uid']='old-uid'
    Store(runtime).execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)',('one',json.dumps({'complete':True,'resources':{'pods':{'items':[pod]}}})))
    return client.get('/api/problems?cluster_id=one').json()['items'][0]


def test_full_incident_uses_normal_completion_not_json_suggestions(client,runtime,monkeypatch):
    configure(client);add(client);issue=incident(client,runtime)
    monkeypatch.setattr(diagnostics,'collect_pod',lambda *a,**kw:report())
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        assert request.url.path=='/v1/chat/completions'
        assert body['max_tokens']==8192 and body['stream'] is False
        prompt=body['messages'][0]['content']
        assert 'шесть разделов' in prompt and 'ТОЛЬКО JSON' not in prompt
        return httpx.Response(200,json={'choices':[{'message':{'content':'## Что произошло\nEmptyDir data превысил 1Gi [E01].\n## Причины\nИсточник роста файлов не установлен.'},'finish_reason':'stop'}]})
    install_transport(monkeypatch,handler)
    sid=session(client,'one','problem')
    job=message(client,sid,'Разбери инцидент полностью',problem_id=issue['id'])
    assert job['status']=='done' and '## Причины' in job['answer']
    trace=json.loads(job['trace'])
    assert any(t['tool']=='knowledge-sources' for t in trace)
    assert any(t['tool']=='model-completion' for t in trace)
    assert client.get('/api/investigations').json()['items']==[]


def test_dialog_is_bound_to_target_and_source_configuration(client,runtime,monkeypatch):
    configure(client);add(client);issue=incident(client,runtime)
    monkeypatch.setattr(diagnostics,'collect_pod',lambda *a,**kw:report())
    monkeypatch.setattr(connectors,'llm_chat',lambda *a:'EmptyDir data 1Gi [E01]')
    sid=session(client,'one','problem')
    assert message(client,sid,'Проанализируй',problem_id=issue['id'])['status']=='done'
    r=client.post(f'/api/sessions/{sid}/messages',headers=MUTATION,json={'question':'Другая цель','namespace':'other','pod':'other','uid':'other-uid'})
    assert r.status_code==409
    client.put('/api/settings',headers=MUTATION,json={'llm':{'url':'http://other.test/v1','model':'qwen'}})
    assert client.post(f'/api/sessions/{sid}/messages',headers=MUTATION,json={'question':'Что проверить?'}).status_code==409


def test_resource_dialog_passes_exact_pod_identity(client,monkeypatch):
    configure(client);add(client);seen=[]
    def collect(cfg,ns,pod,uid,cached,progress):
        seen.append((ns,pod,uid));return report()
    monkeypatch.setattr(diagnostics,'collect_pod',collect)
    monkeypatch.setattr(connectors,'llm_chat',lambda *a:'Ответ [E01]')
    job=message(client,session(client,'one','problem'),'Почему ошибка?',namespace='am-agent',pod='bad',uid='exact-uid')
    assert job['status']=='done' and seen==[('am-agent','bad','exact-uid')]


@pytest.mark.parametrize('key,value',[('max_tokens',True),('max_tokens',100000),('timeout_seconds',1),('temperature',float('inf'))])
def test_invalid_budgets_rejected_without_echoing_credentials(client,key,value):
    # JSON cannot encode Infinity with httpx; exercise the common validator directly.
    from pulse_app.ai_limits import validate
    with pytest.raises(ValueError):validate({key:value})
    if value!=float('inf'):
        response=client.put('/api/settings',headers=MUTATION,json={'llm':{'url':'http://llm.test/v1','model':'qwen','api_key':'SECRET',key:value}})
        assert response.status_code==422 and 'SECRET' not in response.text


def test_configured_token_and_time_budget_reach_native_adapter(monkeypatch):
    def handler(request):
        assert json.loads(request.content)['max_tokens']==12000
        assert request.extensions['timeout']['read']==480
        return httpx.Response(200,json={'choices':[{'message':{'content':'done'},'finish_reason':'stop'}]})
    install_transport(monkeypatch,handler)
    assert connectors.llm_chat({'url':'http://llm.test/v1','model':'qwen','max_tokens':12000,'timeout_seconds':480},[{'role':'user','content':'test'}])=='done'


def test_holmes_refresh_keeps_credentials_and_creates_private_backup(client,runtime):
    configure(client);add(client)
    assert client.post('/api/clusters/one/holmes-bootstrap',headers=MUTATION,json={}).status_code==200
    root=runtime/'holmes/one'
    saved={name:(root/name).read_bytes() for name in ('compose.yaml','.env')}
    tools_before=json.loads((root/'http-toolset.json').read_text())['config']
    model=json.loads((root/'model_list.json').read_text())['pulse-local']
    response=client.post('/api/clusters/one/holmes-refresh',headers=MUTATION,json={'max_steps':10,'max_tokens':6000,'timeout_seconds':240})
    assert response.status_code==200 and response.json()['restart_required']
    assert all((root/name).read_bytes()==value for name,value in saved.items())
    assert json.loads((root/'http-toolset.json').read_text())['config']==tools_before
    current=json.loads((root/'model_list.json').read_text())['pulse-local']
    assert current['api_key']==model['api_key'] and current['api_base']==model['api_base']
    assert current['custom_args']['max_tokens']==6000
    assert json.loads((root/'config.json').read_text())['max_steps']==10
    backup=runtime/response.json()['backup']
    assert backup.is_dir() and backup.stat().st_mode&0o077==0
    assert (root/'skills/investigation/SKILL.md').is_file()


METADATA={'product':'kubernetes','version':'1.30','source':'https://kubernetes.io/docs/'}


def test_archive_only_indexes_markdown_and_never_extracts(tmp_path):
    archive=tmp_path/'docs.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('docs/guide.md','# Guide\nEnough Markdown documentation for indexing.')
        z.writestr('docs/ignore.pdf',b'not indexed')
    sent=[]
    result=import_path(archive,METADATA,lambda doc:(sent.append(doc),{})[1])
    assert result['status']=='complete' and result['indexed']==1 and result['skipped']==1
    assert sent[0]['name']=='docs/guide.md' and not (tmp_path/'docs').exists()


@pytest.mark.parametrize('name',['../escape.md','/absolute.md','C:/escape.md','a\\escape.md'])
def test_unsafe_archive_is_rejected_before_first_api_call(tmp_path,name):
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('safe.md','A valid document that must not be sent before validation.')
        z.writestr(name,'malicious path')
    result=import_path(archive,METADATA,lambda doc:pytest.fail('No HTTP call for an unsafe archive'))
    assert result['status']=='failed' and result['indexed']==0


def test_tar_with_root_directory_and_symbolic_links(tmp_path):
    archive=tmp_path/'docs.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        root=tarfile.TarInfo('.');root.type=tarfile.DIRTYPE;tar.addfile(root)
        body=b'# Root\nValid documentation in a tar archive.'
        member=tarfile.TarInfo('./guide.md');member.size=len(body);tar.addfile(member,io.BytesIO(body))
    assert import_path(archive,METADATA,lambda doc:{})['indexed']==1
    with tarfile.open(archive,'w:gz') as tar:
        link=tarfile.TarInfo('guide.md');link.type=tarfile.SYMTYPE;link.linkname='/etc/passwd';tar.addfile(link)
    assert import_path(archive,METADATA,lambda doc:pytest.fail('No links'))['status']=='failed'


def test_markdown_chunks_keep_normal_fence_and_heading():
    from pulse_app.markdown_chunks import chunks
    text='# First\n\nA paragraph.\n\n```yaml\nkind: Pod\n```\n\n# Second\n\nAnother paragraph.'
    rows=chunks(text)
    assert all(len(r['text'])<=1800 for r in rows)
    code=next(r for r in rows if 'kind: Pod' in r['text'])
    assert code['text'].count('```')==2 and code['heading']=='First'
    assert rows[-1]['heading']=='Second'


def test_idempotence_and_lexical_retrieval_are_cluster_scoped(tmp_path,monkeypatch):
    from test_knowledge import setup,document
    kb,points,calls=setup(tmp_path,monkeypatch)
    kb.ingest(document('one'));before=len(calls)
    assert kb.ingest(document('one'))['unchanged']
    assert not any(embedding for _,_,_,embedding in calls[before:])
    assert kb.lexical('EmptyDir','one') and not kb.lexical('EmptyDir','other')


@pytest.mark.parametrize('redirect',[False,True])
def test_importer_never_routes_admin_credentials_via_proxy_or_redirect(tmp_path,monkeypatch,redirect):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    from pulse_app.library import api_sender
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            calls.append(self.path)
            if redirect:
                self.send_response(302);self.send_header('Location','/outside');self.end_headers()
            else:
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"id":"fixture"}')
        def do_GET(self):
            calls.append(self.path);self.send_response(403);self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    secret=tmp_path/'password';secret.write_text('test-password-not-a-real-key')
    monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1');monkeypatch.setenv('http_proxy','http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY','');monkeypatch.setenv('no_proxy','')
    try:
        send=api_sender('http://127.0.0.1:'+str(server.server_port),secret,'pulse')
        if redirect:
            with pytest.raises(ValueError,match='HTTP 302'):send({'body':'fixture'})
        else:assert send({'body':'fixture'})['id']=='fixture'
        assert calls==['/api/knowledge/documents']
    finally:server.shutdown();server.server_close();thread.join()
