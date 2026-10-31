"""Typed read-only inventory queries and bounded, valid JSON for model context."""
from collections import Counter
from datetime import datetime, timezone
import json
import re
import time

from . import connectors as c
from .dashboard import workload_state


def inventory_intent(question):
    text=question.casefold()
    if re.search(r'\blogs?\b|\bdescribe\b|логи|логов|почему',text):return None
    listing=any(w in text for w in ('вывед','покаж','спис','перечисл','какие','сколько','list','show','which','how many'))
    pods=bool(re.search(r'\bpods?\b|под(?:ы|ов|ах|ам|а)\b',text))
    if not listing:return None
    if pods:
        if re.search(r'(?:не|not|!=|отлич\w* от)[\s\S]{0,45}running|non[- ]?running',text):return 'pods_non_running'
        if any(w in text for w in ('проблем','ошиб','нездоров','не готов','unhealthy','failed','not ready')):return 'pods_unhealthy'
        return 'pods_all'
    if re.search(r'\bnodes?\b|нод|узл',text):return 'nodes_all'
    return None


def pod_row(pod):
    meta,spec,status=pod.get('metadata') or {},pod.get('spec') or {},pod.get('status') or {}
    state=workload_state('pods',pod)
    containers=(status.get('containerStatuses') or [])+(status.get('initContainerStatuses') or [])
    reasons=[x.get('state',{}).get('waiting',{}).get('reason') for x in containers]
    reason=status.get('reason') or next((r for r in reasons if r),None) or ('Completed' if state=='completed' else state)
    return {'name':meta.get('name','?'),'namespace':meta.get('namespace','default'),'uid':meta.get('uid'),
            'phase':status.get('phase','Unknown'),'state':state,'healthy':state in ('ready','completed'),
            'reason':reason,'message':str(status.get('message') or '')[:1200],
            'node':spec.get('nodeName'),'restarts':sum(x.get('restartCount',0) for x in containers),
            'terminating':bool(meta.get('deletionTimestamp'))}


def node_row(node):
    meta,status=node.get('metadata') or {},node.get('status') or {}
    conditions={x.get('type'):x.get('status') for x in status.get('conditions',[])}
    return {'name':meta.get('name','?'),'ready':conditions.get('Ready')=='True','conditions':conditions,
            'version':status.get('nodeInfo',{}).get('kubeletVersion'),'capacity':status.get('capacity',{}),
            'allocatable':status.get('allocatable',{}),'unschedulable':bool(node.get('spec',{}).get('unschedulable'))}


def live_list(config, kind, progress=None):
    if kind not in ('pods','nodes','services'):raise c.ConnectorError('Unsupported read-only inventory kind')
    path=c.RESOURCE_PATHS[kind];started=datetime.now(timezone.utc).isoformat();deadline=time.monotonic()+30
    report={'observed_at':started,'kind':kind,'items':[],'complete':False,'error':None,'records':[]}
    cursor='';seen=set()
    with c._client(config,timeout=8) as client:
        for _ in range(20):
            params={'limit':500}
            if cursor:params['continue']=cursor
            record={'id':f'I{len(report["records"])+1:02d}','check':'Полный список '+kind,'method':'GET','path':path,'query':params,
                    'equivalent_command':f'kubectl get {kind}'+(' -A' if kind!='nodes' else '')+' -o json','started_at':started,'attempted':False}
            try:
                if time.monotonic()>=deadline:raise c.ConnectorError('Время чтения списка исчерпано')
                record['attempted']=True
                data=c._request(client,'GET',c._url(config.get('server'),https_only=True)+path,params=params,deadline=deadline)
                if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise c.ConnectorError('Invalid Kubernetes list response')
                if any(not isinstance(x,dict) or not isinstance(x.get('metadata'),dict) for x in data['items']):raise c.ConnectorError('Invalid Kubernetes object')
                metadata=data.get('metadata') or {};cursor=metadata.get('continue','')
                if not isinstance(cursor,str):raise c.ConnectorError('Invalid pagination cursor')
                report['items'].extend(c._sanitize(x) for x in data['items'])
                record.update(status='ok',data={'count':len(data['items']),'resourceVersion':metadata.get('resourceVersion'),'has_next_page':bool(cursor)})
                if not cursor:report['complete']=True
                elif cursor in seen:raise c.ConnectorError('Повторяющийся cursor: список неполный')
                seen.add(cursor)
            except c.ConnectorError as exc:
                report['error']=str(exc);record.update(status='unavailable',error=str(exc))
            record['finished_at']=datetime.now(timezone.utc).isoformat();report['records'].append(record)
            if progress:progress(report)
            if report['error'] or report['complete']:break
    if not report['complete'] and not report['error']:report['error']='Достигнут лимит страниц; список неполный'
    return report


def inventory_report(config, intent, progress=None):
    kind='nodes' if intent=='nodes_all' else 'pods'
    raw=live_list(config,kind,progress)
    rows=[node_row(x) if kind=='nodes' else pod_row(x) for x in raw['items']]
    if kind=='nodes':selected=rows;counts={'total':len(rows),'ready':sum(x['ready'] for x in rows)}
    else:
        counts={'total':len(rows),'phases':dict(Counter(x['phase'] for x in rows)),
                'healthy':sum(x['healthy'] for x in rows),'unhealthy':sum(not x['healthy'] for x in rows)}
        selected=[x for x in rows if intent=='pods_all' or (x['phase']!='Running' if intent=='pods_non_running' else not x['healthy'])]
        selected.sort(key=lambda x:(x['healthy'],x['namespace'],x['name']))
    return {k:v for k,v in {**raw,'items':None,'intent':intent,'counts':counts,'matched':len(selected),'rows':selected,'unavailable':sum(r['status']!='ok' for r in raw['records'])}.items() if k!='items'}


def escape(value):
    return str(value if value is not None else '—').replace('|','/').replace('\n',' ').replace('\r',' ')[:260]


def inventory_answer(report):
    counts=report['counts'];complete=report['complete'];kind=report['kind']
    lines=['## Данные Kubernetes API',f"Прочитано: {report['observed_at']}. "+('Все страницы списка получены.' if complete else 'Выборка неполная; отсутствие объектов не доказано.')]
    if report['error']:lines.append('Ошибка чтения: '+report['error'])
    if report['error'] and not report['rows']:
        return '\n\n'.join(lines)+'\n\nНевозможно определить состояние объектов: чтение API не выполнено. Это не означает отсутствия проблем.'
    if kind=='nodes':
        lines.append(f"Узлы Ready: {counts['ready']}/{counts['total']}"+('' if complete else ' в прочитанной части'))
        lines+=['| Узел | Ready | Kubelet | CPU | RAM |','| --- | --- | --- | --- | --- |']
        for row in report['rows'][:300]:lines.append('| '+' | '.join(escape(x) for x in (row['name'],'да' if row['ready'] else 'нет',row['version'],row['capacity'].get('cpu'),row['capacity'].get('memory')))+' |')
    else:
        phases=' · '.join(k+': '+str(v) for k,v in counts['phases'].items())
        lines.append(f"Pods: {counts['total']}; здоровы: {counts['healthy']}; требуют проверки: {counts['unhealthy']}. {phases}.")
        lines.append(('Pods не в фазе Running' if report['intent']=='pods_non_running' else 'Нездоровые Pods' if report['intent']=='pods_unhealthy' else 'Все Pods')+f": {report['matched']}.")
        lines.append('Succeeded/Completed с проверенными успешными exitCode — нормальное завершение, не ошибка. Running без Ready не считается здоровым.')
        if not report['rows'] and complete:lines.append('По выбранному фильтру объектов нет.')
        lines+=['| Namespace | Pod | Phase | Оценка / причина | Узел |','| --- | --- | --- | --- | --- |']
        for row in report['rows'][:300]:
            reason=('Здоров / ' if row['healthy'] else 'Проверить / ')+row['reason']
            lines.append('| '+' | '.join(escape(x) for x in (row['namespace'],row['name'],row['phase'],reason,row['node']))+' |')
    if len(report['rows'])>300:lines.append(f"Показаны первые 300 из {len(report['rows'])}. Остальные доступны через /api/clusters/<id>/inventory с offset/limit.")
    lines.append('Это точная выборка API, а не ответ, сгенерированный моделью. Для расследования конкретной причины откройте ✧ у объекта; список сам по себе не доказывает отказ приложения.')
    return '\n\n'.join(lines[:3])+'\n\n'+'\n'.join(lines[3:])


def compact_snapshot(snapshot, namespace=None, limit=30000):
    """Summarize before serializing. Never cut serialized JSON in the middle."""
    if not isinstance(limit,int) or limit<512:raise ValueError('Context limit must be at least 512 characters')
    from .diagnostics import small
    resources=snapshot.get('resources',{});out={'observed_at':snapshot.get('observed_at'),'complete':snapshot.get('complete',False),'errors':snapshot.get('errors',[]),'cluster_info':snapshot.get('cluster_info',{}),'resources':{}}
    # Server metadata and extension resource names are not intrinsically bounded.
    for field in ('observed_at','errors','cluster_info'):out[field]=small(out[field],1500)
    pods=resources.get('pods',{});rows=[pod_row(p) for p in pods.get('items',[]) if not namespace or p.get('metadata',{}).get('namespace')==namespace]
    rows.sort(key=lambda p:(p['healthy'],p['phase']=='Running',p['namespace'],p['name']))
    out['resources']['pods']={'count':len(rows),'phases':dict(Counter(p['phase'] for p in rows)),
        'healthy':sum(p['healthy'] for p in rows),'unhealthy':sum(not p['healthy'] for p in rows),'items':rows[:100],
        'truncated':bool(pods.get('truncated')) or len(rows)>100,'error':pods.get('error') if 'pods' in resources else 'Список Pods не получен'}
    nodes=resources.get('nodes',{})
    if 'pods' not in resources or pods.get('error'):
        out['resources']['pods'].update(count=None,healthy=None,unhealthy=None)
    out['resources']['nodes']={'items':[node_row(n) for n in nodes.get('items',[])[:30]],'truncated':bool(nodes.get('truncated')) or len(nodes.get('items',[]))>30,'error':nodes.get('error')}
    for kind,entry in resources.items():
        if kind in ('pods','nodes'):continue
        items=[x for x in entry.get('items',[]) if not namespace or x.get('metadata',{}).get('namespace') in (None,namespace)]
        out['resources'][kind]={'count':len(items),'error':entry.get('error'),'truncated':entry.get('truncated',False)}
    for row in out['resources']['nodes']['items']:
        for field in ('capacity','allocatable','conditions'):row[field]=small(row[field],800)
    while len(json.dumps(out['resources']['nodes'],ensure_ascii=False))>limit//3 and out['resources']['nodes']['items']:
        out['resources']['nodes']['items'].pop();out['resources']['nodes']['truncated']=True
    def oversized():return len(json.dumps(out,ensure_ascii=False))>limit
    while oversized() and out['resources']['pods']['items']:
        out['resources']['pods']['items'].pop();out['resources']['pods']['truncated']=True
    while oversized() and out['resources']['nodes']['items']:
        out['resources']['nodes']['items'].pop();out['resources']['nodes']['truncated']=True
    if oversized():
        out['context_truncated']=True
        # Keep Pod counts even when metadata alone exceeds the budget.
        for field in ('errors','cluster_info','observed_at'):
            if oversized():out[field]=small(out[field],100)
        for kind in list(out['resources']):
            if kind not in ('pods','nodes') and oversized():del out['resources'][kind]
        for kind in ('pods','nodes'):
            for field in ('error','phases'):
                if field in out['resources'][kind] and oversized():
                    out['resources'][kind][field]=small(out['resources'][kind][field],100)
    if oversized():
        # Very small requested budgets still produce valid, explicitly partial JSON.
        pod_summary=out['resources']['pods']
        out={'complete':False,'context_truncated':True,'resources':{'pods':{
            k:pod_summary[k] for k in ('count','healthy','unhealthy','items')}}}
        out['resources']['pods']['truncated']=True
    return out
