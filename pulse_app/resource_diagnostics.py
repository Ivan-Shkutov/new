"""Fresh, bounded diagnostics for a selected node, controller, PVC or metric.

Only HTTP reads are performed. List results are independently constrained by
node assignment or controller UID; a label match is never an ownership proof.
"""
import time

from . import connectors as c
from .dashboard import workload_state
from .diagnostics import Collector, controller_refs, replacement_facts, pod_facts


KINDS = {'nodes', 'deployments', 'statefulsets', 'daemonsets', 'jobs',
         'cronjobs', 'replicasets', 'persistentvolumeclaims'}


def _conditions(status):
    values = status.get('conditions', [])
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise c.ConnectorError('API не вернул поддерживаемые conditions')
    return values


def resource_facts(kind, obj):
    spec, status = obj.get('spec', {}), obj.get('status', {})
    _conditions(status)
    state = 'unknown'
    if kind == 'nodes':
        conditions = {item.get('type'): item.get('status') for item in _conditions(status)}
        if conditions.get('Ready') == 'False' or any(conditions.get(key) == 'True' for key in
                ('DiskPressure', 'MemoryPressure', 'PIDPressure', 'NetworkUnavailable')):
            state = 'not_ready'
        elif conditions.get('Ready') == 'True':
            state = 'ready'
    elif kind == 'persistentvolumeclaims':
        state = status.get('phase', 'Unknown')
    else:
        state = workload_state(kind, obj)
    return {'metadata': obj.get('metadata', {}),
            'spec': {key: spec[key] for key in ('replicas', 'selector', 'suspend', 'schedule',
                'backoffLimit', 'completions', 'parallelism', 'storageClassName', 'accessModes',
                'volumeName', 'resources', 'unschedulable') if key in spec},
            'status': status, 'conditions': _conditions(status), 'state': state}


class ResourceCollector(Collector):
    def __init__(self, config, progress=None, scenario='deep'):
        super().__init__(config, progress, scenario)
        self.plan = {**self.plan, 'seconds': 30, 'checks': 15}
        self.deadline = time.monotonic() + 30
        self.report['collection_plan'] = {'seconds': 30, 'max_checks': 15}

    def list_scoped(self, client, label, path, command, accept, *, params=None, group='replacements'):
        """Bound pagination and filter locally, including unrelated server rows."""
        items, evidence_ids, continuation, complete = [], [], '', True
        for _ in range(2):
            query = {'limit': 100, **(params or {})}
            if continuation:
                query['continue'] = continuation

            def project(data):
                rows = data.get('items')
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise c.ConnectorError('API не вернул поддерживаемый список объектов')
                return {'items': [row for row in rows if accept(row)][:40],
                        'truncated': bool(data.get('metadata', {}).get('continue')) or
                                     sum(1 for row in rows if accept(row)) > 40}

            result, rid = self.get(client, label, path, command, query, project, group=group)
            evidence_ids.append(rid)
            if result is None or not isinstance(result.get('items'), list):
                complete = False
                break
            rows = result['items']
            if any(not isinstance(row, dict) for row in rows):
                complete = False
                break
            selected = [row for row in rows if accept(row)]
            items.extend(selected)
            continuation = result.get('metadata', {}).get('continue', '')
            if len(items) > 40:
                complete = False
                break
            if not continuation:
                break
        else:
            complete = not bool(continuation)
        return items[:40], {'complete': complete, 'evidence_ids': evidence_ids}

    def collect_object(self, target):
        kind = target.get('kind')
        if kind not in KINDS:
            raise c.ConnectorError('Этот тип ресурса пока не поддерживает live-диагностику')
        name = c._name(target.get('name'), 'name')
        ns = '' if kind == 'nodes' else c._name(target.get('namespace'), 'namespace')
        expected_uid = target.get('uid') or target.get('expected_uid')
        if not isinstance(expected_uid, str) or not expected_uid:
            raise c.ConnectorError('Для live-диагностики нужен UID выбранного объекта')
        collection = c.RESOURCE_PATHS[kind]
        if ns:
            prefix, plural = collection.rsplit('/', 1)
            path = f'{prefix}/namespaces/{ns}/{plural}/{name}'
        else:
            path = collection + '/' + name
        command = f'kubectl {"-n " + ns + " " if ns else ""}get {kind} {name} -o json'
        self.report['target'] = {**target, 'expected_uid': expected_uid}
        resource = {'kind': kind, 'name': name, 'namespace': ns, 'uid': expected_uid,
                    'source_type': 'unavailable', 'state': 'unknown'}
        self.report['resource'] = resource
        with c._client(self.config['kube'], timeout=6) as client:
            obj, rid = self.get(client, 'Выбранный ресурс: spec/status/conditions', path,
                                command, project=lambda value: resource_facts(kind, value), group='resource')
            resource['evidence_id'] = rid
            if obj is not None:
                meta = obj.get('metadata', {})
                valid = (meta.get('name') == name and meta.get('uid') == expected_uid and
                         (not ns or meta.get('namespace') == ns))
                if not valid:
                    self.invalidate(rid, 'API вернул другой namespace/name/UID; объект исключён')
                    self.report['identity_changed'] = meta.get('uid') != expected_uid
                    obj = None
                else:
                    facts = resource_facts(kind, obj)
                    resource.update({key: facts[key] for key in ('spec', 'status', 'conditions', 'state')})
                    resource.update(source_type='live', generation=meta.get('generation'),
                                    observed_generation=facts['status'].get('observedGeneration'),
                                    observed_at=self.report['records'][-1]['finished_at'])
                    self.report['findings'].append({'source': rid,
                        'fact': f'{kind}/{name}: текущее состояние {facts["state"]}.'})
            events_path = f'/api/v1/namespaces/{ns}/events' if ns else '/api/v1/events'
            self.list_scoped(client, 'События UID выбранного ресурса', events_path,
                f'kubectl {"-n " + ns + " " if ns else "--all-namespaces "}get events --field-selector involvedObject.uid={expected_uid} -o json',
                lambda row: (row.get('involvedObject') or row.get('regarding') or {}).get('uid') == expected_uid,
                params={'fieldSelector': 'involvedObject.uid=' + expected_uid}, group='events')
            if obj is not None and kind != 'persistentvolumeclaims':
                owner_uids = {expected_uid}
                ownership_complete = True
                chain_evidence = []
                if kind in ('deployments', 'cronjobs'):
                    child_kind = 'replicasets' if kind == 'deployments' else 'jobs'
                    child_collection = c.RESOURCE_PATHS[child_kind]
                    child_prefix, child_plural = child_collection.rsplit('/', 1)
                    children, coverage = self.list_scoped(client, 'Дочерние контроллеры по owner UID',
                        f'{child_prefix}/namespaces/{ns}/{child_plural}',
                        f'kubectl -n {ns} get {child_kind} -o json',
                        lambda row: row.get('metadata', {}).get('namespace') == ns and
                                    any(ref.get('uid') == expected_uid and ref.get('controller') is True
                                        for ref in controller_refs(row)), group='owner')
                    owner_uids = {row.get('metadata', {}).get('uid') for row in children
                                  if row.get('metadata', {}).get('uid')}
                    ownership_complete = coverage['complete']
                    chain_evidence = coverage['evidence_ids']
                pods_path = f'/api/v1/namespaces/{ns}/pods' if ns else '/api/v1/pods'
                def belongs(row):
                    if kind == 'nodes':
                        return row.get('spec', {}).get('nodeName') == name
                    return row.get('metadata', {}).get('namespace') == ns and any(
                               ref.get('uid') in owner_uids and ref.get('controller') is True
                               for ref in controller_refs(row))
                pods, coverage = self.list_scoped(client, 'Связанные Pod выбранного ресурса', pods_path,
                    f'kubectl {"-n " + ns + " " if ns else "--all-namespaces "}get pods -o json', belongs,
                    params={'fieldSelector': 'spec.nodeName=' + name} if kind == 'nodes' else {})
                pod_items, pod_shapes_complete = [], True
                for pod in pods:
                    try:
                        pod_facts(pod)
                        if not all(pod.get('metadata', {}).get(key) for key in ('name', 'namespace', 'uid')):
                            raise c.ConnectorError('Идентичность связанного Pod не подтверждена')
                        pod_items.append(replacement_facts(pod))
                    except (c.ConnectorError, TypeError, AttributeError):
                        pod_shapes_complete = False
                self.report['related_pods'] = {'items': pod_items,
                    'complete': ownership_complete and coverage['complete'] and pod_shapes_complete,
                    'evidence_ids': chain_evidence + coverage['evidence_ids'],
                    'scope': 'node assignment' if kind == 'nodes' else 'controller UID ownership'}
                if not pod_shapes_complete:
                    _, validation_id = self.run('Проверка структуры связанных Pod', '', 'VALIDATE',
                        'related_pods/validation', lambda: {'truncated': True,
                            'notice': 'Часть связанных Pod не имеет поддерживаемой структуры или полного UID'},
                        group='replacements')
                    self.report['related_pods']['evidence_ids'].append(validation_id)
                # Node names and controller names can be reused during a read.
                # Confirm the selected identity still exists after related lists.
                guard, guard_id = self.get(client, 'Повторная проверка UID выбранного ресурса',
                    path, command, project=lambda value: resource_facts(kind, value), group='resource')
                guard_meta = guard.get('metadata', {}) if guard else {}
                if (guard_meta.get('uid') != expected_uid or guard_meta.get('name') != name or
                        (ns and guard_meta.get('namespace') != ns)):
                    self.invalidate(guard_id, 'UID выбранного ресурса не подтверждён после связанных проверок')
                    self.report['related_pods']['complete'] = False
                    resource['source_type'] = 'unavailable'
                    self.report['identity_changed'] = bool(guard and guard_meta.get('uid') != expected_uid)
                else:
                    facts = resource_facts(kind, guard)
                    resource.update({key: facts[key] for key in ('spec', 'status', 'conditions', 'state')})
                    resource.update(evidence_id=guard_id, generation=guard_meta.get('generation'),
                        observed_generation=facts['status'].get('observedGeneration'),
                        observed_at=self.report['records'][-1]['finished_at'])
                    self.report['findings'][-1] = {'source': guard_id,
                        'fact': f'{kind}/{name}: текущее состояние {facts["state"]}.'}
        self.finish()
        return self.report

    def collect_metric(self, target):
        key = target.get('metric')
        if key not in ('cpu', 'ram', 'disk'):
            raise c.ConnectorError('Неизвестная метрика')
        self.report['target'] = dict(target)
        self.report['metrics'] = {'metric': key, 'series': [], 'complete': False,
                                  'unit': 'percent', 'window': 'instant', 'source_type': 'live',
                                  'observed_at': None, 'evidence_ids': []}
        source = c.metrics_config(self.config)
        try:
            if not c.metrics_configured(source):
                raise c.ConnectorError('Источник метрик не настроен')
            expression = c.utilization_expressions(source)[key]
            per_node = c.utilization_expressions(source, per_node=True)[key]
            with c.metric_client(source) as (client, base):
                for label, expr in (('cluster', expression), ('nodes', per_node)):
                    def read(expr=expr, label=label):
                        reply = c._request(client, 'GET', base + '/api/v1/query',
                            params={'query': expr}, deadline=self.deadline, timeout=6)
                        rows = c._metric_series(reply, 'vector')
                        if not rows or len(rows) > (1 if label == 'cluster' else 500):
                            raise c.ConnectorError('Пустая или неоднозначная выборка метрик')
                        observed = time.time()
                        instances = set()
                        for row in rows:
                            c._metric_point(row.get('value'), earliest=observed-120,
                                            latest=observed+30, percentage=True)
                            if label == 'nodes':
                                instance = row.get('metric', {}).get('instance')
                                if not isinstance(instance, str) or not instance or instance in instances:
                                    raise c.ConnectorError('Некорректная идентичность узла метрик')
                                instances.add(instance)
                        return {'scope': label, 'series': rows}
                    result, rid = self.run('Свежая загрузка ' + key + ': ' + label,
                        'PromQL: ' + expr, 'GET', '/api/v1/query', read,
                        query={'query': expr}, group='metrics')
                    self.report['metrics']['evidence_ids'].append(rid)
                    if result is not None:
                        self.report['metrics']['series'].append(result)
            metric = self.report['metrics']
            metric['complete'] = len(metric['series']) == 2 and all(
                self.complete_record(rid) for rid in metric['evidence_ids'])
            aggregate = next((value for value in metric['series'] if value['scope'] == 'cluster'), None)
            if aggregate:
                timestamp, percent = c._metric_point(aggregate['series'][0]['value'],
                    earliest=time.time()-120, latest=time.time()+30, percentage=True)
                node_series = next((value for value in metric['series'] if value['scope'] == 'nodes'), {'series': []})
                nodes = []
                for row in node_series['series']:
                    observed, number = c._metric_point(row['value'], earliest=time.time()-120,
                                                       latest=time.time()+30, percentage=True)
                    nodes.append({'instance': row['metric']['instance'], 'percent': number,
                                  'observed_at': observed})
                with c._client(self.config['kube'], timeout=6) as kube_client:
                    inventory, coverage = self.list_scoped(kube_client, 'Свежие узлы для проверки покрытия метрик',
                        '/api/v1/nodes', 'kubectl get nodes -o json', lambda row: True, group='node')
                metric['evidence_ids'].extend(coverage['evidence_ids'])
                from .metric_setup import coverage_checks
                metric['coverage'] = coverage_checks({'metrics': {key: {
                    'percent': percent, 'observed_at': timestamp, 'nodes': nodes}}},
                    {'items': inventory, 'complete': coverage['complete']})[key]
                _, validation_id = self.run('Соответствие метрик узлам выбранного кластера', '',
                    'VALIDATE', 'metrics/coverage', lambda: {'truncated': not metric['coverage']['ok'],
                        'coverage': metric['coverage']}, group='metrics')
                metric['evidence_ids'].append(validation_id)
                metric['complete'] = metric['complete'] and metric['coverage']['ok']
                if metric['coverage']['ok']:
                    metric.update(observed_at=timestamp, percent=percent)
                    self.report['findings'].append({'source': metric['evidence_ids'][0],
                        'fact': f'Текущая загрузка {key}: {percent}%. Покрытие узлов сверено с Kubernetes. Причина конкретного сбоя этим не доказана.'})
                else:
                    metric['error'] = metric['coverage']['error']
                    self.report['findings'].append({'source': metric['evidence_ids'][0],
                        'fact': 'Источник вернул метрики, но их полное соответствие выбранному кластеру не подтверждено.'})
        except c.ConnectorError as exc:
            self.run('Источник выбранной метрики', '', 'CONFIG', 'metrics',
                     lambda: (_ for _ in ()).throw(exc), group='metrics')
        self.finish()
        return self.report


def collect_resource(config, target, cached_snapshot=None, progress=None, *, scenario='deep'):
    """The optional snapshot is deliberately not treated as fresh evidence."""
    collector = ResourceCollector(config, progress, scenario)
    return collector.collect_metric(target) if target.get('metric') else collector.collect_object(target)
