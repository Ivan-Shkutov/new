import json
import pytest
from pulse_app import connectors, diagnostics, astra_ai
from pulse_app.store import Store
from test_api import client, runtime, add, MUTATION
from test_sessions import configure, message
from test_diagnostics import failed_pod
from test_analysis_fix import report


@pytest.mark.parametrize('engine',['local','cluster'])
def test_problem_dialog_sends_facts_and_keeps_only_model_reply_in_followup(client,runtime,monkeypatch,engine):
    configure(client)
    add(client,astra_ai={'enabled':True,'url':'http://astra.test/chat/completions','model':'qwen','api_key':'fixture'})
    pod=failed_pod()
    Store(runtime).execute('INSERT INTO snapshots VALUES (?,?)',('one',json.dumps({'complete':True,'resources':{'pods':{'items':[pod]}}})))
    issue=client.get('/api/problems?cluster_id=one').json()['items'][0]
    monkeypatch.setattr(diagnostics,'collect_pod',lambda *args:report())
    calls=[]
    reply='| Параметр | Значение |\n| --- | --- |\n| Причина | EmptyDir data превысил 1Gi [E01]. |'
    def model(cfg,messages,*args):calls.append(messages);return reply
    monkeypatch.setattr(connectors,'llm_chat',model)
    monkeypatch.setattr(astra_ai,'chat',model)
    sid=client.post('/api/sessions',json={'cluster_id':'one','mode':'problem','engine':engine},headers=MUTATION).json()['id']
    first=message(client,sid,'Дай сжатый анализ',problem_id=issue['id'],mode='brief')
    assert first['status']=='done' and 'Проверенные данные Pulse' in first['answer']
    assert 'Параметр | Значение' in calls[0][0]['content']
    second=message(client,sid,'Что проверить первым?',mode='brief')
    assert second['status']=='done' and second['previous_id']==first['id']
    assert next(m['content'] for m in calls[-1] if m['role']=='assistant')==reply
    assert 'EmptyDir' in calls[-1][-1]['content'] and 'E01' in calls[-1][-1]['content']
    assert 'не повторяй весь отчёт' in calls[-1][0]['content']
    assert 'SendText' in calls[-1][0]['content'] and 'не доказывают успешную доставку' in calls[-1][0]['content']
    assert client.get('/api/investigations').json()['items']==[]
