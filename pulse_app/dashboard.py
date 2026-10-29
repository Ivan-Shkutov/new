"""Deterministic dashboard facts; no model-generated states or synthetic metrics."""
from collections import Counter
from datetime import datetime, timezone

WORKLOADS = ('daemonsets', 'deployments', 'jobs', 'cronjobs', 'replicasets', 'statefulsets', 'pods')
LABELS = {'daemonsets':'Готовы', 'deployments':'Готовы', 'jobs':'Завершены', 'cronjobs':'Включены', 'replicasets':'Готовы', 'statefulsets':'Готовы', 'pods':'Ready'}


def workload_state(kind, obj):
    spec, status, meta = obj.get('spec', {}), obj.get('status', {}), obj.get('metadata', {})
    if meta.get('deletionTimestamp'):
        return 'terminating'
    if kind == 'pods':
        phase = status.get('phase', 'Unknown')
        if phase == 'Succeeded':
            return 'completed'
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


def summarize(snapshot, cid, cname, interval=30):
    observed = snapshot.get('observed_at')
    stale = not observed
    if observed:
        try:
            stale = (datetime.now(timezone.utc) - datetime.fromisoformat(observed)).total_seconds() > max(120, 3 * interval)
        except (ValueError, TypeError):
            stale = True
    workloads, issues = {}, []
    resources = snapshot.get('resources', {})
    for kind in WORKLOADS:
        entry = resources.get(kind)
        items = entry.get('items', []) if entry else []
        states = Counter(workload_state(kind, obj) for obj in items)
        positive = 'completed' if kind == 'jobs' else 'enabled' if kind == 'cronjobs' else 'ready'
        usable = bool(observed and entry is not None and not entry.get('error'))
        partial = bool(entry and entry.get('truncated'))
        workloads[kind] = {'total':len(items) if usable else None,
                           'positive':states[positive] if usable else None,
                           'percent':round(100 * states[positive] / len(items), 1) if usable and items else None,
                           'label':LABELS[kind], 'states':dict(states) if usable else {},
                           'partial':partial, 'stale':stale, 'error':entry.get('error') if entry else 'Ещё нет данных'}
        if not usable:
            continue
        for obj in items:
            state = workload_state(kind, obj)
            if state not in ('not_ready', 'failed', 'pending', 'updating'):
                continue
            meta = obj.get('metadata', {})
            issues.append({'cluster_id':cid, 'cluster_name':cname, 'kind':kind,
                           'name':meta.get('name', ''), 'namespace':meta.get('namespace', ''),
                           'uid':meta.get('uid', ''), 'state':state, 'stale':stale,
                           'observed_at':observed})
    for obj in resources.get('nodes', {}).get('items', []):
        conditions = {c.get('type'):c.get('status') for c in obj.get('status', {}).get('conditions', [])}
        for name, expected in [('Ready','True'), ('MemoryPressure','False'), ('DiskPressure','False'), ('PIDPressure','False')]:
            if name in conditions and conditions[name] != expected:
                issues.append({'cluster_id':cid,'cluster_name':cname,'kind':'nodes','name':obj.get('metadata',{}).get('name',''), 'namespace':'','uid':obj.get('metadata',{}).get('uid',''),'state':name+': '+conditions[name], 'stale':stale,'observed_at':observed})
    return {'workloads':workloads, 'issues':issues[:40], 'issues_total':len(issues), 'stale':stale}


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
                        'label':LABELS[kind],'states':dict(states), 'partial':len(valid)!=len(rows) or any(r['partial'] for r in rows),
                        'stale':any(r['stale'] for r in rows), 'error':None if valid else 'Нет доступных снимков'}
    return output
