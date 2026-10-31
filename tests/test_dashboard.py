import time
import httpx
import pytest
from pulse_app import connectors
from pulse_app.dashboard import summarize, combine, workload_state
from test_api import client, runtime, add, fresh_snapshot, MUTATION


def test_ready_semantics_do_not_equate_running_with_ready():
    assert workload_state('pods', {'status':{'phase':'Running'}}) == 'not_ready'
    assert workload_state('pods', {'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}) == 'ready'
    assert workload_state('jobs', {'status':{'active':1}}) == 'active'
    assert workload_state('jobs', {'status':{'conditions':[{'type':'Complete','status':'True'}]}}) == 'completed'
    assert workload_state('cronjobs', {'spec':{'suspend':True}}) == 'suspended'
    assert workload_state('deployments', {'spec':{'replicas':0}}) == 'scaled_zero'
    assert workload_state('deployments', {'metadata':{'generation':3},'spec':{'replicas':2},'status':{'observedGeneration':2,'readyReplicas':2}}) == 'updating'


def test_empty_missing_partial_and_failed_are_distinct():
    snap=fresh_snapshot()
    snap['resources']['jobs']={'items':[],'error':None,'truncated':False}
    snap['resources']['deployments']={'items':[{'spec':{'replicas':1},'status':{'readyReplicas':1}}],'error':None,'truncated':True}
    a=summarize(snap,'one','One')
    assert a['workloads']['jobs']['total']==0
    assert a['workloads']['jobs']['percent'] is None
    assert a['workloads']['cronjobs']['total'] is None
    assert a['workloads']['deployments']['percent']==100
    assert a['workloads']['deployments']['partial']
    b=summarize({'resources':{}},'two','Two')
    result=combine([a,b])
    assert result['deployments']['partial'] and result['deployments']['total']==1


def test_dashboard_scope_and_missing_source(client, monkeypatch):
    add(client,'one');add(client,'two')
    monkeypatch.setattr(connectors,'collect_cluster',lambda cfg:fresh_snapshot())
    client.post('/api/clusters/one/refresh',headers=MUTATION)
    response=client.get('/api/dashboard?cluster_id=one')
    assert response.status_code==200
    assert [r['id'] for r in response.json()['clusters']]==['one']
    assert client.get('/api/dashboard?cluster_id=bad').status_code==404
    assert not client.get('/api/clusters/one/dashboard-metrics').json()['configured']
    assert client.get('/api/clusters/one/dashboard-metrics?window=99h').status_code==422


def test_query_scope_ratios_history_and_nonfinite(monkeypatch):
    seen=[]
    def request(client,method,url,params):
        seen.append((url,params))
        if url.endswith('/query_range'):
            return {'status':'success','data':{'resultType':'matrix','result':[{'values':[[time.time()-60,'NaN'],[time.time()-30,'35']]}]}}
        return {'status':'success','data':{'resultType':'vector','result':[{'value':[time.time(),'42.25']}]}}
    monkeypatch.setattr(connectors,'_request',request)
    result=connectors.dashboard_metrics({'url':'https://metrics.test','labels':{'cluster':'one'}},'6h')
    assert len(seen)==9
    assert all('cluster="one"' in params['query'] for url,params in seen)
    assert all(row['percent']==42.25 and len(row['history'])==1 for row in result['metrics'].values())
    assert all(row['history_error'] for row in result['metrics'].values())
    assert any('mountpoint="/"' in params['query'] for url,params in seen)
    assert any('and on(instance)' in params['query'] for url,params in seen)


@pytest.mark.parametrize('labels',[{}, {'instance':'node'}, {'cluster':12}])
def test_ratio_scope_rejects_unscoped_or_invalid_labels(labels):
    with pytest.raises(connectors.ConnectorError):
        connectors.utilization_expressions({'labels':labels})


def test_history_empty_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(connectors,'_request',lambda client,method,url,params:{'status':'success','data':{'resultType':'matrix' if url.endswith('query_range') else 'vector','result':[]}})
    result=connectors.dashboard_metrics({'url':'https://metrics.test','mode':'dedicated'})
    assert all(row['percent'] is None and row['history']==[] for row in result['metrics'].values())


@pytest.mark.parametrize('payload',[
    [], None, {'status':'success','data':[]},
    {'status':'success','data':{'resultType':'vector','result':{}}},
    {'status':'success','data':{'resultType':'vector','result':[None]}},
    {'status':'success','data':{'resultType':'vector','result':[{'value':[]}]}},
])
def test_malformed_metrics_are_source_errors_not_dashboard_crashes(monkeypatch,payload):
    monkeypatch.setattr(connectors,'_request',lambda *args,**kwargs:payload)
    result=connectors.dashboard_metrics({'url':'https://metrics.test','mode':'dedicated'})
    assert all(row['percent'] is None and row['error'] and row['history_error'] and row['nodes_error'] for row in result['metrics'].values())


def test_instant_freshness_does_not_expand_with_history_window(monkeypatch):
    timestamp=time.time()-1800
    def request(client,method,url,params):
        history=url.endswith('query_range')
        row={'metric':{'instance':'node:9100'},'values' if history else 'value':[[timestamp,'25']] if history else [timestamp,'25']}
        return {'status':'success','data':{'resultType':'matrix' if history else 'vector','result':[row]}}
    monkeypatch.setattr(connectors,'_request',request)
    result=connectors.dashboard_metrics({'url':'https://metrics.test','mode':'dedicated'},'24h')
    for row in result['metrics'].values():
        assert row['percent'] is None and row['error']
        assert row['nodes']==[] and row['nodes_error']
        assert row['history']==[[timestamp,25]] and not row['history_error']


@pytest.mark.parametrize('number',['NaN','+Inf','-1','101',True])
def test_invalid_percentages_have_explicit_errors(monkeypatch,number):
    def request(client,method,url,params):
        history=url.endswith('query_range');point=[time.time(),number]
        row={'metric':{'instance':'node:9100'},'values' if history else 'value':[point] if history else point}
        return {'status':'success','data':{'resultType':'matrix' if history else 'vector','result':[row]}}
    monkeypatch.setattr(connectors,'_request',request)
    result=connectors.dashboard_metrics({'url':'https://metrics.test','mode':'dedicated'})
    assert all(row['percent'] is None and row['error'] and row['history_error'] and row['nodes_error'] for row in result['metrics'].values())


def test_source_warnings_and_node_limit_are_not_silent(monkeypatch):
    settings={'url':'https://metrics.test','mode':'dedicated'}
    node_queries=set(connectors.utilization_expressions(settings,True).values())
    def request(client,method,url,params):
        if params['query'] in node_queries:
            return {'status':'success','data':{'resultType':'vector','result':[{'metric':{'instance':f'node-{i}'},'value':[time.time(),'20']} for i in range(501)]}}
        history=url.endswith('query_range')
        return {'status':'success','warnings':['private source details'],'data':{'resultType':'matrix' if history else 'vector','result':[]}}
    monkeypatch.setattr(connectors,'_request',request)
    result=connectors.dashboard_metrics(settings)
    for row in result['metrics'].values():
        assert 'warnings' in row['error'] and 'warnings' in row['history_error']
        assert 'private' not in row['error']
        assert row['nodes']==[] and '500' in row['nodes_error']
