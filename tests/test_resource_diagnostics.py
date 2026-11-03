import copy
import time

import httpx
import pytest

from pulse_app import connectors as c
from pulse_app.resource_diagnostics import collect_resource
from test_connectors import install_transport


CONFIG = {'kube': {'server': 'https://cluster.test', 'token': 'fixture'},
          'metrics': {'url': 'https://metrics.test', 'mode': 'dedicated'}}


def controller(kind='deployments'):
    return {'metadata': {'name': 'selected', 'namespace': 'one', 'uid': 'selected-uid', 'generation': 3},
            'spec': {'replicas': 1},
            'status': {'observedGeneration': 3, 'readyReplicas': 1, 'availableReplicas': 1}}


def pod(name, owner):
    return {'metadata': {'name': name, 'namespace': 'one', 'uid': name + '-uid',
                         'ownerReferences': [{'uid': owner, 'controller': True}]},
            'spec': {'nodeName': 'selected', 'containers': [{'name': 'main'}]},
            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'containerStatuses': [{'name': 'main', 'ready': True, 'state': {'running': {}}}]}}


def test_workload_checks_uid_events_and_child_owner_chain(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == 'GET'
        path = request.url.path
        if path.endswith('/deployments/selected'):
            return httpx.Response(200, json=controller())
        if path.endswith('/events'):
            assert request.url.params['fieldSelector'] == 'involvedObject.uid=selected-uid'
            return httpx.Response(200, json={'items': [
                {'reason': 'FailedCreate', 'involvedObject': {'uid': 'selected-uid'}},
                {'message': 'FOREIGN EVENT', 'involvedObject': {'uid': 'another-uid'}}]})
        if path.endswith('/replicasets'):
            return httpx.Response(200, json={'items': [
                {'metadata': {'namespace': 'one', 'uid': 'rs-uid', 'ownerReferences': [{'uid': 'selected-uid', 'controller': True}]}},
                {'metadata': {'namespace': 'one', 'uid': 'foreign-rs', 'ownerReferences': [{'uid': 'other', 'controller': True}]}}]})
        if path.endswith('/pods'):
            return httpx.Response(200, json={'items': [pod('ours', 'rs-uid'), pod('FOREIGN POD', 'foreign-rs')]})
        pytest.fail(path)

    install_transport(monkeypatch, handler)
    target = {'kind': 'deployments', 'namespace': 'one', 'name': 'selected', 'uid': 'selected-uid'}
    report = collect_resource(CONFIG, target, scenario='brief')
    assert report['resource']['source_type'] == 'live'
    assert report['resource']['generation'] == 3
    assert report['resource']['observed_generation'] == 3
    assert report['related_pods']['complete']
    assert [item['name'] for item in report['related_pods']['items']] == ['ours']
    assert 'FOREIGN' not in str(report)
    assert 'pod' not in report and report['unavailable'] == 0
    assert len(seen) <= 15 and report['collection_plan']['seconds'] == 30


@pytest.mark.parametrize('kind,child', [('statefulsets', None), ('daemonsets', None),
                                     ('replicasets', None), ('jobs', None), ('cronjobs', 'jobs')])
def test_all_controller_kinds_use_explicit_uid_ownership(monkeypatch, kind, child):
    def handler(request):
        if request.url.path.endswith('/' + kind + '/selected'):
            return httpx.Response(200, json=controller(kind))
        if child and request.url.path.endswith('/' + child):
            return httpx.Response(200, json={'items': [{'metadata': {'namespace': 'one', 'uid': 'child', 'ownerReferences': [
                {'uid': 'selected-uid', 'controller': True}]}}]})
        if request.url.path.endswith('/pods'):
            return httpx.Response(200, json={'items': [pod('ours', 'child' if child else 'selected-uid'),
                                                     pod('foreign', 'unrelated')]})
        return httpx.Response(200, json={'items': []})

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'kind': kind, 'namespace': 'one', 'name': 'selected', 'uid': 'selected-uid'})
    assert [item['name'] for item in report['related_pods']['items']] == ['ours']


def test_changed_uid_excludes_live_facts_and_child_queries(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith('/events'):
            return httpx.Response(200, json={'items': []})
        obj = controller()
        obj['metadata']['uid'] = 'NEW UID'
        return httpx.Response(200, json=obj)

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'kind': 'deployments', 'namespace': 'one', 'name': 'selected', 'uid': 'old'})
    assert report['identity_changed']
    assert report['resource']['source_type'] == 'unavailable'
    assert 'related_pods' not in report
    assert not any(path.endswith('/pods') for path in seen)


def test_node_checks_only_assigned_pods_and_rechecks_uid(monkeypatch):
    reads = []

    def handler(request):
        if request.url.path.endswith('/nodes/selected'):
            reads.append(True)
            return httpx.Response(200, json={'metadata': {'name': 'selected', 'uid': 'node-uid'},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}})
        if request.url.path.endswith('/pods'):
            assert request.url.params['fieldSelector'] == 'spec.nodeName=selected'
            ours, foreign = pod('ours', 'none'), pod('foreign', 'none')
            foreign['spec']['nodeName'] = 'other'
            return httpx.Response(200, json={'items': [ours, foreign]})
        return httpx.Response(200, json={'items': []})

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'kind': 'nodes', 'name': 'selected', 'uid': 'node-uid'})
    assert report['resource']['state'] == 'ready'
    assert len(reads) == 2
    assert [item['name'] for item in report['related_pods']['items']] == ['ours']


def test_reuse_during_related_reads_cannot_be_complete(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        if request.url.path.endswith('/nodes/selected'):
            calls += 1
            return httpx.Response(200, json={'metadata': {'name': 'selected', 'uid': 'node-uid' if calls == 1 else 'new'},
                                             'status': {}})
        return httpx.Response(200, json={'items': []})

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'kind': 'nodes', 'name': 'selected', 'uid': 'node-uid'})
    assert report['resource']['source_type'] == 'unavailable'
    assert report['identity_changed'] and not report['related_pods']['complete']


def test_last_read_status_replaces_initial_controller_read(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        if request.url.path.endswith('/statefulsets/selected'):
            calls += 1
            value = controller()
            if calls == 2:
                value['status']['readyReplicas'] = 0
            return httpx.Response(200, json=value)
        return httpx.Response(200, json={'items': []})

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'kind': 'statefulsets', 'namespace': 'one', 'name': 'selected', 'uid': 'selected-uid'})
    assert report['resource']['state'] == 'not_ready'
    assert report['resource']['status']['readyReplicas'] == 0
    assert report['resource']['evidence_id'] == report['records'][-1]['id']


def test_pagination_limit_is_reported_as_partial(monkeypatch):
    def handler(request):
        if request.url.path.endswith('/statefulsets/selected'):
            return httpx.Response(200, json=controller())
        if request.url.path.endswith('/pods'):
            return httpx.Response(200, json={'items': [], 'metadata': {'continue': 'more'}})
        return httpx.Response(200, json={'items': []})

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'kind': 'statefulsets', 'namespace': 'one', 'name': 'selected', 'uid': 'selected-uid'})
    assert not report['related_pods']['complete'] and report['partial'] >= 1


@pytest.mark.parametrize('metric', ['cpu', 'ram', 'disk'])
def test_metric_queries_only_selected_metric_and_validates_freshness(monkeypatch, metric):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == 'GET'
        if request.url.host == 'cluster.test':
            assert request.url.path == '/api/v1/nodes'
            return httpx.Response(200, json={'items': [{'metadata': {'name': 'node', 'uid': 'node-uid'}}]})
        assert request.url.host == 'metrics.test'
        is_nodes = 'by(instance)' in request.url.params['query'].split('100 * ')[-1]
        return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': {'instance': 'node:9100'} if is_nodes else {}, 'value': [time.time(), '40']}]}})

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'metric': metric})
    assert len(seen) == 3
    assert report['metrics']['percent'] == 40
    assert report['metrics']['complete'] and report['metrics']['unit'] == 'percent'


def test_metrics_for_foreign_nodes_do_not_become_cluster_percentage(monkeypatch):
    def handler(request):
        if request.url.host == 'cluster.test':
            return httpx.Response(200, json={'items': [{'metadata': {'name': 'expected-node', 'uid': 'node-uid'}}]})
        return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': {'instance': 'foreign:9100'}, 'value': [time.time(), '90']}]}})

    install_transport(monkeypatch, handler)
    report = collect_resource(CONFIG, {'metric': 'cpu'})
    assert not report['metrics']['complete'] and not report['metrics']['coverage']['ok']
    assert 'percent' not in report['metrics']
    assert report['complete'] is False and report['partial'] >= 1


def test_stale_metric_does_not_become_current_fact(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={'status': 'success',
        'data': {'resultType': 'vector', 'result': [{'metric': {'instance': 'node:9100'},
                                                   'value': [time.time()-3600, '90']}]}}))
    report = collect_resource(CONFIG, {'metric': 'ram'})
    assert not report['metrics']['complete']
    assert 'percent' not in report['metrics'] and report['unavailable'] == 2


def test_snapshot_does_not_replace_failed_live_read(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(403, json={}))
    snapshot = {'complete': True, 'resources': {'deployments': {'items': [controller()]}}}
    before = copy.deepcopy(snapshot)
    report = collect_resource(CONFIG, {'kind': 'deployments', 'namespace': 'one', 'name': 'selected', 'uid': 'selected-uid'}, snapshot)
    assert snapshot == before and report['resource']['source_type'] == 'unavailable'
    assert 'status' not in report['resource']
