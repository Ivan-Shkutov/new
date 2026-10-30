import json
import threading
import time

import pytest
from test_api import client, runtime, add, MUTATION
from pulse_app import connectors
from pulse_app.sessions import Sessions, SessionGone


def configure(client):
    assert client.put('/api/settings',json={'llm':{'url':'http://127.0.0.1:8002/v1','model':'qwen'}},headers=MUTATION).status_code==200


def session(client,cid='',mode='chat'):
    r=client.post('/api/sessions',json={'cluster_id':cid,'mode':mode},headers=MUTATION)
    assert r.status_code==201,r.text
    assert r.json()['persistent'] is False
    return r.json()['id']


def message(client,sid,question='Объясни Kubernetes',**extra):
    r=client.post(f'/api/sessions/{sid}/messages',json={'question':question,**extra},headers=MUTATION)
    assert r.status_code==202,r.text
    jid=r.json()['id']
    for _ in range(300):
        job=client.get(f'/api/sessions/{sid}/jobs/{jid}').json()
        if job['status'] not in ('queued','running'):return job
        time.sleep(.01)
    raise AssertionError('Job did not finish')


def test_dialog_followup_is_volatile_and_reopen_is_empty(client,runtime,monkeypatch):
    configure(client);calls=[]
    monkeypatch.setattr(connectors,'llm_chat',lambda cfg,messages:(calls.append(messages),'Ответ из модели')[1])
    sid=session(client)
    first=message(client,sid,'Почему небо синее?')
    assert first['status']=='done' and first['answer']=='Ответ из модели'
    second=message(client,sid,'Объясни подробнее')
    assert second['previous_id']==first['id']
    assert any(m['role']=='assistant' and m['content']=='Ответ из модели' for m in calls[-1])
    assert 'на любые вопросы' in calls[0][0]['content']
    assert client.get('/api/investigations').json()['items']==[]
    assert client.get('/api/investigations/'+first['id']).status_code==404
    assert client.delete('/api/sessions/'+sid,headers=MUTATION).status_code==200
    assert client.get(f'/api/sessions/{sid}/jobs/{first["id"]}').status_code==404
    newer=session(client)
    assert newer!=sid
    third=message(client,newer)
    assert third['previous_id']=='' and not any(m['role']=='assistant' for m in calls[-1])
    from pulse_app.store import Store
    assert Store(runtime).rows('SELECT * FROM investigations')==[]


def test_close_during_generation_discards_late_answer(client,monkeypatch):
    configure(client);started=threading.Event();release=threading.Event()
    def llm(*args):
        started.set();release.wait(3);return 'Late private answer'
    monkeypatch.setattr(connectors,'llm_chat',llm)
    sid=session(client)
    r=client.post(f'/api/sessions/{sid}/messages',json={'question':'Длинный вопрос'},headers=MUTATION)
    assert r.status_code==202 and started.wait(2)
    jid=r.json()['id']
    client.delete('/api/sessions/'+sid,headers=MUTATION);release.set()
    assert client.get(f'/api/sessions/{sid}/jobs/{jid}').status_code==404
    assert client.get('/api/investigations').json()['items']==[]


def test_session_uses_selected_astra_engine_and_keeps_scoped_context(client,monkeypatch):
    import pulse_app.astra_ai as astra
    add(client,astra_ai={'enabled':True,'url':'http://astra.test/api/chat/completions','model':'qwen','api_key':'fake'})
    seen=[]
    monkeypatch.setattr(astra,'chat',lambda cfg,messages,on_partial:(seen.append(messages),'Astra reply')[1])
    monkeypatch.setattr(connectors,'llm_chat',lambda *a:pytest.fail('Unexpected local fallback'))
    sid=session(client,'one');job=message(client,sid)
    assert job['status']=='done' and job['answer']=='Astra reply' and seen
    other=session(client,'one')
    assert client.get(f'/api/sessions/{other}/jobs/{job["id"]}').status_code==404


def test_session_expiry_and_capacity_are_bounded(monkeypatch):
    import pulse_app.sessions as module
    clock=[100.0];monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    store=Sessions(ttl=10,capacity=1);sid=store.create('','chat')
    with pytest.raises(ValueError):store.create('','chat')
    clock[0]+=11
    with pytest.raises(SessionGone):store.get(sid)
    assert store.create('','chat')!=sid


def test_sessions_require_auth_and_mutation_header(client):
    assert client.post('/api/sessions',json={'mode':'chat'}).status_code==403
    assert client.post('/api/sessions',json={'mode':'chat'},headers=MUTATION,auth=None).status_code==401
    assert client.post('/api/sessions',json={'mode':'problem'},headers=MUTATION).status_code==422


def test_chat_can_bypass_astra_with_explicit_local_engine(client,monkeypatch):
    import pulse_app.astra_ai as astra
    configure(client)
    add(client,astra_ai={'enabled':True,'url':'http://astra.test/api/chat/completions','model':'qwen','api_key':'fake'})
    monkeypatch.setattr(astra,'chat',lambda *a:pytest.fail('Astra must not be called in local mode'))
    monkeypatch.setattr(connectors,'llm_chat',lambda *a:'Direct local reply')
    sid=client.post('/api/sessions',json={'cluster_id':'one','mode':'chat','engine':'local'},headers=MUTATION).json()['id']
    assert message(client,sid)['answer']=='Direct local reply'


def test_problem_checks_and_knowledge_are_collected_before_model(client,runtime,monkeypatch):
    from pulse_app import diagnostics
    from pulse_app.knowledge import Knowledge
    from pulse_app.store import Store
    from test_diagnostics import failed_pod
    from datetime import datetime,timezone
    configure(client);add(client);order=[]
    snap={'observed_at':datetime.now(timezone.utc).isoformat(),'complete':True,'resources':{'pods':{'items':[failed_pod()]}},'cluster_info':{'server_version':'v1.30.8'}}
    Store(runtime).execute('INSERT INTO snapshots VALUES (?,?)',('one',json.dumps(snap)))
    issue=client.get('/api/problems?cluster_id=one').json()['items'][0]
    def collect(cfg,ns,pod,uid,cached,progress):
        order.append('logs')
        report={'records':[{'id':'E01','check':'Pod status and logs','status':'ok','data':{'message':'EmptyDir size limit'}}],'pod':{'message':'EmptyDir size limit'},'findings':[{'source':'E01','fact':'EmptyDir size limit exceeded'}],'unavailable':0}
        progress(report);return report
    def search(self,query,cid,**kw):
        order.append('knowledge');assert kw['kubernetes_version']=='1.30'
        return [{'id':'D01','source':'https://kubernetes.io/docs/','version':'1.30','excerpt':'EmptyDir sizeLimit'}],'matches 1'
    def llm(cfg,messages):
        order.append('model');text=json.dumps(messages)
        assert 'E01' in text and 'D01' in text and 'EmptyDir' in text
        return 'Подтверждено сообщением Pod [E01].'
    monkeypatch.setattr(diagnostics,'collect_pod',collect);monkeypatch.setattr(Knowledge,'search',search);monkeypatch.setattr(connectors,'llm_chat',llm)
    sid=session(client,'one','problem');job=message(client,sid,'В чем проблема?',problem_id=issue['id'])
    assert job['status']=='done' and order==['logs','knowledge','model']
    assert client.get('/api/investigations').json()['items']==[]


def test_local_incomplete_answer_can_be_continued_without_false_done(client,monkeypatch):
    from pulse_app.astra_ai import IncompleteAnswer
    configure(client);calls=[]
    def llm(cfg,messages):
        calls.append(messages)
        if len(calls)==1:raise IncompleteAnswer('token limit','Незавершённый разбор')
        assert any(m['role']=='assistant' and 'Незавершённый разбор' in m['content'] for m in messages)
        assert 'Qdrant не настроен' in messages[-1]['content']
        return 'Продолженный ответ'
    monkeypatch.setattr(connectors,'llm_chat',llm)
    sid=session(client);first=message(client,sid)
    assert first['status']=='failed' and first['answer']=='Незавершённый разбор'
    second=message(client,sid,'Продолжи ответ')
    assert second['status']=='done' and second['previous_id']==first['id']
