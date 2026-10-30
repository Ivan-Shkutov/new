"""Compact cluster facts, with capacity and declared allocation kept separate from usage."""
from decimal import Decimal,InvalidOperation
import re
import math

FACTORS={'':1,'n':1e-9,'u':1e-6,'m':1e-3,'k':1000,'K':1000,'M':1000**2,'G':1000**3,'T':1000**4,'P':1000**5,'E':1000**6,
         'Ki':1024,'Mi':1024**2,'Gi':1024**3,'Ti':1024**4,'Pi':1024**5,'Ei':1024**6}


def quantity(value):
    if value is None:return None
    match=re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)',str(value))
    if not match or match[2] not in FACTORS:return None
    try:
        result=Decimal(match[1])*Decimal(str(FACTORS[match[2]]))
        number=float(result)
        return number if math.isfinite(number) and number>=0 else None
    except (InvalidOperation,OverflowError):return None


def pod_budget(pod,resource,field):
    spec=pod.get('spec',{})
    if spec.get('resources') or pod.get('status',{}).get('resize'):return None
    def read(container):return quantity(container.get('resources',{}).get(field,{}).get(resource,'0'))
    app=[read(c) for c in spec.get('containers',[])]
    init=[(c,read(c)) for c in spec.get('initContainers',[])]
    if any(v is None for v in app) or any(v is None for _,v in init):return None
    sidecars=0;maximum=0
    for container,value in init:
        if container.get('restartPolicy')=='Always':
            sidecars+=value;maximum=max(maximum,sidecars)
        else:maximum=max(maximum,sidecars+value)
    result=max(sum(app)+sidecars,maximum)
    overhead=quantity(spec.get('overhead',{}).get(resource,'0'))
    if overhead is None:return None
    return result+overhead if field=='requests' or result>0 else result


def summarize(snapshot):
    resources=snapshot.get('resources',{})
    nodes_entry=resources.get('nodes',{});pods_entry=resources.get('pods',{})
    nodes=nodes_entry.get('items',[]) if not nodes_entry.get('error') else []
    pods=pods_entry.get('items',[]) if not pods_entry.get('error') else []
    nodes_known='nodes' in resources and not nodes_entry.get('error') and not nodes_entry.get('truncated') and bool(nodes)
    pods_known='pods' in resources and not pods_entry.get('error') and not pods_entry.get('truncated')
    rows=[]
    for node in nodes:
        meta,spec,status=node.get('metadata',{}),node.get('spec',{}),node.get('status',{})
        name=meta.get('name');info=status.get('nodeInfo',{});labels=meta.get('labels') or {}
        active=[p for p in pods if p.get('spec',{}).get('nodeName')==name and p.get('status',{}).get('phase') not in ('Succeeded','Failed')]
        capacity={r:quantity(status.get('capacity',{}).get(r)) for r in ('cpu','memory','ephemeral-storage')}
        allocatable={r:quantity(status.get('allocatable',{}).get(r)) for r in ('cpu','memory','ephemeral-storage')}
        budget={};risks=[]
        for r in ('cpu','memory'):
            totals={}
            for field in ('requests','limits'):
                values=[pod_budget(p,r,field) for p in active]
                totals[field]=sum(values) if pods_known and all(v is not None for v in values) else None
                base=allocatable[r]
                totals[field+'_percent']=round(100*totals[field]/base,1) if base and totals[field] is not None else None
            totals['containers_without_limit']=sum(r not in c.get('resources',{}).get('limits',{}) for p in active for c in p.get('spec',{}).get('containers',[])+[c for c in p.get('spec',{}).get('initContainers',[]) if c.get('restartPolicy')=='Always']) if pods_known else None
            budget[r]=totals
            if r=='cpu' and (totals['requests_percent'] or 0)>=90:risks.append('CPU requests ≥90%')
            if r=='memory' and (totals['limits_percent'] or 0)>100:risks.append('RAM limits >100%')
        roles=[k.split('/',1)[1] for k in labels if k.startswith('node-role.kubernetes.io/')]
        control=any(r in ('control-plane','master') for r in roles)
        rows.append({'name':name,'roles':roles or ['worker'],'control_plane':control,'version':info.get('kubeletVersion'),
                     'os':info.get('osImage'),'kernel':info.get('kernelVersion'),'runtime':info.get('containerRuntimeVersion'),
                     'ip':next((a.get('address') for a in status.get('addresses',[]) if a.get('type')=='InternalIP'),None),
                     'capacity':capacity,'allocatable':allocatable,'budget':budget,'budget_risks':risks,
                     'active_pods':len(active) if pods_known else None,'unschedulable':bool(spec.get('unschedulable')),
                     'conditions':status.get('conditions',[]),'taints':spec.get('taints',[])})
    totals={}
    for r in ('cpu','memory'):
        totals[r]=sum(n['capacity'][r] for n in rows) if nodes_known and all(n['capacity'][r] is not None for n in rows) else None
    ns=resources.get('namespaces',{})
    versions=sorted({n['version'] for n in rows if n['version']})
    return {'api_version':snapshot.get('cluster_info',{}).get('server_version'),'api_version_error':snapshot.get('cluster_info',{}).get('error'),
            'kubelet_versions':versions,'capacity':totals,'nodes_known':nodes_known,'budget_complete':nodes_known and pods_known and all((n['allocatable'][r] or 0)>0 and n['budget'][r][f] is not None for n in rows for r in ('cpu','memory') for f in ('requests','limits')),
            'node_roles':{'control_plane':sum(n['control_plane'] for n in rows),'workers':sum(not n['control_plane'] for n in rows)},
            'namespaces':len(ns.get('items',[])) if ns and not ns.get('error') and not ns.get('truncated') else None,
            'budget_risk_nodes':sum(bool(n['budget_risks']) for n in rows),'nodes':rows}
