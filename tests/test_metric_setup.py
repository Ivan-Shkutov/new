import time
import httpx
import pytest
from pulse_app import connectors as c
from pulse_app import metric_setup as setup
from test_api import client,runtime,add,MUTATION,TOKEN
from test_connectors import install_transport

SOURCE={'transport':'kubernetes','namespace':'monitoring','service':'kube-prom-kube-prometheus-prometheus','port':9090,'mode':'dedicated','labels':{},'url':''}


def upstream(request):
    assert request.headers['authorization']=='Bearer '+TOKEN
    path=request.url.path;now=time.time()
    if path=='/api/v1/nodes':return httpx.Response(200,json={'items':[{'metadata':{'name':'node-'+str(i)},'status':{'addresses':[{'type':'InternalIP','address':'10.0.0.'+str(i)}]}} for i in range(1,7)]})
    assert path.startswith('/api/v1/namespaces/monitoring/services/http:kube-prom-kube-prometheus-prometheus:9090/proxy/api/v1/')
    expression=request.url.params['query']
    if path.endswith('query_range'):data={'resultType':'matrix','result':[{'values':[[now-60,'40'],[now,'42']]}]}
    else:
        per_node=expression in c.utilization_expressions(SOURCE,per_node=True).values()
        rows=[{'metric':{'instance':'10.0.0.'+str(i)+':9100'},'value':[now,str(30+i)]} for i in range(1,7)] if per_node else [{'value':[now,'34']}]
        data={'resultType':'vector','result':rows}
    return httpx.Response(200,json={'status':'success','data':data})


def test_connect_probes_all_metrics_and_saves_atomically(client,monkeypatch):
    add(client);seen=[]
    def handler(r):seen.append(r);return upstream(r)
    install_transport(monkeypatch,handler)
    result=client.post('/api/clusters/one/metrics-connect',json={'source':SOURCE},headers=MUTATION)
    assert result.status_code==200,result.text
    data=result.json();assert data['saved'] and data['ok']
    assert all(v['covered']==6 and v['expected']==6 and v['percent']==34 for v in data['metrics'].values())
    assert len([r for r in seen if '/proxy/' in r.url.path])==9
    row=client.get('/api/clusters').json()['items'][0]
    assert row['metrics']['transport']=='kubernetes' and row['has_metrics']
    assert 'test-token' not in result.text


@pytest.mark.parametrize('status,word',[(401,'401'),(403,'services/proxy'),(404,'namespace')])
def test_probe_failure_never_saves_or_claims_success(client,monkeypatch,status,word):
    add(client)
    install_transport(monkeypatch,lambda r:httpx.Response(status,json={}) if '/proxy/' in r.url.path else upstream(r))
    data=client.post('/api/clusters/one/metrics-connect',json={'source':SOURCE},headers=MUTATION).json()
    assert not data['saved'] and not data['ok'] and word in data['action']
    assert not client.get('/api/clusters').json()['items'][0]['has_metrics']


def test_foreign_node_source_rejected(client,monkeypatch):
    add(client)
    def handler(r):
        reply=upstream(r)
        if r.url.path.endswith('/query') and r.url.params['query'] in c.utilization_expressions(SOURCE,True).values():
            data=reply.json();data['data']['result'][0]['metric']['instance']='192.0.2.55:9100';return httpx.Response(200,json=data)
        return reply
    install_transport(monkeypatch,handler)
    result=client.post('/api/clusters/one/metrics-connect',json={'source':SOURCE},headers=MUTATION).json()
    assert not result['saved'] and 'неизвестные узлы' in result['action']


def test_missing_config_is_not_a_prometheus_query_failure(client):
    add(client);result=client.get('/api/clusters/one/metrics-diagnostics').json()
    assert result['status']=='unconfigured' and not result['configured']
    assert 'не сохранён' in result['action']


def test_changed_connection_during_probe_is_not_overwritten(client,monkeypatch):
    add(client)
    def probe(config,source):
        client.put('/api/clusters/one',json={'name':'Renamed','astra':{'url':'http://astra.test'}},headers=MUTATION)
        return {'ok':True,'source':source,'metrics':{}}
    monkeypatch.setattr(setup,'probe',probe)
    response=client.post('/api/clusters/one/metrics-connect',json={'source':SOURCE},headers=MUTATION)
    assert response.status_code==409
    row=client.get('/api/clusters').json()['items'][0];assert row['name']=='Renamed' and not row['has_metrics']


def test_build_is_visible_and_identifies_running_code(client):
    from pulse_app import __version__
    from pulse_app.build import identity
    public=client.get('/healthz').json();private=client.get('/api/version').json()
    assert public['build_id']==private['build_id']==identity()['build_id'] and len(public['build_id'])==16
    assert public['version']==__version__


@pytest.mark.parametrize('case',['complete','missing','foreign','duplicate','ambiguous','incomplete','stale'])
def test_dashboard_coverage_checks_identities_not_only_counts(case):
    now=time.time()
    nodes={'items':[{'metadata':{'name':name}} for name in ('node-a','node-b')],'complete':True,'error':None}
    rows=[{'instance':name+':9100','percent':25,'observed_at':now} for name in ('node-a','node-b')]
    if case=='missing':rows.pop()
    elif case=='foreign':rows[-1]['instance']='foreign:9100'
    elif case=='duplicate':rows.append({**rows[0],'instance':'node-a:9200'})
    elif case=='ambiguous':
        nodes['items']=[{'metadata':{'name':'node-a.example'}},{'metadata':{'name':'node-a.other'}}]
        rows=[{**rows[0]},{**rows[0],'instance':'node-a:9200'}]
    elif case=='incomplete':nodes['complete']=False
    elif case=='stale':rows[-1]['observed_at']-=1800
    data={'metrics':{k:{'percent':25,'observed_at':now,'history':[[now,25]],'nodes':rows,'error':None,'nodes_error':None} for k in ('cpu','ram','disk')}}
    result=setup.apply_coverage(data,nodes)
    for value in result['metrics'].values():
        assert value['coverage']['ok']==(case=='complete')
        if case=='complete':assert value['percent']==25 and value['history'] and value['nodes']
        else:assert value['percent'] is None and value['error'] and value['history']==[] and value['nodes']==[]
    assert data['metrics']['cpu']['percent']==25  # validation does not corrupt cached raw observations
