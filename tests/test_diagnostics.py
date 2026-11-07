import json
import time
from copy import deepcopy
from urllib.parse import parse_qs
import httpx
import pytest
from pulse_app import connectors as c,diagnostics as d
from pulse_app.dashboard import summarize
from test_connectors import install_transport
from test_health import pod,snapshot
from test_api import client,runtime,add,MUTATION
from test_compact_features import wait


def failed_pod():
    return {'metadata':{'name':'bad','namespace':'am-agent','uid':'old-uid','labels':{'app':'agent'},'ownerReferences':[{'kind':'ReplicaSet','name':'rs-old','uid':'rs-uid','controller':True}]},
            'spec':{'nodeName':'worker-2','containers':[{'name':'agent','image':'agent:1','resources':{'requests':{'ephemeral-storage':'100Mi'}}}]},
            'status':{'phase':'Failed','reason':'Evicted','message':'The node was low on resource: ephemeral-storage. Container agent was using 3Gi.',
                      'containerStatuses':[{'name':'agent','restartCount':1,'state':{'terminated':{'reason':'Error','exitCode':137,'finishedAt':'2026-10-30T07:00:00Z'}}}]}}


def upstream_handler(request):
    path=request.url.path
    if path=='/api/v1/namespaces/am-agent/pods/bad':return httpx.Response(200,json=failed_pod())
    if path.endswith('/log'):return httpx.Response(200,text='disk allocation failed token=secret-log-value')
    if path=='/api/v1/nodes/worker-2':return httpx.Response(200,json={'metadata':{'name':'worker-2','uid':'node-uid'},'status':{'conditions':[{'type':'Ready','status':'True'}]}})
    if path.endswith('/replicasets/rs-old'):return httpx.Response(200,json={'metadata':{'name':'rs-old','uid':'rs-uid','ownerReferences':[{'kind':'Deployment','name':'agent','uid':'dep-uid','controller':True}]},'spec':{'selector':{'matchLabels':{'app':'agent'}}},'status':{'readyReplicas':0}})
    if path.endswith('/deployments/agent'):return httpx.Response(200,json={'metadata':{'name':'agent','uid':'dep-uid'},'spec':{'replicas':1,'selector':{'matchLabels':{'app':'agent'}}},'status':{'readyReplicas':1}})
    if path.endswith('/replicasets'):return httpx.Response(200,json={'items':[{'metadata':{'uid':'rs-new','ownerReferences':[{'uid':'dep-uid'}]}}]})
    if path=='/api/v1/namespaces/am-agent/pods':return httpx.Response(200,json={'items':[{'metadata':{'name':'replacement','uid':'new-uid','ownerReferences':[{'uid':'rs-new'}]},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}]})
    if path=='/astra/api/v1/logs/search/monitorLogs':
        assert request.method=='POST' and request.headers['authorization']=='Bearer astra-test'
        body=json.loads(request.content);assert body['time']['from']<'2026-10-30T07:00:00' and body['time']['to']>'2026-10-30T07:00:00'
        return httpx.Response(200,json={'items':[{'labels.cluster':'peoplehub','labels.k8s.namespace.name':'am-agent','labels.k8s.pod.name':'bad','message':'our eviction log'}, {'labels.cluster':'other','message':'foreign-secret'}]})
    if path.endswith('/api/v1/query_range'):return httpx.Response(200,json={'status':'success','data':{'resultType':'matrix','result':[]}})
    if path.endswith('/services'):return httpx.Response(200,json={'items':[{'metadata':{'name':'matching'},'spec':{'selector':{'app':'agent'}}},{'metadata':{'name':'foreign'},'spec':{'selector':{'app':'other'}}}]})
    if path.endswith('/endpoints/matching'):return httpx.Response(200,json={'subsets':[]})
    if path.endswith('/networkpolicies'):return httpx.Response(200,json={'items':[]})
    if path.endswith('/events'):
        if '/namespaces/' in path:assert request.url.params['fieldSelector']=='involvedObject.uid=old-uid'
        return httpx.Response(200,json={'items':[{'reason':'Evicted','message':'ephemeral-storage','lastTimestamp':'2026-10-30T07:00:01Z'}]})
    raise AssertionError('Unexpected request '+request.method+' '+path)


def config():
    return {'kube':{'server':'https://cluster.test','token':'kube-test'},
            'metrics':{'url':'https://metrics.test','mode':'dedicated'},
            'astra':{'url':'https://cluster.test/astra','token':'astra-test','cluster_value':'peoplehub'}}


def test_targeted_readonly_investigation_collects_eviction_proof_and_replacement(monkeypatch):
    requests=[]
    def handler(request):requests.append(request);return upstream_handler(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid')
    assert report['pod']['node']=='worker-2'
    assert 'ephemeral-storage' in report['pod']['message']
    assert report['replacements']['items'][0]['name']=='replacement'
    assert report['unavailable']==0
    encoded=json.dumps(report)
    assert 'secret-log-value' not in encoded and 'foreign-secret' not in encoded
    assert 'our eviction log' in encoded
    assert any('previous' in r['equivalent_command'] for r in report['records'])
    assert all(r.method=='GET' or r.url.path=='/astra/api/v1/logs/search/monitorLogs' for r in requests)
    assert 'время чтения' not in d.verified_summary(report) or report['records']
    assert report['history_window']['basis'].startswith('container finishedAt')
    assert len(json.dumps(d.model_evidence(report)))<100000


def test_deleted_pod_still_queries_astra_and_preserves_gaps(monkeypatch):
    seen=[]
    def handler(request):
        seen.append(request.url.path)
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':return httpx.Response(404,json={})
        return upstream_handler(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid')
    assert '/astra/api/v1/logs/search/monitorLogs' in seen
    assert any(r['status']=='unavailable' and '404' in r.get('error','') for r in report['records'])
    assert report['pod']['node'] is None


def test_evidence_persists_when_model_fails(client,monkeypatch):
    add(client)
    cgf=config()
    client.put('/api/clusters/one',json={'metrics':cgf['metrics'],'astra':cgf['astra']},headers=MUTATION)
    client.put('/api/settings',json={'llm':{'url':'http://localhost:8002/v1','model':'qwen'}},headers=MUTATION)
    monkeypatch.setattr(c,'llm_chat',lambda *a:(_ for _ in ()).throw(c.ConnectorError('Model unavailable')))
    install_transport(monkeypatch,upstream_handler)
    response=client.post('/api/investigations',json={'cluster_id':'one','namespace':'am-agent','pod':'bad','question':'Нужные команды','mode':'commands'},headers=MUTATION)
    job=wait(client,response.json()['id'])
    assert job['status']=='failed'
    report=json.loads(job['evidence'])
    assert report['records'] and report['pod']['reason']=='Evicted'
    assert client.get('/api/investigations?cluster_id=one').json()['items'][0].get('evidence') is None


@pytest.mark.parametrize('change,healthy', [('ok',2),('nonzero',1),('missing',1),('failed',1),('names',1),('initfailed',1),('deleting',2)])
def test_pods_healthy_over_all_with_verified_completed(change,healthy):
    s=snapshot();complete=pod('Succeeded')
    if change=='nonzero':complete['status']['containerStatuses'][0]['state']['terminated']['exitCode']=1
    if change=='missing':complete['status']['containerStatuses']=[]
    if change=='failed':complete['status'].update(phase='Failed',reason='Evicted')
    if change=='names':complete['spec']={'containers':[{'name':'real'}]};complete['status']['containerStatuses'][0]['name']='other'
    if change=='initfailed':complete['spec']={'initContainers':[{'name':'init'}]};complete['status']['initContainerStatuses']=[{'name':'init','state':{'terminated':{'exitCode':1}}}]
    if change=='deleting':complete['metadata']['deletionTimestamp']='2026-10-30T07:00:00Z'
    s['resources']['pods']['items']=[pod(),complete]
    out=summarize(s,'one','One');h=out['health']['pods']
    assert h['total']==2 and h['healthy']==healthy
    assert out['workloads']['pods']['positive']==healthy
    assert h['health']==('healthy' if change in ('ok','deleting') else 'critical')


def test_kubernetes_metrics_proxy_preserves_cluster_scope_and_credential(monkeypatch):
    seen=[]
    def handler(request):
        seen.append(request)
        assert request.headers['authorization']=='Bearer kube-test'
        assert request.url.path=='/api/v1/namespaces/monitoring/services/http:kube-prom-kube-prometheus-prometheus:9090/proxy/api/v1/query'
        assert 'cluster="one"' in request.url.params['query']
        return httpx.Response(200,json={'status':'success','data':{'resultType':'vector','result':[{'value':[time.time(),'100']}]}})
    install_transport(monkeypatch,handler)
    cfg={'kube':{'server':'https://cluster.test','token':'kube-test'},'metrics':{'transport':'kubernetes','namespace':'monitoring','service':'kube-prom-kube-prometheus-prometheus','port':9090,'labels':{'cluster':'one'},'token':'wrong-direct-token'}}
    assert not c.query_metrics(c.metrics_config(cfg),'node_memory_total')['empty']
    assert len(seen)==1


def test_metrics_discovery_config_and_error_reporting(client,monkeypatch):
    add(client)
    s=snapshot();s['resources']['services']={'items':[{'metadata':{'namespace':'monitoring','name':'kube-prom-kube-prometheus-prometheus'},'spec':{'ports':[{'port':9090}]}},{'metadata':{'name':'kube-prom-kube-prometheus-operator'},'spec':{'ports':[{'port':443}]}}]}
    monkeypatch.setattr(c,'collect_cluster',lambda cfg:s)
    client.post('/api/clusters/one/refresh',headers=MUTATION)
    install_transport(monkeypatch,lambda request:httpx.Response(200,json=s['resources']['services']))
    selected=client.get('/api/clusters/one/metrics-discovery').json()['items']
    assert len(selected)==1 and selected[0]['service'].endswith('-prometheus')
    response=client.put('/api/clusters/one',json={'metrics':{**selected[0],'mode':'dedicated'}},headers=MUTATION)
    assert response.json()['has_metrics']
    monkeypatch.setattr(c,'query_metrics',lambda *a:(_ for _ in ()).throw(c.ConnectorError('Upstream HTTP 403')))
    test=client.post('/api/clusters/one/metrics-test',headers=MUTATION)
    assert test.status_code==502 and 'services/proxy' in test.text
    assert client.put('/api/clusters/one',json={'metrics':{'port':'9090'}},headers=MUTATION).status_code==422


def test_commands_mode_uses_real_evidence_and_no_shell(client,monkeypatch):
    add(client)
    client.put('/api/settings',json={'llm':{'url':'http://localhost:8002/v1','model':'qwen'}},headers=MUTATION)
    import subprocess
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('Shell/subprocess execution is forbidden'))
    captured=[]
    monkeypatch.setattr(c,'llm_chat',lambda settings,messages:(captured.append(messages) or 'Причина: ephemeral-storage [E01].'))
    install_transport(monkeypatch,upstream_handler)
    invalid=client.post('/api/investigations',json={'cluster_id':'one','question':'Проверь','mode':'run-shell'},headers=MUTATION)
    assert invalid.status_code==422
    result=client.post('/api/investigations',json={'cluster_id':'one','question':'Команды','namespace':'am-agent','pod':'bad','mode':'commands'},headers=MUTATION)
    job=wait(client,result.json()['id'])
    assert job['status']=='done'
    assert 'единый последовательный план' in captured[0][0]['content']
    assert 'ephemeral-storage' in captured[0][-1]['content']
    assert 'worker-2' in captured[0][-1]['content']
    assert json.loads(job['context'])['uid']=='old-uid'
    assert 'Проверенные данные Pulse' in job['answer']


def test_metrics_proxy_403_is_visible_for_all_gauges(monkeypatch):
    install_transport(monkeypatch,lambda request:httpx.Response(403,json={'message':'not authorized'}))
    source=c.metrics_config({'kube':{'server':'https://cluster.test','token':'read-only'},'metrics':{'transport':'kubernetes','namespace':'monitoring','service':'prometheus','port':9090,'mode':'dedicated'}})
    result=c.dashboard_metrics(source)
    assert all(v['percent'] is None and '403' in v['error'] for v in result['metrics'].values())


def test_same_name_new_uid_is_excluded_from_old_case(monkeypatch):
    def handler(request):
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':
            replacement=failed_pod();replacement['metadata']['uid']='new-uid'
            replacement['status']['message']='DO-NOT-ATTRIBUTE-THIS-TO-OLD-POD'
            return httpx.Response(200,json=replacement)
        return upstream_handler(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid')
    assert report['records'][0]['status']=='identity_changed'
    assert not report['records'][0]['complete']
    assert report['pod']['source_type']=='unavailable'
    assert 'DO-NOT-ATTRIBUTE' not in json.dumps(report)
