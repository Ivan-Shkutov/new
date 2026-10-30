"""Regression tests only for Astra 401 and the reported EmptyDir incident."""
import json
import httpx
import pytest
from pulse_app import astra_ai,connectors as c,diagnostics as d
from pulse_app.store import Store
from test_api import client,runtime,add,MUTATION
from test_astra_ai import SETTINGS,MESSAGES,event,response
from test_connectors import install_transport
from test_diagnostics import failed_pod
from test_sessions import configure,message


def report():
    return {'pod':{'reason':'Evicted','message':'Usage of EmptyDir volume "data" exceeds the limit "1Gi".','node':'worker-2','source_type':'live'},
            'records':[{'id':'E01','check':'Pod status','status':'ok','data':{'reason':'Evicted'}},{'id':'E02','check':'Previous logs','status':'unavailable','error':'Upstream HTTP 404'}],
            'findings':[{'source':'E01','fact':'Evicted: EmptyDir data limit 1Gi exceeded.'}],'unavailable':1}


@pytest.mark.parametrize('value',['test-ai-token',' Bearer test-ai-token ','bearer test-ai-token'])
def test_bearer_header_is_normalized_only_for_astra_ai(monkeypatch,value):
    def handler(request):
        assert request.headers['authorization']=='Bearer test-ai-token'
        return response(event('Ответ.')+event(finish='stop')+'data: [DONE]\n\n')
    install_transport(monkeypatch,handler)
    cfg={**SETTINGS,'api_key':value,'token':'wrong-log-token'}
    assert astra_ai.chat(cfg,MESSAGES)=='Ответ.'
    assert cfg['api_key']==value  # Do not mutate encrypted config or log credentials.


def test_401_keeps_authorization_hint_without_leaking_body(monkeypatch):
    install_transport(monkeypatch,lambda request:httpx.Response(401,text='PRIVATE-UPSTREAM-BODY'))
    with pytest.raises(astra_ai.IncompleteAnswer) as error:astra_ai.chat(SETTINGS,MESSAGES)
    assert '401' in str(error.value) and 'Bearer token именно Astra AI' in str(error.value)
    assert 'PRIVATE' not in str(error.value) and SETTINGS['api_key'] not in str(error.value)
    assert not error.value.partial


def test_confirmed_emptydir_cause_and_observable_answer_omissions():
    data=report();assert d.emptydir_cause(data)=={'volume':'data','limit':'1Gi'}
    assert 'превышен лимит EmptyDir `data` — `1Gi`' in d.verified_summary(data)
    assert d.review_answer(data,'Недостаточно ресурсов.')
    assert d.review_answer(data,'EmptyDir data 1Gi [E99]')
    assert not d.review_answer(data,'Evicted: EmptyDir data превысил 1Gi [E01]. Причина роста файлов ещё не установлена.')
    data['pod']['reason']='Unknown';assert d.emptydir_cause(data) is None


def test_401_preserves_proof_then_explicit_local_retry_uses_same_problem(client,runtime,monkeypatch):
    configure(client);add(client,astra_ai={**SETTINGS,'enabled':True})
    pod=failed_pod();pod['status']['message']=report()['pod']['message']
    Store(runtime).execute('INSERT INTO snapshots VALUES (?,?)',('one',json.dumps({'complete':True,'resources':{'pods':{'items':[pod]}}})))
    issue=client.get('/api/problems?cluster_id=one').json()['items'][0]
    monkeypatch.setattr(d,'collect_pod',lambda *args:report())
    def denied(*args):raise astra_ai.IncompleteAnswer('Astra AI HTTP 401; ответ не считается завершённым')
    monkeypatch.setattr(astra_ai,'chat',denied)
    calls=[]
    def local(cfg,messages):
        calls.append(messages)
        assert 'EmptyDir' in messages[-1]['content'] and '1Gi' in messages[-1]['content'] and 'E01' in messages[-1]['content']
        return 'Evicted: EmptyDir data превысил 1Gi [E01]. Причина роста файлов не доказана.'
    monkeypatch.setattr(c,'llm_chat',local)
    def start(engine):return client.post('/api/sessions',json={'cluster_id':'one','mode':'problem','engine':engine},headers=MUTATION).json()['id']
    first=message(client,start('cluster'),'В чём проблема?',problem_id=issue['id'])
    assert first['status']=='failed' and '401' in first['error'] and '1Gi' in first['answer']
    assert json.loads(first['evidence'])['unavailable']==1 and not calls
    retry=message(client,start('local'),'В чём проблема?',problem_id=issue['id'])
    assert retry['status']=='done' and len(calls)==1
    assert next(t for t in json.loads(retry['trace']) if t['tool']=='answer-review')['status']=='checked'
    assert client.get('/api/clusters').json()['items'][0]['astra_ai']['enabled']  # No global fallback or source changes.
    assert client.get('/api/investigations').json()['items']==[]
