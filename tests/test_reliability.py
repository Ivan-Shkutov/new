"""Regression cases for source failures and evidence-selected investigation paths."""
import httpx
import pytest
from pulse_app.connectors import ConnectorError
from pulse_app import connectors, diagnostics
from test_api import client, runtime, add, MUTATION, wait_job
from test_sessions import configure, session, message
from test_analysis_fix import report
from test_connectors import install_transport
from test_knowledge import setup


def test_runbook_selected_from_live_pod_even_without_problem_id(client, monkeypatch):
    configure(client);add(client)
    monkeypatch.setattr(diagnostics,'collect_pod',lambda *a:report())
    captured=[]
    monkeypatch.setattr(connectors,'llm_chat',lambda cfg,messages:(captured.append(messages),'EmptyDir data 1Gi [E01]')[1])
    response=client.post('/api/investigations',headers=MUTATION,json={'cluster_id':'one','question':'Что произошло?','namespace':'am-agent','pod':'bad'})
    assert response.status_code==202
    result=wait_job(client,response.json()['id'])
    assert result['status']=='done'
    assert 'Выселение Pod' in captured[0][0]['content']


@pytest.mark.parametrize('payload',[[],None,{'status':{'error':'unavailable'}}])
def test_knowledge_http_bad_response_is_source_error(tmp_path,monkeypatch,payload):
    kb,_,_=setup(tmp_path,monkeypatch)
    from pulse_app.knowledge import Knowledge
    install_transport(monkeypatch,lambda request:httpx.Response(200,json=payload))
    with pytest.raises(ConnectorError):Knowledge.request(kb,'POST','/collections/test/points?wait=true',{})


def test_knowledge_missing_write_target_is_not_success(tmp_path,monkeypatch):
    kb,_,_=setup(tmp_path,monkeypatch)
    from pulse_app.knowledge import Knowledge
    install_transport(monkeypatch,lambda request:httpx.Response(404,json={'status':{'error':'missing'}}))
    assert Knowledge.request(kb,'GET','/collections/test') is None
    with pytest.raises(ConnectorError):Knowledge.request(kb,'PUT','/collections/test/points?wait=true',{})


def test_knowledge_http_failure_keeps_status_without_upstream_secrets(tmp_path,monkeypatch):
    kb,_,_=setup(tmp_path,monkeypatch)
    from pulse_app.knowledge import Knowledge
    install_transport(monkeypatch,lambda request:httpx.Response(500,json={'error':'token=private-value'}))
    with pytest.raises(ConnectorError,match='Qdrant HTTP 500') as exc:
        Knowledge.request(kb,'PUT','/collections/test/points?wait=true',{})
    assert 'private-value' not in str(exc.value)


@pytest.mark.parametrize('payload',[{}, {'result':None}, {'result':[]}, {'result':{'config':None}}, {'result':{'config':{'params':{'vectors':[]}}}}])
def test_invalid_qdrant_metadata_does_not_crash_status_or_report_ready(tmp_path,monkeypatch,payload):
    kb,_,_=setup(tmp_path,monkeypatch)
    monkeypatch.setattr(kb,'request',lambda *a,**kw:payload)
    assert kb.status()['qdrant']=='unavailable'
    with pytest.raises(ConnectorError):kb.ensure()


def test_invalid_embedding_catalogue_is_an_explicit_source_error(tmp_path,monkeypatch):
    kb,_,_=setup(tmp_path,monkeypatch);kb.revision='pinned'
    monkeypatch.setattr(kb,'request',lambda *a,**kw:{'models':[None]})
    with pytest.raises(ConnectorError):kb.embed(['query'])


@pytest.mark.parametrize('payload',[[],{'result':None},{'result':{'points':'bad'}},{'result':{'points':[None]}}])
def test_knowledge_invalid_search_is_not_empty_result(tmp_path,monkeypatch,payload):
    kb,_,_=setup(tmp_path,monkeypatch)
    monkeypatch.setattr(kb,'embed',lambda texts:[[1,0,0]])
    monkeypatch.setattr(kb,'request',lambda *a,**kw:payload)
    with pytest.raises(ConnectorError):kb.search('why','one')


def test_dashboard_rechecks_node_identity_after_connection_saved(client,monkeypatch):
    import time
    from pulse_app import cluster_chat
    add(client,metrics={'url':'http://metrics.test','mode':'dedicated'})
    point={'percent':40,'observed_at':time.time(),'nodes':[{'instance':'foreign:9100','percent':40,'observed_at':time.time()}],'history':[[time.time(),40]]}
    monkeypatch.setattr(connectors,'dashboard_metrics',lambda *a:{'metrics':{k:point for k in ('cpu','ram','disk')}})
    monkeypatch.setattr(cluster_chat,'live_list',lambda *a:{'items':[{'metadata':{'name':'worker'}}],'complete':True,'error':None})
    data=client.get('/api/clusters/one/dashboard-metrics').json()
    assert data['status']=='partial' and data['expected_nodes']==1
    for value in data['metrics'].values():
        assert value['percent'] is None and value['history']==[]
        assert value['coverage']['foreign']==['foreign:9100']


def test_concurrent_settings_change_is_not_overwritten(client,runtime,monkeypatch):
    from test_api import kubeconfig
    from pulse_app.store import Store
    add(client)
    store=Store(runtime);original=connectors.validate_kubeconfig
    def validate(text):
        config=store.cluster('one')['config']
        config['metrics']={'url':'http://new-metrics.test','mode':'dedicated'}
        store.execute('UPDATE clusters SET config=? WHERE id=?',(store.encrypt(config),'one'))
        return original(text)
    monkeypatch.setattr(connectors,'validate_kubeconfig',validate)
    response=client.put('/api/clusters/one',json={'kubeconfig':kubeconfig()},headers=MUTATION)
    assert response.status_code==409
    assert store.cluster('one')['config']['metrics']['url']=='http://new-metrics.test'
