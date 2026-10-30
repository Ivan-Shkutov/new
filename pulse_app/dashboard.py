"""Deterministic dashboard facts; no model-generated states or synthetic metrics."""
from collections import Counter
from datetime import datetime, timezone

WORKLOADS = ('daemonsets', 'deployments', 'jobs', 'cronjobs', 'replicasets', 'statefulsets', 'pods')
LABELS = {'daemonsets':'Готовы', 'deployments':'Готовы', 'jobs':'Завершены', 'cronjobs':'Включены', 'replicasets':'Готовы', 'statefulsets':'Готовы', 'pods':'Ready'}


def completed_pod_ok(obj):
    """Succeeded is accepted only with complete, successful container evidence."""
    spec, status = obj.get('spec', {}), obj.get('status', {})
    if status.get('phase') != 'Succeeded':
        return False
    main = status.get('containerStatuses', [])
    if not main or len(main) < len(spec.get('containers', [])):
        return False
    init = status.get('initContainerStatuses', [])
    if len(init) < len(spec.get('initContainers', [])):
        return False
    if not {c.get('name') for c in spec.get('containers',[])} <= {c.get('name') for c in main}:
        return False
    if not {c.get('name') for c in spec.get('initContainers',[])} <= {c.get('name') for c in init}:
        return False
    sidecars = {c.get('name') for c in spec.get('initContainers', []) if c.get('restartPolicy') == 'Always'}
    for container in main + init:
        terminated = container.get('state', {}).get('terminated')
        if not terminated or (terminated.get('exitCode') != 0 and container.get('name') not in sidecars):
            return False
    return True


def workload_state(kind, obj):
    spec, status, meta = obj.get('spec', {}), obj.get('status', {}), obj.get('metadata', {})
    if meta.get('deletionTimestamp') and not (kind=='pods' and status.get('phase') in ('Succeeded','Failed')):
        return 'terminating'
    if kind == 'pods':
        phase = status.get('phase', 'Unknown')
        if phase == 'Succeeded':
            return 'completed' if completed_pod_ok(obj) else 'completion_unverified'
        if phase == 'Failed':
            return 'failed'
        if phase == 'Running' and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', [])):
            return 'ready'
        return 'pending' if phase == 'Pending' else 'not_ready' if phase == 'Running' else 'unknown'
    if kind == 'jobs':
        conditions = {c.get('type') for c in status.get('conditions', []) if c.get('status') == 'True'}
        if 'Complete' in conditions:
            return 'completed'
        if 'Failed' in conditions:
            return 'failed'
        if spec.get('suspend'):
            return 'suspended'
        return 'active' if status.get('active', 0) else 'pending'
    if kind == 'cronjobs':
        return 'suspended' if spec.get('suspend', False) else 'enabled'
    if meta.get('generation') is not None and status.get('observedGeneration', -1) < meta['generation']:
        return 'updating'
    desired = status.get('desiredNumberScheduled') if kind == 'daemonsets' else spec.get('replicas', 1)
    if desired is None:
        return 'unknown'
    if desired == 0:
        return 'scaled_zero'
    ready = status.get('numberReady', 0) if kind == 'daemonsets' else status.get('readyReplicas', 0)
    if ready < desired:
        return 'not_ready'
    # Readiness is distinct from rollout completion and intentionally named as such.
    return 'ready'


def summarize(snapshot, cid, cname, interval=30, issue_limit=40):
    from .health import assess, age
    observed = snapshot.get('observed_at')
    elapsed = age(observed,datetime.now(timezone.utc))
    stale = elapsed is None or elapsed > max(120,3*interval)
    workloads = {}
    for kind in WORKLOADS:
        entry = snapshot.get('resources',{}).get(kind)
        items = entry.get('items',[]) if entry else []
        states = Counter(workload_state(kind,obj) for obj in items)
        positive = 'completed' if kind=='jobs' else 'enabled' if kind=='cronjobs' else 'ready'
        positives = states[positive] + (states['completed'] if kind == 'pods' else 0)
        usable = bool(observed and entry is not None and not entry.get('error'))
        workloads[kind] = {'total':len(items) if usable else None,
                           'positive':positives if usable else None,
                           'percent':round(100*positives/len(items),1) if usable and items else None,
                           'label':'Здоровы' if kind=='pods' else LABELS[kind], 'states':dict(states) if usable else {},
                           'partial':bool(entry and entry.get('truncated')), 'stale':stale,
                           'error':entry.get('error') if entry else 'Ещё нет данных'}
    health = assess(snapshot,cid,cname,workloads,interval,issue_limit=issue_limit)
    from .inventory import summarize as cluster_profile
    return {'workloads':workloads,'issues':health['issues'],'issues_total':health['issues_total'],'stale':stale,'health':health,'profile':cluster_profile(snapshot)}


def combine(summaries):
    output = {}
    for kind in WORKLOADS:
        rows = [s['workloads'][kind] for s in summaries]
        valid = [r for r in rows if r['total'] is not None]
        total = sum(r['total'] for r in valid) if valid else None
        positive = sum(r['positive'] for r in valid) if valid else None
        states = Counter()
        for row in valid:
            states.update(row['states'])
        output[kind] = {'total':total,'positive':positive,'percent':round(100*positive/total,1) if total else None,
                        'label':'Здоровы' if kind=='pods' else LABELS[kind],'states':dict(states), 'partial':len(valid)!=len(rows) or any(r['partial'] for r in rows),
                        'stale':any(r['stale'] for r in rows), 'error':None if valid else 'Нет доступных снимков',
                        'health':'critical' if any(r.get('health')=='critical' for r in rows) else 'unknown' if any(r.get('health')=='unknown' for r in rows) else 'warning' if any(r.get('health')=='warning' for r in rows) else 'healthy' if total else 'neutral',
                        'problems':sum(r.get('problems',0) for r in rows)}
    return output
