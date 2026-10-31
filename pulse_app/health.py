"""Explainable health of observed Kubernetes objects, independent of model output."""
from collections import Counter
from datetime import datetime, timezone

LABELS = {'healthy':'Готово', 'critical':'Есть проблемы', 'warning':'Требует внимания',
          'unknown':'Недостаточно данных', 'stale':'Данные устарели', 'paused':'Сбор приостановлен'}
SEVERITY_ORDER = {'critical':0, 'warning':1, 'info':2}


def age(value, now):
    try:
        dt = datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None:
            return None
        return max(0,(now-dt).total_seconds())
    except (ValueError, TypeError, AttributeError):
        return None


def assess(snapshot, cid, cname, workloads, interval=30, enabled=True, issue_limit=40):
    now = datetime.now(timezone.utc)
    observed = snapshot.get('observed_at')
    elapsed = age(observed,now)
    stale = elapsed is None or elapsed > max(120, interval*3)
    resources = snapshot.get('resources',{})
    issues, node_rows, events = [], [], []

    def issue(kind, obj, severity, title, detail):
        m = obj.get('metadata',{})
        issues.append({'cluster_id':cid,'cluster_name':cname,'kind':kind,
                       'name':m.get('name','Без имени'),'namespace':m.get('namespace',''),
                       'uid':m.get('uid',''),'severity':severity,'title':title,'detail':detail,
                       'reason':obj.get('status',{}).get('reason'),'phase':obj.get('status',{}).get('phase'),
                       'state':title,'stale':stale or not enabled,'observed_at':observed})

    def entry_items(kind):
        entry=resources.get(kind)
        return entry.get('items',[]) if entry and not entry.get('error') else []

    node_entry=resources.get('nodes')
    known_nodes=bool(observed and node_entry is not None and not node_entry.get('error'))
    node_ready=0
    for obj in entry_items('nodes'):
        m,spec,status=obj.get('metadata',{}),obj.get('spec',{}),obj.get('status',{})
        conditions={c.get('type'):c for c in status.get('conditions',[])}
        ready=conditions.get('Ready',{}).get('status')
        node_ready += ready == 'True'
        problems=[]
        if ready != 'True':
            reason=conditions.get('Ready',{}).get('reason') or ('нет condition Ready' if ready is None else 'Ready='+str(ready))
            problems.append(reason)
            issue('nodes',obj,'critical' if ready is not None else 'warning','Узел не готов',reason)
        for name in ('MemoryPressure','DiskPressure','PIDPressure','NetworkUnavailable'):
            if conditions.get(name,{}).get('status') == 'True':
                problems.append(name)
                issue('nodes',obj,'critical',name,conditions[name].get('reason') or 'Condition '+name+'=True')
        pods=sum(p.get('spec',{}).get('nodeName')==m.get('name') and p.get('status',{}).get('phase') not in ('Succeeded','Failed') for p in entry_items('pods'))
        node_rows.append({'name':m.get('name',''),'ready':ready=='True','unknown':ready is None,
                          'status':'critical' if (ready is not None and ready!='True') or any(conditions.get(k,{}).get('status')=='True' for k in ('MemoryPressure','DiskPressure','PIDPressure','NetworkUnavailable')) else 'unknown' if ready is None else 'healthy',
                          'problems':problems,'unschedulable':bool(spec.get('unschedulable')),
                          'pods':pods if resources.get('pods') and not resources['pods'].get('error') else None,
                          'version':status.get('nodeInfo',{}).get('kubeletVersion','—'),
                          'ip':next((a.get('address') for a in status.get('addresses',[]) if a.get('type')=='InternalIP'),'—')})

    from .dashboard import completed_pod_ok
    restarts, active_pods, ready_pods, completed_pods, failed_pods, unverified_pods, terminating_pods = 0,0,0,0,0,0,0
    for obj in entry_items('pods'):
        m,status=obj.get('metadata',{}),obj.get('status',{})
        phase=status.get('phase','Unknown')
        containers=status.get('containerStatuses',[])+status.get('initContainerStatuses',[])
        restarts+=sum(c.get('restartCount',0) for c in containers)
        if phase=='Failed':
            failed_pods += 1
            issue('pods',obj,'critical','Pod завершился ошибкой',((status.get('reason') or 'Failed')+': '+status.get('message',''))[:2000])
        if phase=='Succeeded':
            if completed_pod_ok(obj):
                completed_pods += 1
            else:
                unverified_pods += 1
                issue('pods',obj,'critical','Завершение Pod не подтверждено','Succeeded без полного набора успешных exitCode; проверьте containerStatuses.')
        if phase in ('Succeeded','Failed'):
            continue
        active_pods+=1
        if m.get('deletionTimestamp'):
            terminating_pods+=1
            duration=age(m['deletionTimestamp'],now)
            stuck=duration is not None and duration>=300
            issue('pods',obj,'critical' if stuck else 'warning','Pod долго завершается' if stuck else 'Pod завершается',
                  'Срок удаления истёк более 5 минут назад; проверьте finalizers, события и состояние узла.' if stuck else
                  'Удаление запрошено; Pod ещё существует и не учитывается как здоровый.')
            continue
        conditions={c.get('type'):c for c in status.get('conditions',[])}
        is_ready=phase=='Running' and conditions.get('Ready',{}).get('status')=='True'
        ready_pods+=is_ready
        if is_ready:
            continue
        waiting=[c.get('state',{}).get('waiting',{}).get('reason') for c in containers]
        hard=next((r for r in waiting if r in ('CrashLoopBackOff','ImagePullBackOff','ErrImagePull','CreateContainerConfigError','CreateContainerError','InvalidImageName','RunContainerError')),None)
        ready_condition=conditions.get('Ready',{})
        duration=age(ready_condition.get('lastTransitionTime') or m.get('creationTimestamp'),now)
        if hard:
            issue('pods',obj,'critical',hard,'Контейнер сейчас не запускается. Проверьте события и логи.')
        elif phase=='Pending':
            scheduled=conditions.get('PodScheduled',{})
            reason=scheduled.get('reason') or 'Pending'
            issue('pods',obj,'critical' if duration is not None and duration>=300 else 'warning',reason,
                  'Ожидание более 5 минут.' if duration is not None and duration>=300 else 'Ожидание запуска; проверьте длительность и события.')
        elif phase=='Running':
            issue('pods',obj,'critical' if duration is not None and duration>=180 else 'warning','Pod не Ready',
                  'Готовность отсутствует более 3 минут.' if duration is not None and duration>=180 else 'Контейнеры запускаются или не проходят readiness.')
        else:
            issue('pods',obj,'warning','Состояние Pod неизвестно','API не подтвердил Ready.')

    for kind in workloads:
        if kind in ('pods','cronjobs'):
            continue
        for obj in entry_items(kind):
            from .dashboard import workload_state
            state=workload_state(kind,obj)
            if state=='failed':
                issue(kind,obj,'warning','Задание завершилось ошибкой','Сохранённый Failed Job; проверьте время и актуальность.')
            elif state in ('not_ready','updating'):
                status=obj.get('status',{})
                deadline=any(c.get('type')=='Progressing' and c.get('status')=='False' and c.get('reason')=='ProgressDeadlineExceeded' for c in status.get('conditions',[]))
                unavailable=any(c.get('type')=='Available' and c.get('status')=='False' and (age(c.get('lastTransitionTime'),now) or 0)>=300 for c in status.get('conditions',[]))
                issue(kind,obj,'critical' if deadline or unavailable else 'warning','Недостаточно готовых реплик' if state=='not_ready' else 'Обновление контроллера',
                      'Превышен progress deadline.' if deadline else 'Available=False более 5 минут.' if unavailable else 'Сверьте желаемые и готовые реплики; возможен плановый rollout.')

    for obj in entry_items('persistentvolumeclaims'):
        phase=obj.get('status',{}).get('phase')
        if phase in ('Lost','Pending'):
            issue('persistentvolumeclaims',obj,'critical' if phase=='Lost' else 'warning','PVC '+phase,'Проверьте StorageClass, PV и события тома.')
    for obj in entry_items('events'):
        if obj.get('type')!='Warning':
            continue
        ts=obj.get('lastTimestamp') or obj.get('eventTime') or obj.get('series',{}).get('lastObservedTime') or obj.get('metadata',{}).get('creationTimestamp')
        seconds=age(ts,now)
        if seconds is not None and seconds<=900:
            ref=obj.get('involvedObject',{}) or obj.get('regarding',{})
            events.append({'time':ts,'reason':obj.get('reason','Warning'),'message':str(obj.get('message',''))[:500],
                           'name':ref.get('name',''),'kind':ref.get('kind',''),'namespace':ref.get('namespace',obj.get('metadata',{}).get('namespace','')),
                           'count':obj.get('count',obj.get('series',{}).get('count',1))})
    events.sort(key=lambda e:e['time'],reverse=True)
    issues.sort(key=lambda i:(SEVERITY_ORDER[i['severity']],0 if i['kind']=='nodes' else 1,i['kind'],i['namespace'],i['name']))
    counts=Counter(i['severity'] for i in issues)
    required=('nodes','pods','daemonsets','deployments','jobs','cronjobs','replicasets','statefulsets','persistentvolumeclaims')
    missing=[kind for kind in required if kind not in resources or resources[kind].get('error') or resources[kind].get('truncated')]
    incomplete=bool(missing or snapshot.get('complete') is False or snapshot.get('errors') or any(r['unknown'] for r in node_rows))
    status='paused' if not enabled else 'unknown' if elapsed is None else 'stale' if stale else 'critical' if counts['critical'] else 'unknown' if incomplete or not node_rows else 'warning' if counts['warning'] else 'healthy'
    nodes_total=len(node_rows) if known_nodes else None
    node_tone='unknown' if stale or not enabled or not known_nodes or not nodes_total or node_entry.get('truncated') else 'critical' if any(r['status']=='critical' for r in node_rows) else 'unknown' if any(r['unknown'] for r in node_rows) else 'healthy'
    if any(r['unknown'] for r in node_rows) and node_tone=='healthy':
        node_tone='unknown'
    for kind,value in workloads.items():
        related=[i for i in issues if i['kind']==kind]
        value['health']='unknown' if stale or not enabled or value['total'] is None or value['partial'] else 'critical' if any(i['severity']=='critical' for i in related) else 'warning' if related else 'healthy' if value['total'] else 'neutral'
        value['problems']=len(related)
    return {'status':status,'label':LABELS[status],'critical':counts['critical'],'warning':counts['warning'],
            'incomplete':incomplete,'missing':missing,'age_seconds':round(elapsed) if elapsed is not None else None,
            'nodes':{'ready':node_ready if known_nodes else None,'total':nodes_total,'health':node_tone},
            'pods':{'ready':ready_pods if resources.get('pods') and not resources['pods'].get('error') else None,
                    'active':active_pods if resources.get('pods') and not resources['pods'].get('error') else None,
                    'total':len(entry_items('pods')) if resources.get('pods') and not resources['pods'].get('error') else None,
                    'healthy':ready_pods+completed_pods if resources.get('pods') and not resources['pods'].get('error') else None,
                    'completed':completed_pods,'failed':failed_pods,'unverified':unverified_pods,'terminating':terminating_pods,
                    'health':'unknown' if stale or not enabled or resources.get('pods',{}).get('error') or resources.get('pods',{}).get('truncated') or 'pods' not in resources else 'critical' if any(i['kind']=='pods' and i['severity']=='critical' for i in issues) else 'warning' if any(i['kind']=='pods' for i in issues) else 'healthy',
                    'restarts':restarts,'restart_note':'Сумма счётчиков текущих Pod; не частота за интервал'},
            'node_rows':node_rows,'events':events[:10],'issues':issues if issue_limit is None else issues[:issue_limit],'issues_total':len(issues)}


def aggregate(rows):
    if not rows:
        return {'status':'unknown','label':LABELS['unknown'],'critical':0,'warning':0,'nodes':{'ready':None,'total':None,'health':'unknown'}}
    statuses={r['status'] for r in rows}
    status='paused' if statuses=={'paused'} else 'critical' if 'critical' in statuses else 'stale' if 'stale' in statuses else 'unknown' if statuses & {'unknown','paused'} else 'warning' if 'warning' in statuses else 'healthy'
    known=[r for r in rows if r['nodes']['total'] is not None]
    nodes={'ready':sum(r['nodes']['ready'] for r in known) if known else None,'total':sum(r['nodes']['total'] for r in known) if known else None,
           'health':'critical' if any(r['nodes']['health']=='critical' for r in rows) else 'healthy' if all(r['nodes']['health']=='healthy' for r in rows) else 'unknown'}
    return {'status':status,'label':LABELS[status],'critical':sum(r['critical'] for r in rows if r['status'] not in ('stale','paused','unknown')),'warning':sum(r['warning'] for r in rows if r['status'] not in ('stale','paused','unknown')),'nodes':nodes}
