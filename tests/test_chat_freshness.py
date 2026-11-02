import json
import pytest
from test_api import client, runtime, add, MUTATION, wait_job
from test_sessions import configure, session, message
from pulse_app import connectors, diagnostics
from test_analysis_fix import report


@pytest.mark.parametrize('mode',['chat','problem'])
def test_new_volatile_dialog_never_injects_past_incident_solutions(client,runtime,monkeypatch,mode):
    import sqlite3
    configure(client);add(client)
    with sqlite3.connect(runtime/'pulse.sqlite3') as db:
        db.execute('INSERT INTO incidents VALUES (?,?,?,?,?,?)',('old','one','OLD-QUESTION','OLD-ANSWER','OLD-SOLUTION','2026-01-01'))
    calls=[]
    monkeypatch.setattr(connectors,'llm_chat',lambda cfg,messages:(calls.append(messages),'Current answer')[1])
    sid=session(client,'one',mode)
    assert message(client,sid,'Объясни текущую проблему')['status']=='done'
    assert 'OLD-' not in json.dumps(calls[-1])
    assert client.delete('/api/sessions/'+sid,headers=MUTATION).status_code==200
    newer=session(client,'one',mode)
    assert message(client,newer,'Другой вопрос')['previous_id']==''
    assert 'Current answer' not in json.dumps(calls[-1])
    with sqlite3.connect(runtime/'pulse.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM incidents').fetchone()[0]==1


def test_impact_facts_are_available_even_when_ai_is_unavailable():
    data=report()
    data['pod']['phase']='Failed'
    answer=diagnostics.verified_summary(data,'impact')
    assert 'терминальную фазу Failed' in answer
    assert 'Возможные последствия' in answer
    assert 'Факт потери данных и длительность перерыва не установлены' in answer
    assert 'отказ всего кластера' in answer and 'не подтверждены' in answer
    assert 'Последствия: оценка Pulse' not in diagnostics.verified_summary(data)


def test_astra_403_retains_impact_facts_without_claiming_ai_success(client,monkeypatch):
    from pulse_app import astra_ai
    add(client,astra_ai={'enabled':True,'url':'http://astra.test/chat','model':'qwen','api_key':'fixture'})
    data=report();data['pod']['phase']='Failed'
    monkeypatch.setattr(diagnostics,'collect_pod',lambda *a,**kw:data)
    def forbidden(*a,**kw):raise astra_ai.IncompleteAnswer('Astra AI HTTP 403','')
    monkeypatch.setattr(astra_ai,'chat',forbidden)
    response=client.post('/api/investigations',json={'cluster_id':'one','namespace':'am-agent','pod':'bad','question':'Последствия?','mode':'impact'},headers=MUTATION)
    job=wait_job(client,response.json()['id'])
    assert job['status']=='failed' and '403' in job['error']
    assert 'Последствия: оценка Pulse' in job['answer']
    assert 'Возможные последствия' in job['answer']


def test_astra_mapping_is_read_back_without_credentials(client):
    source={'url':'http://astra.test','token':'PRIVATE-LOG-TOKEN','cluster_field':'labels.cluster',
            'cluster_value':'one','namespace_field':'labels.pod_namespace','pod_field':'labels.pod_name'}
    add(client,astra=source)
    reply=client.get('/api/clusters')
    public=reply.json()['items'][0]['astra']
    assert public['has_token'] and public['pod_field']==source['pod_field']
    assert public['url']==source['url'] and 'PRIVATE-LOG-TOKEN' not in reply.text
