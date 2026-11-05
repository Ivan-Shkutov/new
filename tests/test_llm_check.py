import json
import pytest
from pulse_app import astra_ai, connectors
from pulse_app.llm_check import CASES, run_cases
from test_api import client, runtime, MUTATION
from test_sessions import configure


def test_live_check_uses_local_engine_policy_and_removes_temporary_sessions(client,monkeypatch):
    configure(client);calls=[];sessions=[];jobs=[];deleted=[]
    def model(cfg,messages):
        calls.append(messages)
        assert cfg['model']=='qwen' and 'Senior DevOps/SRE' in messages[0]['content']
        return 'Контрольный ответ HTTP-стенда, не оценка семантики.'
    monkeypatch.setattr(connectors,'llm_chat',model)
    monkeypatch.setattr(astra_ai,'chat',lambda *args:pytest.fail('Local check must not call Astra'))
    def api(method,path,body=None):
        response=client.request(method,path,json=body,headers=MUTATION)
        assert response.status_code<400,response.text
        data=response.json()
        if method=='POST' and path=='/api/sessions':
            assert body=={'mode':'chat','engine':'local'};sessions.append(data['id'])
        elif method=='POST':jobs.append(data['id'])
        elif method=='DELETE':deleted.append(path.rsplit('/',1)[-1])
        return data
    report=run_cases(api,interval=.01)
    assert report['generation_ok'] and report['quality_verified'] is False
    assert len(calls)==len(report['cases'])==len(CASES)==9
    assert deleted==sessions
    for sid,jid in zip(sessions,jobs):assert client.get(f'/api/sessions/{sid}/jobs/{jid}').status_code==404
    assert all(row['review'] and row['policy_applied'] for row in report['cases'])
    assert client.get('/api/investigations').json()['items']==[]


def test_failed_generation_is_reported_without_a_quality_pass_and_session_is_closed():
    requests=[]
    def api(method,path,body=None):
        requests.append((method,path))
        if path=='/api/sessions':return {'id':'s'}
        if path.endswith('/messages'):return {'id':'j'}
        if method=='DELETE':return {'ok':True}
        return {'status':'failed','answer':'','error':'Локальная модель не ответила','trace':'[]'}
    report=run_cases(api,['exit137'])
    assert not report['generation_ok'] and not report['quality_verified']
    assert requests[-1]==('DELETE','/api/sessions/s')
    assert report['cases'][0]['error']=='Локальная модель не ответила'


def test_timeout_removes_session_and_keeps_report():
    removed=[]
    def api(method,path,body=None):
        if path=='/api/sessions':return {'id':'s'}
        if path.endswith('/messages'):return {'id':'j'}
        if method=='DELETE':removed.append(path);return {'ok':True}
        return {'status':'running'}
    report=run_cases(api,['service'],deadline=0)
    assert not report['generation_ok'] and removed==['/api/sessions/s']
    assert 'Истёк срок' in report['cases'][0]['error']
