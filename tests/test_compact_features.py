import json
import time
import pytest
from pulse_app import connectors
from test_api import client,runtime,add,MUTATION
from test_health import snapshot,stamp,pod


def wait(client,jid):
    for _ in range(100):
        job=client.get('/api/investigations/'+jid).json()
        if job['status'] not in ('queued','running'):return job
        time.sleep(.02)
    raise AssertionError('job did not finish')


def test_all_problems_are_paginated_scoped_and_searchable(client,monkeypatch):
    add(client);add(client,'two');s=snapshot()
    s['resources']['pods']['items']=[pod('Pending',ready='False',seconds=600) for _ in range(80)]
    for i,p in enumerate(s['resources']['pods']['items']):p['metadata']['name']=f'web-{i:03d}'
    monkeypatch.setattr(connectors,'collect_cluster',lambda cfg:s)
    client.post('/api/clusters/one/refresh',headers=MUTATION)
    a=client.get('/api/problems?cluster_id=one&limit=25').json()
    b=client.get('/api/problems?cluster_id=one&limit=25&offset=25').json()
    assert a['total']==80 and len(a['items'])==len(b['items'])==25
    assert {i['id'] for i in a['items']}.isdisjoint({i['id'] for i in b['items']})
    assert client.get('/api/problems?cluster_id=two').json()['total']==0
    assert client.get('/api/problems?q=web-079').json()['total']==1
    assert client.get('/api/problems?severity=warning').json()['total']==0
    assert client.get('/api/problems?limit=9999').status_code==422


def test_issue_context_trace_and_followup_survive_resolution(client,monkeypatch):
    add(client);s=snapshot();s['resources']['pods']['items']=[pod(ready='False',reason='CrashLoopBackOff')]
    monkeypatch.setattr(connectors,'collect_cluster',lambda cfg:s)
    client.post('/api/clusters/one/refresh',headers=MUTATION)
    problem=client.get('/api/problems').json()['items'][0]
    client.put('/api/settings',json={'llm':{'url':'http://localhost:8002/v1','model':'qwen'}},headers=MUTATION)
    messages=[]
    monkeypatch.setattr(connectors,'resource_logs',lambda *a,**k:'readiness failed')
    def llm(cfg,m):messages.append(m);return 'Наблюдение: CrashLoop. Причина пока не установлена.'
    monkeypatch.setattr(connectors,'llm_chat',llm)
    r=client.post('/api/investigations',json={'cluster_id':'one','question':'В чём причина?','problem_id':problem['id']},headers=MUTATION)
    job=wait(client,r.json()['id'])
    assert job['status']=='done'
    assert json.loads(job['context'])['id']==problem['id']
    assert any(t['tool']=='skill:crashloop' for t in json.loads(job['trace']))
    assert 'не выдумывай' in messages[0][0]['content'].lower()
    s['resources']['pods']['items']=[];client.post('/api/clusters/one/refresh',headers=MUTATION)
    assert client.post('/api/investigations',json={'cluster_id':'one','question':'Почему?','problem_id':problem['id']},headers=MUTATION).status_code==409
    follow=client.post('/api/investigations',json={'cluster_id':'one','question':'Что проверить?','previous_id':job['id']},headers=MUTATION)
    assert wait(client,follow.json()['id'])['status']=='done'
    assert any(m['role']=='assistant' for m in messages[-1])


def test_metric_detail_uses_allowlisted_queries(client,monkeypatch):
    add(client,metrics={'url':'https://metrics.test','mode':'dedicated'})
    monkeypatch.setattr(connectors,'dashboard_metrics',lambda cfg,w:{'metrics':{},'window':w})
    called=[]
    def query(cfg,metric):called.append(metric);return {'data':{'result':[]}}
    monkeypatch.setattr(connectors,'query_metrics',query)
    assert client.get('/api/clusters/one/metrics-detail?metric=ram').status_code==200
    assert set(called)=={'node_memory','node_memory_total','memory'}
    assert client.get('/api/clusters/one/metrics-detail?metric=arbitrary-promql').status_code==422


def test_holmes_bundle_scopes_token_hides_keys_and_does_not_enable_agent(client,runtime):
    add(client);add(client,'two')
    client.put('/api/settings',json={'llm':{'url':'http://127.0.0.1:8002/v1','model':'qwen','api_key':'model-secret'}},headers=MUTATION)
    result=client.post('/api/clusters/one/holmes-bootstrap',json={'port':5051},headers=MUTATION)
    assert result.status_code==200 and not result.json()['enabled']
    root=runtime/'holmes/one'
    config=json.loads((root/'http-toolset.json').read_text())
    endpoint=config['config']['endpoints'][0]
    assert endpoint['methods']==['GET'] and all(p.startswith('/agent/one/') for p in endpoint['paths'])
    token=config['config']['default_headers']['Authorization']
    assert token not in result.text and 'model-secret' not in client.get('/api/clusters').text
    assert json.loads((root/'compose.yaml').read_text())['services']['holmes']['volumes']==['.:/bundle:ro']
    assert len(list((root/'skills').glob('*/SKILL.md')))==9
    assert (root/'model_list.json').stat().st_mode&0o077==0
    assert client.get('/agent/one/snapshot',auth=None).status_code==401
    assert client.get('/agent/one/snapshot',auth=None,headers={'Authorization':token}).status_code==200
    assert client.get('/agent/two/snapshot',auth=None,headers={'Authorization':token}).status_code==401
    assert client.get('/agent/one/snapshot?kind=secrets',auth=None,headers={'Authorization':token}).status_code==422
    assert client.post('/api/clusters/one/holmes-bootstrap',json={'port':5051},headers=MUTATION).status_code==409


def test_holmes_protocol_is_distinct_and_tools_do_not_leak_arguments(monkeypatch):
    seen=[]
    def request(client,method,url,json=None,**kwargs):
        if url.endswith('/api/info'):return {'auth_enabled':True,'toolsets':[{'name':'pulse-readonly','enabled':True,'status':'enabled'}]}
        seen.append((url,json));return {'analysis':'Короткий ответ','tool_calls':[{'tool_name':'pulse-readonly','status':'success','arguments':{'token':'do-not-leak'}}]}
    monkeypatch.setattr(connectors,'_request',request)
    answer,trace=connectors.holmes_chat({'url':'http://localhost:5051','model':'pulse-local'},[{'role':'system','content':'safe'},{'role':'user','content':'ask'}])
    assert answer=='Короткий ответ' and seen[0][0].endswith('/api/chat')
    assert seen[0][1]['ask']=='ask' and not seen[0][1]['stream']
    assert 'do-not-leak' not in json.dumps(trace)


def test_enabled_holmes_is_used_without_fallback(client,monkeypatch):
    add(client,holmes={'enabled':True,'url':'http://localhost:5051','model':'pulse-local'})
    called=[]
    monkeypatch.setattr(connectors,'holmes_chat',lambda cfg,m:(called.append(True) or 'Holmes ответил',[{'tool':'read-only','status':'ok'}]))
    monkeypatch.setattr(connectors,'llm_chat',lambda *a:pytest.fail('silent fallback'))
    response=client.post('/api/investigations',json={'cluster_id':'one','question':'Проверь состояние'},headers=MUTATION)
    assert wait(client,response.json()['id'])['answer']=='Holmes ответил'
    assert called



def test_holmes_refuses_extra_tools_before_chat(monkeypatch):
    calls=[]
    def request(client,method,url,**kwargs):
        calls.append(url)
        return {'auth_enabled':True,'toolsets':[{'name':n,'enabled':True,'status':'enabled'} for n in ('pulse-readonly','bash')]}
    monkeypatch.setattr(connectors,'_request',request)
    with pytest.raises(connectors.ConnectorError,match='tools'):
        connectors.holmes_chat({'url':'http://localhost:5051'},[{'role':'system','content':'x'},{'role':'user','content':'x'}])
    assert all(not url.endswith('/api/chat') for url in calls)


def test_holmes_challenge_requires_real_tool_result(client,runtime,monkeypatch):
    add(client)
    client.put('/api/settings',json={'llm':{'url':'http://localhost:8002/v1','model':'qwen'}},headers=MUTATION)
    client.post('/api/clusters/one/holmes-bootstrap',json={'port':5051},headers=MUTATION)
    token=json.loads((runtime/'holmes/one/http-toolset.json').read_text())['config']['default_headers']['Authorization']
    assert client.get('/agent/one/challenge',auth=None,headers={'Authorization':token}).status_code==404
    def fake(settings,messages):
        value=client.get('/agent/one/challenge',auth=None,headers={'Authorization':token}).json()['nonce']
        assert value not in json.dumps(messages)
        return value,[{'tool':'pulse-readonly','status':'success'}]
    monkeypatch.setattr(connectors,'holmes_chat',fake)
    response=client.post('/api/clusters/one/holmes-accept',headers=MUTATION)
    assert response.status_code==200 and response.json()['tool_call_verified']
    monkeypatch.setattr(connectors,'holmes_chat',lambda *args:('Модель просто ответила',[]))
    assert not client.post('/api/clusters/one/holmes-accept',headers=MUTATION).json()['tool_call_verified']


def test_context_redaction_keeps_valid_json_and_history_filter(client,runtime):
    add(client)
    # Access persistence without creating a second Store (which would alter live job states).
    import sqlite3
    value=connectors.redact_structure({'id':'problem-one','detail':'password="private-value"','Authorization':'Bearer hidden'})
    text=json.dumps(value)
    assert 'private-value' not in text and 'hidden' not in text
    with sqlite3.connect(runtime/'pulse.sqlite3') as c:
        for i in range(110):
            ctx=text if i==0 else '{"id":"different-problem"}'
            c.execute("INSERT INTO investigations(id,cluster_id,question,status,created_at,context) VALUES (?,?,?,?,?,?)",(str(i),'one','test','done',str(i).zfill(5),ctx))
    response=client.get('/api/investigations?cluster_id=one&problem_id=problem-one')
    assert response.status_code==200 and [r['id'] for r in response.json()['items']]==['0']


def test_upgrade_old_database_preserves_history_and_encryption(tmp_path):
    import sqlite3
    from pulse_app.store import Store
    old=Store(tmp_path)
    old.execute('INSERT INTO settings VALUES (1,?)',(old.encrypt({'llm':{'model':'qwen','api_key':'private'}}),))
    original_key=(tmp_path/'registry.key').read_bytes()
    with sqlite3.connect(old.path) as c:
        c.execute('DROP TABLE investigations')
        c.execute("CREATE TABLE investigations (id TEXT PRIMARY KEY,cluster_id TEXT NOT NULL,question TEXT NOT NULL,status TEXT NOT NULL,answer TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,previous_id TEXT NOT NULL DEFAULT '')")
        c.execute("INSERT INTO investigations(id,cluster_id,question,status,answer,created_at) VALUES ('old','one','Почему?','done','Прежний ответ','2026-10-29')")
    upgraded=Store(tmp_path)
    job=upgraded.rows('SELECT * FROM investigations')[0]
    assert job['answer']=='Прежний ответ' and job['status']=='done'
    assert json.loads(job['context'])=={} and json.loads(job['trace'])=={}
    assert (tmp_path/'registry.key').read_bytes()==original_key
    assert upgraded.settings()['llm']['api_key']=='private'
