"""Deterministic, bounded read-only Pod investigation with a durable evidence journal."""
from datetime import datetime, timedelta, timezone
import json
import ipaddress
import re
import time
from . import connectors as c

OWNER_PATHS = {'ReplicaSet':'apps/v1/replicasets','Deployment':'apps/v1/deployments',
               'StatefulSet':'apps/v1/statefulsets','DaemonSet':'apps/v1/daemonsets',
               'Job':'batch/v1/jobs','CronJob':'batch/v1/cronjobs'}


def utc():
    return datetime.now(timezone.utc).isoformat()


def small(value, limit=9000):
    value=c.redact_structure(c._sanitize(value))
    text=json.dumps(value,ensure_ascii=False)
    return value if len(text)<=limit else {'excerpt':text[:limit], 'truncated':True}


def pod_facts(pod):
    spec=pod.get('spec',{})
    return {'metadata':pod.get('metadata',{}),'status':pod.get('status',{}),
            'spec':{k:spec[k] for k in ('nodeName','restartPolicy','priorityClassName','schedulerName','volumes','tolerations','affinity','overhead') if k in spec},
            'containers':[{k:v for k,v in container.items() if k in ('name','image','resources','readinessProbe','livenessProbe','startupProbe','restartPolicy')} for container in spec.get('containers',[])+spec.get('initContainers',[])]}


class Collector:
    def __init__(self, config, progress=None):
        self.config=config; self.progress=progress
        self.report={'started_at':utc(),'records':[],'findings':[], 'note':'Выполнены HTTP API-запросы. Команды kubectl — эквиваленты для воспроизведения, shell не запускался.'}
        self.deadline=time.monotonic()+90

    def save(self):
        if self.progress:self.progress(self.report)

    def run(self, label, command, method, path, call, project=lambda x:x, query=None):
        row={'id':f'E{len(self.report["records"])+1:02d}','check':label,'equivalent_command':command,
             'method':method,'path':path,'query':query or {},'started_at':utc(),'attempted':False}
        try:
            if time.monotonic()>=self.deadline or len(self.report['records'])>=55:
                raise c.ConnectorError('Бюджет сбора исчерпан; проверка не выполнена')
            row['attempted']=True
            result=call();row.update(status='ok',data=small(project(result)))
        except Exception as exc:
            result=None;row.update(status='unavailable',error=str(exc) if isinstance(exc,c.ConnectorError) else 'Источник не вернул поддерживаемые данные')
        row['finished_at']=utc();self.report['records'].append(row);self.save()
        return result,row['id']

    def get(self, client, label, path, command, params=None, project=lambda x:x):
        return self.run(label,command,'GET',path,lambda:c._request(client,'GET',c._url(self.config['kube']['server'],https_only=True)+path,params=params or {},deadline=self.deadline),project,query=params)

    def collect(self, namespace, name, uid='', cached=None):
        ns=c._name(namespace,'namespace');name=c._name(name,'pod')
        prefix=f'/api/v1/namespaces/{ns}'
        target={'namespace':namespace,'name':name,'expected_uid':uid}
        self.report['target']=target
        node_ips=[]
        with c._client(self.config['kube'],timeout=6) as client:
            pod,pid=self.get(client,'Выбранный Pod: status.message, exitCode, requests/limits',prefix+'/pods/'+name,
                              f'kubectl -n {ns} get pod {name} -o json',project=pod_facts)
            pod_source='live'
            if pod and (pod.get('metadata',{}).get('name')!=name or not pod.get('metadata',{}).get('uid')):
                pod=None
                self.report['records'][-1].update(status='unavailable',error='API не вернул объект Pod с name/UID')
            if pod and uid and pod.get('metadata',{}).get('uid')!=uid:
                self.report['records'][-1].update(status='identity_changed',data={'name':name,'observed_uid':pod.get('metadata',{}).get('uid'),'expected_uid':uid,'excluded':True})
                self.report['findings'].append({'source':pid,'fact':'Имя занято другим UID. Новый Pod не используется как доказательство прежней проблемы.'})
                pod=None
            if not pod and cached and (not uid or cached.get('metadata',{}).get('uid')==uid):
                pod=cached;pod_source='cached'
                _,pid=self.run('Последний сохранённый Pod (не live)', '', 'CACHE', 'snapshot/pods',lambda:pod,project=pod_facts)
            if not pod:
                pod_source='unavailable'
                self.report['findings'].append({'source':pid,'fact':'Исходный Pod недоступен. Причина по его status не установлена.'})
                pod={'metadata':{'name':name,'namespace':ns,'uid':uid},'status':{},'spec':{}}
            meta,status,spec=pod.get('metadata',{}),pod.get('status',{}),pod.get('spec',{})
            uid=meta.get('uid') or uid;target['uid']=uid
            self.report['pod']={'uid':uid,'node':spec.get('nodeName'),'phase':status.get('phase'),'reason':status.get('reason'),
                                'message':status.get('message'),'created_at':meta.get('creationTimestamp'),'source_type':pod_source,'observed_at':self.report['started_at'] if pod_source=='live' else None}
            self.report['findings'].append({'source':pid,'fact':f"Pod phase={status.get('phase','Unknown')}; reason={status.get('reason','не указан')}; status.message={status.get('message') or 'не заполнено'}."})
            self.report['findings'].append({'source':pid,'fact':'Время наблюдения не является временем аварии. Время создания Pod также не является временем выселения.'})
            if uid:self.get(client,'События выбранного UID',prefix+'/events',f'kubectl -n {ns} get events --field-selector involvedObject.uid={uid} -o json',{'fieldSelector':'involvedObject.uid='+uid,'limit':200})
            node=spec.get('nodeName')
            if node:
                node_obj,nid=self.get(client,'Фактический узел Pod', '/api/v1/nodes/'+c._name(node,'node'),f'kubectl get node {node} -o json')
                for address in (node_obj or {}).get('status',{}).get('addresses',[]):
                    if address.get('type')=='InternalIP':
                        try:node_ips.append(str(ipaddress.ip_address(address['address'])))
                        except (ValueError,KeyError):pass
                self.report['findings'].append({'source':pid,'fact':'Фактический узел из spec.nodeName: '+node+'. Текущие Ready/Pressure не исключают прошлого давления.'})
                self.get(client,'События фактического узла','/api/v1/events',f'kubectl get events -A --field-selector involvedObject.kind=Node,involvedObject.name={node} -o json',{'fieldSelector':'involvedObject.kind=Node,involvedObject.name='+node,'limit':200})
            owners=[];obj=pod;owner_uids=set();selector={}
            for _ in range(3):
                ref=next((r for r in obj.get('metadata',{}).get('ownerReferences',[]) if r.get('controller')),None)
                if not ref or ref.get('kind') not in OWNER_PATHS:break
                kind,oname=ref['kind'],c._name(ref.get('name'),'owner')
                api,version,resource=OWNER_PATHS[kind].split('/') if OWNER_PATHS[kind].count('/')==2 else (None,None,None)
                path=f'/apis/{api}/{version}/namespaces/{ns}/{resource}/{oname}'
                owner,eid=self.get(client,'Владелец '+kind,path,f'kubectl -n {ns} get {resource} {oname} -o json')
                if not owner or owner.get('metadata',{}).get('uid')!=ref.get('uid'):
                    self.report['findings'].append({'source':eid,'fact':'Владелец недоступен или его UID изменился.'});break
                owners.append({'kind':kind,'name':oname,'uid':ref['uid']});owner_uids.add(ref['uid'])
                selector=owner.get('spec',{}).get('selector',{}).get('matchLabels',{}) or selector
                obj=owner
            self.report['owners']=owners
            label_selector=','.join(k+'='+v for k,v in sorted(selector.items()) if isinstance(v,str))
            params={'limit':250}
            if label_selector:params['labelSelector']=label_selector
            # Include old/new ReplicaSets of the same Deployment when examining replacements.
            dep=next((o for o in owners if o['kind']=='Deployment'),None)
            if dep:
                sets,_=self.get(client,'ReplicaSets этого Deployment',f'/apis/apps/v1/namespaces/{ns}/replicasets',f'kubectl -n {ns} get replicasets -o json',params)
                for rs in (sets or {}).get('items',[]):
                    if any(r.get('uid')==dep['uid'] for r in rs.get('metadata',{}).get('ownerReferences',[])):
                        owner_uids.add(rs.get('metadata',{}).get('uid'))
            rows=[];partial=False
            for _ in range(4):
                if not owner_uids:break
                siblings,_=self.get(client,'Текущие Pod владельца',prefix+'/pods',f'kubectl -n {ns} get pods -o json',params,project=lambda result:{'items':[pod_facts(p) for p in result.get('items',[]) if any(r.get('uid') in owner_uids for r in p.get('metadata',{}).get('ownerReferences',[]))],'continue':bool(result.get('metadata',{}).get('continue'))})
                if not siblings:break
                rows.extend(p for p in siblings.get('items',[]) if any(r.get('uid') in owner_uids for r in p.get('metadata',{}).get('ownerReferences',[])))
                cursor=siblings.get('metadata',{}).get('continue');partial=bool(cursor)
                if not cursor:break
                params={**params,'continue':cursor}
            self.report['replacements']={'items':[{'name':p.get('metadata',{}).get('name'),'uid':p.get('metadata',{}).get('uid'),'phase':p.get('status',{}).get('phase'),'conditions':p.get('status',{}).get('conditions',[])} for p in rows[:50]],'truncated':partial or len(rows)>50}
            for vol in spec.get('volumes',[])[:8]:
                claim=vol.get('persistentVolumeClaim',{}).get('claimName')
                if claim:self.get(client,'PVC Pod',prefix+'/persistentvolumeclaims/'+c._name(claim,'PVC'),f'kubectl -n {ns} get pvc {claim} -o json')
            labels=meta.get('labels') or {}
            services,_=self.get(client,'Services и selectors Pod',prefix+'/services',f'kubectl -n {ns} get services -o json',{'limit':100},project=lambda data:{'items':[svc for svc in data.get('items',[]) if svc.get('spec',{}).get('selector') and all(labels.get(k)==v for k,v in svc['spec']['selector'].items())],'truncated':bool(data.get('metadata',{}).get('continue'))})
            for svc in [s for s in (services or {}).get('items',[]) if s.get('spec',{}).get('selector') and all(labels.get(k)==v for k,v in s['spec']['selector'].items())][:5]:
                service=c._name(svc.get('metadata',{}).get('name'),'service')
                self.get(client,'Endpoints сервиса '+service,prefix+'/endpoints/'+service,f'kubectl -n {ns} get endpoints {service} -o json')
            self.get(client,'NetworkPolicies namespace',f'/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies',f'kubectl -n {ns} get networkpolicies -o json',{'limit':100},project=lambda data:{'items':data.get('items',[]),'truncated':bool(data.get('metadata',{}).get('continue'))})
        containers=status.get('initContainerStatuses',[])+status.get('containerStatuses',[])
        known={container.get('name') for container in containers}
        containers += [{'name':container['name']} for container in spec.get('initContainers',[])+spec.get('containers',[]) if container.get('name') and container['name'] not in known]
        selected=sorted(containers,key=lambda s:not bool(s.get('state',{}).get('waiting') or s.get('lastState',{}).get('terminated')))[:4]
        for container in selected:
            cn=container.get('name')
            if not cn or not uid:continue
            for previous in ([False,True] if container.get('restartCount',0) else [False]):
                command=f'kubectl -n {ns} logs {name} -c {cn} --tail=500 --timestamps'+(' --previous' if previous else '')
                self.run('Логи контейнера '+cn+(' (previous)' if previous else ''),command,'GET',prefix+f'/pods/{name}/log',
                         lambda cn=cn,previous=previous:c.resource_logs(self.config['kube'],ns,name,cn,previous,expected_uid=uid),project=lambda value:value[-5000:])
        finished=[s.get('state',{}).get('terminated',{}).get('finishedAt') or s.get('lastState',{}).get('terminated',{}).get('finishedAt') for s in containers]
        candidate=max((v for v in finished if v),default=None)
        basis='последние 6 часов; время аварии неизвестно'
        now=datetime.now(timezone.utc);end=now;start=now-timedelta(hours=6)
        if candidate:
            try:
                center=datetime.fromisoformat(candidate.replace('Z','+00:00'))
                if center.tzinfo and center<=now:
                    start=center-timedelta(minutes=30);end=min(now,center+timedelta(minutes=30))
                    basis='container finishedAt ±30m; не точное время выселения'
            except ValueError:pass
        self.report['history_window']={'from':start.isoformat(),'to':end.isoformat(),'basis':basis}
        astra=self.config.get('astra',{})
        if astra.get('url'):
            self.run('Astra: логи выбранного Pod','Astra logs search (cluster/namespace/pod)','POST','/api/v1/logs/search/monitorLogs',lambda:c.astra_logs(astra,ns,name,start.isoformat(),end.isoformat()),project=lambda d:{**d,'items':d.get('items',[])[:30],'identity_note':'Scope: cluster/namespace/pod + time. UID строк Astra не подтверждён: не приписывай логи переиспользованного имени старому объекту.'})
        else:self.run('Astra: логи выбранного Pod','','CONFIG','astra',lambda:(_ for _ in ()).throw(c.ConnectorError('Astra не настроена; её логи не проверены')))
        source=c.metrics_config(self.config)
        if c.metrics_configured(source):
            try:self.collect_metrics(source,ns,name,node,start,end,node_ips)
            except c.ConnectorError as exc:
                self.run('История метрик','','CONFIG','metrics',lambda:(_ for _ in ()).throw(exc))
        else:self.run('История метрик','','CONFIG','metrics',lambda:(_ for _ in ()).throw(c.ConnectorError('Источник метрик не настроен; история не проверена')))
        self.finish();return self.report

    def collect_metrics(self, source, ns, pod, node, start, end, node_ips):
        c.utilization_expressions(source)  # Validate cluster scope before adding object selectors.
        selectors=[k+'='+json.dumps(v) for k,v in (source.get('labels') or {}).items()]
        def scoped(*extras):return '{'+','.join(selectors+list(extras))+'}'
        queries={'pod_memory':'max by(container)(container_memory_working_set_bytes'+scoped('namespace='+json.dumps(ns),'pod='+json.dumps(pod),'container!=""','container!="POD"')+')',
                 'pod_cpu':'sum by(container)(rate(container_cpu_usage_seconds_total'+scoped('namespace='+json.dumps(ns),'pod='+json.dumps(pod),'container!=""','container!="POD"')+'[5m]))'}
        if node:queries['node_pressure']='kube_node_status_condition'+scoped('node='+json.dumps(node),'condition=~"MemoryPressure|DiskPressure|PIDPressure"','status="true"')
        if node_ips:
            pattern='('+'|'.join(re.escape(ip) for ip in node_ips)+')(:[0-9]+)?'
            selector='instance=~'+json.dumps(pattern)
            queries['target_node_memory_available']='node_memory_MemAvailable_bytes'+scoped(selector)
            queries['target_node_filesystem_available']='node_filesystem_avail_bytes'+scoped(selector,'fstype!~"tmpfs|overlay|squashfs|ramfs"')
        # Cluster aggregates complement targeted series; never claim that aggregate == this node.
        queries.update({'cluster_'+key:expr for key,expr in c.utilization_expressions(source).items()})
        with c.metric_client(source) as (client,base):
            for key,expr in queries.items():
                params={'query':expr,'start':start.timestamp(),'end':end.timestamp(),'step':max(30,int((end-start).total_seconds()/60))}
                def read(params=params):
                    data=c._request(client,'GET',base+'/api/v1/query_range',params=params,deadline=self.deadline)
                    if data.get('status')!='success':raise c.ConnectorError('Ошибка API метрик')
                    return data
                self.run('История '+key,'PromQL: '+expr,'GET','/api/v1/query_range',read,query=params)

    def finish(self):
        self.report['finished_at']=utc()
        self.report['unavailable']=sum(r['status']!='ok' for r in self.report['records'])
        self.save()


def collect_pod(config, namespace, name, uid='', cached=None, progress=None):
    return Collector(config,progress).collect(namespace,name,uid,cached)


def verified_summary(report):
    lines=['## Проверенные данные Pulse']
    for finding in report.get('findings',[]):lines.append('- ['+finding['source']+'] '+finding['fact'])
    lines.append(f"Проверок: {len(report.get('records',[]))}; недоступны: {report.get('unavailable',0)}. Полный журнал — в доказательствах расследования.")
    return '\n'.join(lines)


def model_evidence(report):
    """Keep every check/status and the target facts within a model context budget."""
    output={k:v for k,v in report.items() if k!='records'}
    count=max(1,len(report.get('records',[])))
    output['records']=[{**{k:v for k,v in row.items() if k!='data'},
                        **({'data':small(row['data'],min(5000,45000//count))} if 'data' in row else {})}
                       for row in report.get('records',[])]
    return output
