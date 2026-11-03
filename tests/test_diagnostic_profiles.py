"""Read-only collection contracts for the six guided Pod scenarios."""
from copy import deepcopy
import json

import httpx
import pytest

from pulse_app import connectors as c, diagnostics as d
from test_connectors import install_transport
from test_diagnostics import config, failed_pod, upstream_handler


def replacement():
    return {'metadata':{'namespace':'am-agent','name':'replacement','uid':'new-uid',
                        'ownerReferences':[{'kind':'ReplicaSet','name':'rs-new','uid':'rs-new','controller':True}]},
            'spec':{'containers':[{'name':'agent'}]},
            'status':{'phase':'Running','podIP':'10.20.30.40',
                      'conditions':[{'type':'Ready','status':'True'}],
                      'containerStatuses':[{'name':'agent','ready':True,'state':{'running':{}}}]}}


def strict_upstream(request):
    path=request.url.path
    if path.endswith('/replicasets'):
        return httpx.Response(200,json={'items':[{'metadata':{'name':'rs-new','uid':'rs-new',
            'namespace':'am-agent','ownerReferences':[{'kind':'Deployment','name':'agent',
                'uid':'dep-uid','controller':True}]}}]})
    if path=='/api/v1/namespaces/am-agent/pods':
        return httpx.Response(200,json={'items':[replacement()]})
    if path.endswith('/endpoints/matching'):
        return httpx.Response(200,json={'metadata':{'name':'matching','namespace':'am-agent'},
            'subsets':[{'addresses':[{'ip':'10.20.30.40','targetRef':{
                'kind':'Pod','name':'replacement','namespace':'am-agent','uid':'new-uid'}}]}]})
    response=upstream_handler(request)
    if path.endswith('/deployments/agent') or path.endswith('/replicasets/rs-old'):
        value=response.json();value['metadata']['generation']=2
        value['status'].update(observedGeneration=2,replicas=1,readyReplicas=1,availableReplicas=1)
        return httpx.Response(200,json=value)
    return response


@pytest.mark.parametrize('scenario',['brief','deep','causes','impact','plan','recovery'])
def test_scenario_collects_only_readonly_sources_and_explicit_groups(monkeypatch,scenario):
    requests=[]
    def handler(request):
        requests.append(request);return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario=scenario)
    assert report['scenario']==scenario
    assert report['records'] and all(row['group']==row['role'] for row in report['records'])
    assert all(row['group'] in {'pod','events','node','owner','replacements','volume',
        'service','endpoints','policies','logs','astra','metrics'} for row in report['records'])
    assert all(request.method=='GET' or (request.method=='POST' and
        request.url.path=='/astra/api/v1/logs/search/monitorLogs') for request in requests)
    if scenario in ('impact','recovery'):
        assert not {'logs','astra','metrics','node','policies','volume','events'} & {
            row['group'] for row in report['records']}
    if scenario=='brief':
        assert not {'astra','metrics','policies'} & {row['group'] for row in report['records']}
        assert sum(row['group']=='logs' for row in report['records'])<=2
    if scenario in ('causes','plan'):
        assert any(row['group']=='metrics' for row in report['records'])
        assert not any(row['check'].startswith('История cluster_') for row in report['records'])
    if scenario=='deep':
        assert any(row['check']=='История cluster_cpu' for row in report['records'])


def test_recovery_has_fresh_complete_uid_bound_controller_pods_and_endpoints(monkeypatch):
    install_transport(monkeypatch,strict_upstream)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='recovery')
    assert report['pod']['reason']=='Evicted'  # Historical failure does not become current health.
    assert report['owners_complete']
    assert all(owner['source_type']=='live' and owner['generation']==owner['observedGeneration']==2
               for owner in report['owners'])
    assert report['replacements']['complete'] and not report['replacements']['truncated']
    pod=report['replacements']['items'][0]
    assert pod['ready'] and pod['readiness_complete'] and pod['owner_uids']==['rs-new']
    assert pod['source_type']=='live' and pod['uid']=='new-uid'
    assert report['services']['complete']
    endpoint=report['endpoints'][0]
    assert endpoint['complete'] and endpoint['addresses'][0]['target_uid']==pod['uid']
    assert endpoint['addresses'][0]['ready']


def test_brief_direct_emptydir_fact_does_not_collect_unrelated_logs(monkeypatch):
    def handler(request):
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':
            value=failed_pod();value['status']['message']='Usage of EmptyDir volume "data" exceeds the limit "1Gi".'
            return httpx.Response(200,json=value)
        assert not request.url.path.endswith('/log')
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='brief')
    assert d.emptydir_cause(report)=={'volume':'data','limit':'1Gi'}
    assert not any(row['group']=='logs' for row in report['records'])


def test_brief_does_not_request_logs_for_a_container_that_never_started(monkeypatch):
    def handler(request):
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':
            value=failed_pod();value['status']={'phase':'Pending','containerStatuses':[
                {'name':'agent','restartCount':0,'state':{'waiting':{'reason':'ImagePullBackOff'}}}]}
            return httpx.Response(200,json=value)
        assert not request.url.path.endswith('/log')
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='brief')
    assert not any(row['group']=='logs' for row in report['records'])


def test_recovery_uses_cached_evicted_pod_only_for_owner_identity(monkeypatch):
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':return httpx.Response(404,json={})
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',cached=failed_pod(),scenario='recovery')
    assert report['pod']['source_type']=='cached' and report['pod']['observed_at'] is None
    assert report['owners_complete'] and report['replacements']['complete']
    assert all(row['source_type']=='live' for row in report['replacements']['items'])
    assert report['unavailable']==1 and not report['complete']
    assert any('/deployments/' in path for path in paths)
    assert not any('/log' in path or '/query_range' in path for path in paths)


@pytest.mark.parametrize('broken',['owner_uid','replicasets_page','pods_page','pod_excerpt','endpoints_403','invalid_endpoints'])
def test_recovery_cannot_hide_missing_or_partial_sources(monkeypatch,broken):
    def handler(request):
        path=request.url.path
        if broken=='owner_uid' and path.endswith('/replicasets/rs-old'):
            response=strict_upstream(request).json();response['metadata']['uid']='foreign-owner'
            return httpx.Response(200,json=response)
        if broken=='replicasets_page' and path.endswith('/replicasets'):
            response=strict_upstream(request).json();response['metadata']={'continue':'more-rs'}
            return httpx.Response(200,json=response)
        if broken=='pods_page' and path=='/api/v1/namespaces/am-agent/pods':
            return httpx.Response(200,json={'items':[replacement()],'metadata':{'continue':'more-pods'}})
        if broken=='pod_excerpt' and path=='/api/v1/namespaces/am-agent/pods':
            value=replacement();value['status']['message']='large-status-'*1500
            return httpx.Response(200,json={'items':[value]})
        if path.endswith('/endpoints/matching'):
            if broken=='endpoints_403':return httpx.Response(403,text='do not expose token=private')
            if broken=='invalid_endpoints':return httpx.Response(200,json={'subsets':None})
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='recovery')
    if broken in ('owner_uid','replicasets_page','pods_page','pod_excerpt'):
        assert not report['replacements']['complete']
    else:
        assert not report['endpoints'][0]['complete']
    assert report['unavailable'] or report['partial']
    assert 'private' not in json.dumps(report)


def test_other_uid_and_namespace_are_excluded_from_replacement_evidence(monkeypatch):
    def handler(request):
        if request.url.path=='/api/v1/namespaces/am-agent/pods':
            foreign=replacement();foreign['metadata'].update(name='foreign',uid='foreign-pod')
            foreign['metadata']['ownerReferences'][0]['uid']='other-deployment-rs'
            wrong_namespace=deepcopy(replacement());wrong_namespace['metadata'].update(namespace='other',name='wrong-ns',uid='wrong-ns-pod')
            unmanaged=deepcopy(replacement());unmanaged['metadata'].update(name='unmanaged',uid='unmanaged-pod')
            unmanaged['metadata']['ownerReferences'][0]['controller']=False
            return httpx.Response(200,json={'items':[replacement(),foreign,wrong_namespace,unmanaged]})
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='recovery')
    assert [item['uid'] for item in report['replacements']['items']]==['new-uid']
    encoded=json.dumps(report)
    assert 'foreign-pod' not in encoded and 'wrong-ns-pod' not in encoded and 'unmanaged-pod' not in encoded


def test_reused_pod_name_is_excluded_but_cached_owner_bridge_is_retained(monkeypatch):
    def handler(request):
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':
            pod=failed_pod();pod['metadata']['uid']='other-uid';pod['status']['message']='foreign failure'
            return httpx.Response(200,json=pod)
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',cached=failed_pod(),scenario='recovery')
    assert report['identity_changed'] and report['pod']['source_type']=='cached'
    assert report['pod']['uid']=='old-uid' and report['replacements']['items'][0]['uid']=='new-uid'
    assert report['records'][0]['status']=='identity_changed'
    assert 'foreign failure' not in json.dumps(report)


def test_log_uid_race_discards_result_and_does_not_leak_foreign_log(monkeypatch):
    count=0
    def handler(request):
        nonlocal count
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':
            count+=1;pod=failed_pod()
            if count>=3:pod['metadata']['uid']='reused-name-uid'
            return httpx.Response(200,json=pod)
        if request.url.path.endswith('/log'):return httpx.Response(200,text='FOREIGN-LOG-MUST-BE-DISCARDED')
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='brief')
    assert any(row['group']=='logs' and row['status']=='unavailable' for row in report['records'])
    assert 'FOREIGN-LOG' not in json.dumps(report)


def test_exhausted_budget_never_sends_a_request(monkeypatch):
    install_transport(monkeypatch,lambda request:pytest.fail('Expired collector must not send HTTP'))
    collector=d.Collector(config(),scenario='recovery');collector.deadline=0
    report=collector.collect('am-agent','bad','old-uid',cached=failed_pod())
    assert not report['complete'] and not report['replacements']['complete']
    assert not any(row['attempted'] for row in report['records'])


@pytest.mark.parametrize('change',['missing_status','not_ready','deleting','init_missing'])
def test_incomplete_or_unready_container_state_is_never_ready(change):
    value=replacement()
    if change=='missing_status':value['status'].pop('containerStatuses')
    if change=='not_ready':value['status']['containerStatuses'][0]['ready']=False
    if change=='deleting':value['metadata']['deletionTimestamp']='2026-11-02T14:00:00Z'
    if change=='init_missing':value['spec']['initContainers']=[{'name':'init'}]
    assert not d.replacement_facts(value)['ready']


def test_unknown_scenario_does_not_make_requests(monkeypatch):
    install_transport(monkeypatch,lambda request:pytest.fail('Invalid scenario'))
    with pytest.raises(c.ConnectorError):d.collect_pod(config(),'am-agent','bad','old-uid',scenario='shell')


def test_real_collector_recovery_contract_rejects_old_failure_as_current_health(monkeypatch):
    from pulse_app.workflows import recovery_assessment
    install_transport(monkeypatch,strict_upstream)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='recovery')
    assert report['pod']['phase']=='Failed'
    assert recovery_assessment(report)['state']=='verified'
    report['replacements']['complete']=False
    assert recovery_assessment(report)['state']!='verified'


def test_status_text_is_redacted_in_both_summary_and_journal(monkeypatch):
    def handler(request):
        if request.url.path=='/api/v1/namespaces/am-agent/pods/bad':
            value=failed_pod();value['status']['message']+=' token=private-status-value'
            return httpx.Response(200,json=value)
        return strict_upstream(request)
    install_transport(monkeypatch,handler)
    report=d.collect_pod(config(),'am-agent','bad','old-uid',scenario='brief')
    assert 'private-status-value' not in json.dumps(report)
