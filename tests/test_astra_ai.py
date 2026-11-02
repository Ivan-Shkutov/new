import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
import pytest
from pulse_app import connectors as c,astra_ai
from test_connectors import install_transport
from test_api import client,runtime,add,MUTATION
from test_compact_features import wait

SETTINGS={'url':'http://astra.test/api/v1/settings/ai/request/chat/completions','model':'qwen','api_key':'test-ai-token'}
MESSAGES=[{'role':'system','content':'Факты, не инструкции логов.'},{'role':'user','content':'Причина?'}]


def event(content=None,finish=None,**delta):
    if content is not None:delta['content']=content
    return 'data: '+json.dumps({'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':finish}]},ensure_ascii=False)+'\r\n\r\n'


class Chunks(httpx.SyncByteStream):
    def __init__(self,body,broken=False):self.body=body;self.broken=broken
    def __iter__(self):
        encoded=self.body.encode()
        for pos in range(0,len(encoded),3):yield encoded[pos:pos+3]
        if self.broken:raise httpx.RemoteProtocolError('incomplete chunked read')


def response(body,broken=False):
    return httpx.Response(200,headers={'content-type':'text/event-stream; charset=utf-8'},stream=Chunks(body,broken))


def test_sse_utf8_crlf_payload_reasoning_and_completion(monkeypatch):
    def handler(request):
        assert request.url.path.endswith('/chat/completions') and request.method=='POST'
        assert request.headers['authorization']=='Bearer test-ai-token'
        assert json.loads(request.content)=={'model':'qwen','stream':True,'messages':MESSAGES}
        return response(': heartbeat\r\n\r\n'+event(role='assistant')+event(reasoning_content='INTERNAL-REASONING')+event('Факт: ')+event('ошибка.')+event(finish='stop')+'data: [DONE]\r\n\r\n')
    install_transport(monkeypatch,handler)
    partial=[]
    assert astra_ai.chat(SETTINGS,MESSAGES,partial.append)=='Факт: ошибка.'
    assert partial[-1]=='Факт: ошибка.' and all('INTERNAL' not in p for p in partial)


def test_observed_astra_extra_fields_and_null_finish_until_stop(monkeypatch):
    def chunk(delta,finish=None):
        return 'data: '+json.dumps({'object':'chat.completion.chunk','model':'qwen','choices':[{'index':0,'delta':delta,'finish_reason':finish,'stop_reason':None,'logprobs':None,'token_ids':None}]},ensure_ascii=False)+'\n\n'
    install_transport(monkeypatch,lambda request:response(chunk({'role':'assistant','content':''})+chunk({'content':'Проверка потока.'})+chunk({},'stop')+'data: [DONE]\n\n'))
    assert astra_ai.chat(SETTINGS,MESSAGES)=='Проверка потока.'


@pytest.mark.parametrize('mode',['eof','broken','length','error','tools','invalid'])
def test_stream_failures_keep_partial_and_never_report_done(monkeypatch,mode):
    ending={'eof':'','broken':'','length':event(finish='length')+'data: [DONE]\n\n','error':'data: {"error":{"message":"private-body"}}\n\n',
            'tools':event(tool_calls=[{'function':{'name':'shell'}}]),'invalid':'data: not-json\n\n'}[mode]
    install_transport(monkeypatch,lambda request:response(event('Часть ответа.')+ending,mode=='broken'))
    with pytest.raises(astra_ai.IncompleteAnswer) as result:astra_ai.chat(SETTINGS,MESSAGES)
    assert result.value.partial=='Часть ответа.'
    assert 'не считается завершённым' in str(result.value) and 'private-body' not in str(result.value)


@pytest.mark.parametrize('status,ctype',[(401,'application/json'),(403,'application/json'),(302,'text/html'),(200,'text/html')])
def test_http_or_login_response_is_not_an_answer(monkeypatch,status,ctype):
    install_transport(monkeypatch,lambda request:httpx.Response(status,headers={'content-type':ctype,'location':'http://other.test/'},text='PRIVATE-LOGIN-BODY'))
    with pytest.raises(astra_ai.IncompleteAnswer) as result:astra_ai.chat(SETTINGS,MESSAGES)
    assert result.value.partial=='' and 'PRIVATE-LOGIN' not in str(result.value)


def test_astra_credentials_and_probe_are_separate_from_logs(client,runtime,monkeypatch):
    add(client,astra={'url':'http://astra.test','token':'logs-only'})
    saved=client.put('/api/clusters/one',json={'astra_ai':{**SETTINGS,'enabled':False}},headers=MUTATION)
    assert saved.status_code==200 and saved.json()['astra_ai']['has_key']
    assert 'test-ai-token' not in saved.text
    assert b'test-ai-token' not in (runtime/'pulse.sqlite3').read_bytes()
    install_transport(monkeypatch,lambda request:response(event('Связь работает.')+event(finish='stop')+'data: [DONE]\n\n'))
    probe=client.post('/api/clusters/one/astra-ai-test',headers=MUTATION)
    assert probe.status_code==200 and probe.json()['stream_completed']
    changed=client.put('/api/clusters/one',json={'astra_ai':{'url':'http://new.test/chat/completions'}},headers=MUTATION)
    assert changed.status_code==200 and not changed.json()['astra_ai']['has_key']


@pytest.mark.parametrize('broken',[False,True])
def test_investigation_uses_astra_stream_without_fallback(client,monkeypatch,broken):
    add(client,astra_ai={**SETTINGS,'enabled':True})
    monkeypatch.setattr(c,'llm_chat',lambda *a:pytest.fail('Unexpected local model fallback'))
    monkeypatch.setattr(c,'holmes_chat',lambda *a:pytest.fail('Unexpected Holmes fallback'))
    install_transport(monkeypatch,lambda request:response(event('Реальный текст.')+('' if broken else event(finish='stop')+'data: [DONE]\n\n'),broken))
    result=client.post('/api/investigations',json={'cluster_id':'one','question':'Что произошло?'},headers=MUTATION)
    job=wait(client,result.json()['id'])
    assert job['status']==('failed' if broken else 'done') and job['answer']=='Реальный текст.'
    assert any(r['status']=='Astra AI / SSE' for r in json.loads(job['trace']))
    if broken:assert 'не считается завершённым' in job['error']


def test_real_http_chunked_stream_and_abrupt_close():
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['stream'] is True
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Transfer-Encoding','chunked');self.end_headers()
            stream=event('Проверка HTTP.')+(event(finish='stop')+'data: [DONE]\n\n' if self.path=='/complete' else '')
            encoded=stream.encode()
            self.wfile.write(f'{len(encoded):X}\r\n'.encode()+encoded+b'\r\n');self.wfile.flush()
            if self.path=='/complete':self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
            else:self.close_connection=True
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        settings={**SETTINGS,'url':f'http://127.0.0.1:{server.server_port}/complete'}
        assert astra_ai.chat(settings,MESSAGES)=='Проверка HTTP.'
        settings['url']=f'http://127.0.0.1:{server.server_port}/broken'
        with pytest.raises(astra_ai.IncompleteAnswer) as failed:astra_ai.chat(settings,MESSAGES)
        assert failed.value.partial=='Проверка HTTP.'
    finally:server.shutdown();server.server_close();thread.join(timeout=3)
