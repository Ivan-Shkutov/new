import copy,time
import httpx
import pytest
from pulse_app import inventory as inv,connectors as c
from test_connectors import install_transport
from test_api import client,runtime,add,MUTATION


@pytest.mark.parametrize('raw,value',[('100m',.1),('2500u',.0025),('10n',1e-8),('16Gi',17179869184),('1.5G',1500000000),('1e3',1000),('1',1),('0',0),('bad',None),('-1',None),('NaN',None),('1e999',None),(None,None)])
def test_quantities(raw,value):assert inv.quantity(raw)==value


def test_requests_include_restartable_init_peak_and_overhead():
    pod={'spec':{'containers':[{'resources':{'requests':{'cpu':'500m'}}}], 'initContainers':[
        {'restartPolicy':'Always','resources':{'requests':{'cpu':'200m'}}},
        {'resources':{'requests':{'cpu':'2'}}},
        {'restartPolicy':'Always','resources':{'requests':{'cpu':'300m'}}}], 'overhead':{'cpu':'100m'}}}
    assert inv.pod_budget(pod,'cpu','requests')==pytest.approx(2.3)
    pod['status']={'resize':'InProgress'}
    assert inv.pod_budget(pod,'cpu','requests') is None


def sample():
    return {'cluster_info':{'server_version':'v1.30.8'},'resources':{
        'nodes':{'items':[{'metadata':{'name':'node','labels':{'node-role.kubernetes.io/control-plane':''}},'status':{'capacity':{'cpu':'4','memory':'16Gi'},'allocatable':{'cpu':'4','memory':'16Gi'},'nodeInfo':{'kubeletVersion':'v1.30.14'}}}]},
        'namespaces':{'items':[{},{}]},
        'pods':{'items':[{'spec':{'nodeName':'node','containers':[{'resources':{'requests':{'cpu':'3975m','memory':'1Gi'},'limits':{'memory':'20Gi'}}}]},'status':{'phase':'Running'}}]}}}


def test_profile_keeps_api_version_distinct_and_budget_not_usage():
    snap=sample();snap['resources']['pods']['items'].append({'spec':{'nodeName':'node','containers':[{'resources':{'requests':{'cpu':'99'}}}]},'status':{'phase':'Failed'}})
    p=inv.summarize(snap)
    assert p['api_version']=='v1.30.8' and p['kubelet_versions']==['v1.30.14']
    assert p['capacity']=={'cpu':4,'memory':16*1024**3} and p['namespaces']==2
    node=p['nodes'][0]
    assert node['budget']['cpu']['requests_percent']==99.4
    assert node['budget']['memory']['limits_percent']==125
    assert node['budget']['cpu']['containers_without_limit']==1
    assert p['budget_complete'] and p['budget_risk_nodes']==1


@pytest.mark.parametrize('kind',['nodes','pods'])
def test_partial_snapshots_do_not_claim_complete_budget(kind):
    s=sample();s['resources'][kind]['truncated']=True
    p=inv.summarize(s)
    assert not p['budget_complete']
    if kind=='nodes':assert p['capacity']['cpu'] is None
    else:assert p['nodes'][0]['budget']['cpu']['requests'] is None


def test_unknown_capacity_and_quantities_stay_unknown():
    s=sample();s['resources']['nodes']['items'][0]['status'].pop('capacity')
    s['resources']['pods']['items'][0]['spec']['containers'][0]['resources']['requests']['cpu']='invalid'
    p=inv.summarize(s)
    assert p['capacity']['cpu'] is None and not p['budget_complete']


@pytest.mark.parametrize('forbidden',[False,True])
def test_live_version_is_optional_and_never_inferred_from_kubelet(monkeypatch,forbidden):
    monkeypatch.setattr(c,'RESOURCE_PATHS',{'nodes':'/api/v1/nodes'})
    def handle(request):
        if request.url.path=='/version':return httpx.Response(403 if forbidden else 200,json={'gitVersion':'v1.30.8'})
        return httpx.Response(200,json={'items':sample()['resources']['nodes']['items']})
    install_transport(monkeypatch,handle)
    result=c.collect_cluster({'server':'https://kube.test'})
    assert result['complete']
    assert result['cluster_info']['server_version']==(None if forbidden else 'v1.30.8')
    assert bool(result['cluster_info']['error'])==forbidden


def test_per_node_metrics_keep_hotspot_when_average_is_low(monkeypatch):
    now=time.time()
    def request(client,method,url,params):
        if url.endswith('query_range'):return {'status':'success','data':{'resultType':'matrix','result':[]}}
        query=params['query']
        per_node='sum by(instance)' in query or query.startswith('100 * (1 - (')
        result=[{'metric':{'instance':'node-a'},'value':[now,'20']},{'metric':{'instance':'node-b'},'value':[now,'96']}] if per_node else [{'value':[now,'40']}]
        return {'status':'success','data':{'resultType':'vector','result':result}}
    monkeypatch.setattr(c,'_request',request)
    result=c.dashboard_metrics({'url':'https://prom.test','mode':'dedicated'})
    for metric in result['metrics'].values():
        assert metric['percent']==40 and max(n['percent'] for n in metric['nodes'])==96


def test_dashboard_profile_survives_api_serialization(client,monkeypatch):
    from datetime import datetime,timezone
    add(client);s=sample();s['observed_at']=datetime.now(timezone.utc).isoformat();s['complete']=True;s['errors']=[]
    monkeypatch.setattr(c,'collect_cluster',lambda config:s)
    client.post('/api/clusters/one/refresh',headers=MUTATION)
    p=client.get('/api/dashboard?cluster_id=one').json()['clusters'][0]['profile']
    assert p['api_version']=='v1.30.8' and p['nodes'][0]['budget']['memory']['limits_percent']==125
