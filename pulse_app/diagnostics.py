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

# Separate collection plans keep a button's investigation relevant and bounded.
# Missing integrations are gaps only when that plan actually needs them.
COLLECTION_PLANS = {
    'deep': dict(seconds=90, checks=55, events=True, node=True, node_events=True,
                 logs=4, astra=True, metrics='all', volumes=True, policies=True, pages=4),
    'brief': dict(seconds=35, checks=24, events=True, node=True, node_events=False,
                  logs=1, astra=False, metrics=None, volumes=False, policies=False, pages=2),
    'causes': dict(seconds=75, checks=44, events=True, node=True, node_events=True,
                   logs=3, astra=True, metrics='targeted', volumes=True, policies='network', pages=3),
    'impact': dict(seconds=35, checks=24, events=False, node=False, node_events=False,
                   logs=0, astra=False, metrics=None, volumes=False, policies=False, pages=3),
    'plan': dict(seconds=70, checks=42, events=True, node=True, node_events=True,
                 logs=2, astra=True, metrics='relevant', volumes=True, policies='network', pages=3),
    'recovery': dict(seconds=45, checks=30, events=False, node=False, node_events=False,
                     logs=0, astra=False, metrics=None, volumes=False, policies=False, pages=4),
}


def record_group(path, label=''):
    if 'Astra' in label or path == 'astra': return 'astra'
    if path == 'metrics' or path.endswith('/query_range'): return 'metrics'
    if path.endswith('/log'): return 'logs'
    if '/events' in path: return 'events'
    if '/nodes/' in path: return 'node'
    if '/persistentvolumeclaims/' in path: return 'volume'
    if '/endpoints/' in path: return 'endpoints'
    if path.endswith('/services'): return 'service'
    if path.endswith('/networkpolicies'): return 'policies'
    if path.endswith('/pods'): return 'replacements'
    if '/pods/' in path or path == 'snapshot/pods': return 'pod'
    if any('/'+kind.split('/')[-1] in path for kind in OWNER_PATHS.values()): return 'owner'
    return 'pod'


def controller_refs(obj):
    return [ref for ref in obj.get('metadata', {}).get('ownerReferences', [])
            if isinstance(ref, dict) and ref.get('uid')]


def replacement_facts(pod):
    meta, status = pod.get('metadata', {}), pod.get('status', {})
    states = status.get('containerStatuses') or []
    conditions = status.get('conditions') or []
    ready_condition = any(item.get('type') == 'Ready' and item.get('status') == 'True'
                          for item in conditions if isinstance(item, dict))
    # Ready alone is insufficient when a container is missing or terminating.
    expected = {item.get('name') for item in pod.get('spec', {}).get('containers', [])}
    observed = {item.get('name') for item in states if isinstance(item, dict)}
    state_complete = bool(states) and None not in observed and len(observed)==len(states) and (not expected or expected == observed)
    containers_ready = state_complete and all(item.get('ready') is True for item in states)
    init_states = status.get('initContainerStatuses') or []
    expected_init = {item.get('name') for item in pod.get('spec', {}).get('initContainers', [])}
    init_complete = not expected_init or expected_init == {item.get('name') for item in init_states}
    init_ready = init_complete and all(item.get('state', {}).get('terminated', {}).get('exitCode') == 0 or
                     (item.get('ready') is True and item.get('state', {}).get('running'))
                     for item in init_states)
    return {'name': meta.get('name'), 'uid': meta.get('uid'), 'namespace': meta.get('namespace'),
            'phase': status.get('phase'), 'conditions': c.redact_structure(conditions),
            'ready': bool(status.get('phase') == 'Running' and ready_condition and containers_ready
                          and init_ready and not meta.get('deletionTimestamp')),
            'readiness_complete': bool(state_complete and init_complete), 'deleting': bool(meta.get('deletionTimestamp')),
            'owner_uids': [ref['uid'] for ref in controller_refs(pod)], 'source_type': 'live',
            'ip': status.get('podIP'), 'container_statuses': small(states, 3000)}


def utc():
    return datetime.now(timezone.utc).isoformat()


def small(value, limit=9000):
    value=c.redact_structure(c._sanitize(value))
    text=json.dumps(value,ensure_ascii=False)
    if len(text)<=limit:return value
    # Escaping the excerpt adds bytes/characters; include the wrapper in the budget.
    excerpt={'excerpt':'','truncated':True}
    low,high=0,len(text)
    while low<high:
        middle=(low+high+1)//2;excerpt['excerpt']=text[:middle]
        if len(json.dumps(excerpt,ensure_ascii=False))<=limit:low=middle
        else:high=middle-1
    excerpt['excerpt']=text[:low]
    return excerpt


def is_partial(value):
    return isinstance(value,dict) and bool(value.get('truncated') or value.get('continue') or
        (value.get('metadata') or {}).get('continue'))


def pod_facts(pod):
    spec=pod.get('spec',{})
    status=pod.get('status',{})
    for source,keys in ((spec,('containers','initContainers')),
                        (status,('containerStatuses','initContainerStatuses','conditions'))):
        for key in keys:
            if not isinstance(source.get(key,[]),list) or any(not isinstance(item,dict) for item in source.get(key,[])):
                raise c.ConnectorError('Pod содержит неподдерживаемые '+key)
    for state in status.get('containerStatuses',[])+status.get('initContainerStatuses',[]):
        if any(key in state and not isinstance(state[key],dict) for key in ('state','lastState')):
            raise c.ConnectorError('Pod содержит неподдерживаемое состояние контейнера')
    return {'metadata':pod.get('metadata',{}),'status':pod.get('status',{}),
            'spec':{k:spec[k] for k in ('nodeName','restartPolicy','priorityClassName','schedulerName','volumes','tolerations','affinity','overhead') if k in spec},
            'containers':[{k:v for k,v in container.items() if k in ('name','image','resources','readinessProbe','livenessProbe','startupProbe','restartPolicy')} for container in spec.get('containers',[])+spec.get('initContainers',[])]}


class Collector:
    def __init__(self, config, progress=None, scenario='deep'):
        if scenario not in COLLECTION_PLANS:
            raise c.ConnectorError('Неизвестный сценарий диагностики')
        self.config=config; self.progress=progress
        self.scenario=scenario; self.plan=COLLECTION_PLANS[scenario]
        self.report={'started_at':utc(),'scenario':scenario,'records':[],'findings':[],
                     'collection_plan':{'seconds':self.plan['seconds'],'max_checks':self.plan['checks']},
                     'identity_changed':False,
                     'note':'Выполнены HTTP API-запросы. Команды kubectl — эквиваленты для воспроизведения, shell не запускался.'}
        self.deadline=time.monotonic()+self.plan['seconds']

    def save(self):
        if self.progress:self.progress(self.report)

    def run(self, label, command, method, path, call, project=lambda x:x, query=None, group=None):
        group=group or record_group(path,label)
        row={'id':f'E{len(self.report["records"])+1:02d}','check':label,'equivalent_command':command,
             'method':method,'path':path,'group':group,'role':group,
             'query':query or {},'started_at':utc(),'attempted':False}
        try:
            if time.monotonic()>=self.deadline or len(self.report['records'])>=self.plan['checks']:
                raise c.ConnectorError('Бюджет сбора исчерпан; проверка не выполнена')
            row['attempted']=True
            result=call();data=small(project(result))
            partial=is_partial(result) or is_partial(data)
            row.update(status='partial' if partial else 'ok',complete=not partial,data=data)
            if partial:row['notice']='Данные ограничены страницей или обрезаны; отсутствие других фактов не доказано'
        except Exception as exc:
            result=None;row.update(status='unavailable',complete=False,error=str(exc) if isinstance(exc,c.ConnectorError) else 'Источник не вернул поддерживаемые данные')
        row['finished_at']=utc();self.report['records'].append(row);self.save()
        return result,row['id']

    def get(self, client, label, path, command, params=None, project=lambda x:x, group=None):
        def read():
            data=self.bounded_request(client,'GET',c._url(self.config['kube']['server'],https_only=True)+path,
                                      params=params or {})
            if not isinstance(data,dict) or any(key in data and not isinstance(data[key],dict)
                                                for key in ('metadata','spec','status')):
                raise c.ConnectorError('API не вернул поддерживаемый Kubernetes object')
            refs=data.get('metadata',{}).get('ownerReferences',[])
            if not isinstance(refs,list) or any(not isinstance(ref,dict) for ref in refs):
                raise c.ConnectorError('API не вернул поддерживаемые ownerReferences')
            return data
        return self.run(label,command,'GET',path,read,project,query=params,group=group)

    def bounded_request(self,client,method,url,**kwargs):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise c.ConnectorError('Бюджет сбора исчерпан; проверка не выполнена')
        return c._request(client,method,url,deadline=self.deadline,timeout=min(6,remaining),**kwargs)

    def complete_record(self, evidence_id):
        return any(row['id']==evidence_id and row.get('complete') is True for row in self.report['records'])

    def invalidate(self, evidence_id, message):
        for row in self.report['records']:
            if row['id']==evidence_id:
                row.update(status='unavailable',complete=False,error=message,data={'excluded':True})
                break

    def collect(self, namespace, name, uid='', cached=None):
        ns=c._name(namespace,'namespace');name=c._name(name,'pod')
        prefix=f'/api/v1/namespaces/{ns}'
        target={'namespace':ns,'name':name,'expected_uid':uid}
        self.report['target']=target
        node_ips=[]
        with c._client(self.config['kube'],timeout=6) as client:
            pod,pid=self.get(client,'Выбранный Pod: status.message, exitCode, requests/limits',prefix+'/pods/'+name,
                              f'kubectl -n {ns} get pod {name} -o json',project=pod_facts,group='pod')
            pod_source='live'
            if pod and (pod.get('metadata',{}).get('name')!=name or not pod.get('metadata',{}).get('uid') or
                        pod.get('metadata',{}).get('namespace',ns)!=ns):
                self.invalidate(pid,'API не вернул выбранный объект Pod с namespace/name/UID');pod=None
            if pod and uid and pod.get('metadata',{}).get('uid')!=uid:
                self.report['records'][-1].update(status='identity_changed',complete=False,
                    data={'name':name,'observed_uid':pod.get('metadata',{}).get('uid'),'expected_uid':uid,'excluded':True})
                self.report['identity_changed']=True
                self.report['findings'].append({'source':pid,'fact':'Имя занято другим UID. Новый Pod не используется как доказательство прежней проблемы.'})
                pod=None
            cache_meta=(cached or {}).get('metadata',{})
            if not pod and cached and cache_meta.get('name')==name and cache_meta.get('namespace',ns)==ns and cache_meta.get('uid') and (not uid or cache_meta['uid']==uid):
                pod,pid=self.run('Последний сохранённый Pod (не live)', '', 'CACHE', 'snapshot/pods',lambda:cached,project=pod_facts,group='pod')
                pod_source='cached'
            if not pod:
                pod_source='unavailable'
                self.report['findings'].append({'source':pid,'fact':'Исходный Pod недоступен. Причина по его status не установлена.'})
                pod={'metadata':{'name':name,'namespace':ns,'uid':uid},'status':{},'spec':{}}
            meta,status,spec=pod.get('metadata',{}),pod.get('status',{}),pod.get('spec',{})
            uid=meta.get('uid') or uid;target['uid']=uid
            self.report['pod']={**replacement_facts(pod),'uid':uid,'node':spec.get('nodeName'),
                'phase':status.get('phase'),'reason':status.get('reason'),
                'message':c.redact_sensitive_text(status['message']) if isinstance(status.get('message'),str) else None,
                'created_at':meta.get('creationTimestamp'),'source_type':pod_source,'evidence_id':pid,
                'observed_at':self.report['started_at'] if pod_source=='live' else None}
            self.report['omitted_groups']=[group for group,enabled in (
                ('logs',self.plan['logs']),('astra',self.plan['astra']),('metrics',self.plan['metrics']),
                ('volume',self.plan['volumes']),('policies',self.plan['policies']),
                ('node',self.plan['node']),('events',self.plan['events'])) if not enabled]
            self.report['findings'].append({'source':pid,'fact':c.redact_sensitive_text(f"Pod phase={status.get('phase','Unknown')}; reason={status.get('reason','не указан')}; status.message={status.get('message') or 'не заполнено'}.")})
            self.report['findings'].append({'source':pid,'fact':'Время наблюдения не является временем аварии. Время создания Pod также не является временем выселения.'})
            if uid and self.plan['events']:
                self.get(client,'События выбранного UID',prefix+'/events',
                    f'kubectl -n {ns} get events --field-selector involvedObject.uid={uid} -o json',
                    {'fieldSelector':'involvedObject.uid='+uid,'limit':100 if self.scenario=='brief' else 200},
                    project=lambda data:{'items':[item for item in self.list_items(data)
                        if item.get('involvedObject',{}).get('uid',uid)==uid],
                        'truncated':is_partial(data)},group='events')
            node=spec.get('nodeName')
            if node and self.plan['node']:
                node_obj,nid=self.get(client,'Фактический узел Pod','/api/v1/nodes/'+c._name(node,'node'),
                                      f'kubectl get node {node} -o json',group='node')
                for address in (node_obj or {}).get('status',{}).get('addresses',[]):
                    if address.get('type')=='InternalIP':
                        try:node_ips.append(str(ipaddress.ip_address(address['address'])))
                        except (ValueError,KeyError):pass
                self.report['node']={'name':node,'conditions':(node_obj or {}).get('status',{}).get('conditions',[]),
                    'complete':self.complete_record(nid),'source_type':'live','evidence_id':nid}
                self.report['findings'].append({'source':pid,'fact':'Фактический узел из spec.nodeName: '+node+'. Текущие Ready/Pressure не исключают прошлого давления.'})
                if self.plan['node_events']:
                    self.get(client,'События фактического узла','/api/v1/events',
                        f'kubectl get events -A --field-selector involvedObject.kind=Node,involvedObject.name={node} -o json',
                        {'fieldSelector':'involvedObject.kind=Node,involvedObject.name='+node,'limit':200},
                        project=lambda data:{'items':self.list_items(data),'truncated':is_partial(data)},group='events')
            owners,owner_uids,selector=self.collect_owners(client,ns,pod)
            self.collect_replacements(client,ns,owners,owner_uids,selector)
            if self.plan['volumes']:
                claims=[v.get('persistentVolumeClaim',{}).get('claimName') for v in spec.get('volumes',[])]
                self.report['volumes_complete']=len([value for value in claims if value])<=8
                for claim in [value for value in claims if value][:8]:
                    self.get(client,'PVC Pod',prefix+'/persistentvolumeclaims/'+c._name(claim,'PVC'),
                             f'kubectl -n {ns} get pvc {claim} -o json',group='volume')
            self.collect_services(client,ns,meta.get('labels') or {})
            network_issue=bool(re.search(r'network|connection|timeout|dns|probe|unhealthy',
                json.dumps(status,ensure_ascii=False),re.I))
            if self.plan['policies'] is True or (self.plan['policies']=='network' and network_issue):
                self.get(client,'NetworkPolicies namespace',f'/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies',
                    f'kubectl -n {ns} get networkpolicies -o json',{'limit':100},
                    project=lambda data:{'items':self.list_items(data),'truncated':is_partial(data)},group='policies')
        self.collect_logs(ns,name,uid,pod)
        start,end=self.history_window(status)
        if self.plan['astra']:
            astra=self.config.get('astra',{})
            if astra.get('url'):
                self.run('Astra: логи выбранного Pod','Astra logs search (cluster/namespace/pod)','POST',
                    '/api/v1/logs/search/monitorLogs',
                    lambda:c.astra_logs(astra,ns,name,start.isoformat(),end.isoformat(),deadline=self.deadline),
                    project=lambda data:{**data,'items':self.list_items(data)[:30],
                        'truncated':is_partial(data) or len(self.list_items(data))>30,
                        'identity_note':'Scope: cluster/namespace/pod + time. UID строк Astra не подтверждён: не приписывай логи переиспользованного имени старому объекту.'},group='astra')
            else:
                self.run('Astra: логи выбранного Pod','','CONFIG','astra',
                    lambda:(_ for _ in ()).throw(c.ConnectorError('Astra не настроена; её логи не проверены')),group='astra')
        metric_mode=self.plan['metrics']
        resource_issue=bool(re.search(r'evict|oom|memory|storage|disk|cpu|pressure',
                                      json.dumps(status,ensure_ascii=False),re.I))
        if metric_mode and (metric_mode!='relevant' or resource_issue):
            source=c.metrics_config(self.config)
            if c.metrics_configured(source):
                try:self.collect_metrics(source,ns,name,node,start,end,node_ips)
                except c.ConnectorError as exc:
                    self.run('История метрик','','CONFIG','metrics',lambda:(_ for _ in ()).throw(exc),group='metrics')
            else:
                self.run('История метрик','','CONFIG','metrics',
                    lambda:(_ for _ in ()).throw(c.ConnectorError('Источник метрик не настроен; история не проверена')),group='metrics')
        self.finish();return self.report

    @staticmethod
    def list_items(data):
        if not isinstance(data,dict) or not isinstance(data.get('items'),list) or any(not isinstance(item,dict) for item in data['items']):
            raise c.ConnectorError('API не вернул поддерживаемый список items; полнота не подтверждена')
        return data['items']

    def collect_owners(self,client,ns,pod):
        owners=[];obj=pod;owner_uids=set();selector={};complete=True
        for _ in range(4):
            ref=next((r for r in controller_refs(obj) if r.get('controller')),None)
            if not ref:break
            if ref.get('kind') not in OWNER_PATHS:
                complete=False;break
            kind,oname=ref['kind'],c._name(ref.get('name'),'owner')
            api,version,resource=OWNER_PATHS[kind].split('/')
            owner,eid=self.get(client,'Владелец '+kind,
                f'/apis/{api}/{version}/namespaces/{ns}/{resource}/{oname}',
                f'kubectl -n {ns} get {resource} {oname} -o json',group='owner')
            owner_meta=(owner or {}).get('metadata',{})
            if not owner or owner_meta.get('uid')!=ref['uid'] or owner_meta.get('name',oname)!=oname or owner_meta.get('namespace',ns)!=ns:
                complete=False
                if owner:self.invalidate(eid,'UID/namespace/name владельца не совпал; объект исключён')
                self.report['findings'].append({'source':eid,'fact':'Владелец недоступен или его UID изменился.'});break
            owner_spec=owner.get('spec',{});owner_status=owner.get('status',{})
            safe_spec={key:owner_spec[key] for key in ('replicas','selector','minReadySeconds','progressDeadlineSeconds',
                'updateStrategy','strategy','serviceName','podManagementPolicy','completions','parallelism','suspend') if key in owner_spec}
            owners.append({'kind':kind,'name':oname,'uid':ref['uid'],'spec':small(safe_spec,3000),
                'status':small(owner_status,4000),'generation':owner_meta.get('generation'),
                'observedGeneration':owner_status.get('observedGeneration'),'source_type':'live',
                'evidence_id':eid,'complete':self.complete_record(eid)})
            complete=complete and self.complete_record(eid)
            owner_uids.add(ref['uid'])
            selector=owner_spec.get('selector',{}).get('matchLabels',{}) or selector
            obj=owner
        else:
            if any(ref.get('controller') for ref in controller_refs(obj)):complete=False
        self.report['owners']=owners
        self.report['owners_complete']=bool(complete and owners)
        return owners,owner_uids,selector

    def collect_replacements(self,client,ns,owners,owner_uids,selector):
        def owned_by(obj,uids):
            # UID-only owner refs remain accepted by the historical deep
            # collector. Guided actions require the actual controller flag;
            # a non-controller reference is not a managed replacement.
            return any(ref['uid'] in uids and (ref.get('controller') is True or
                (self.scenario=='deep' and 'controller' not in ref)) for ref in controller_refs(obj))
        params={'limit':250}
        label_selector=','.join(k+'='+v for k,v in sorted(selector.items()) if isinstance(v,str))
        if label_selector:params['labelSelector']=label_selector
        scope_complete=bool(owner_uids) and self.report['owners_complete']
        evidence_ids=[];dep=next((o for o in owners if o['kind']=='Deployment'),None)
        if dep:
            sets,eid=self.get(client,'ReplicaSets этого Deployment',f'/apis/apps/v1/namespaces/{ns}/replicasets',
                f'kubectl -n {ns} get replicasets -o json',params,
                project=lambda data:{'items':self.list_items(data),'truncated':is_partial(data)},group='owner')
            evidence_ids.append(eid)
            scope_complete=scope_complete and bool(sets is not None) and self.complete_record(eid)
            for rs in (sets or {}).get('items',[]):
                meta=rs.get('metadata',{})
                if meta.get('namespace',ns)!=ns:continue
                if owned_by(rs,{dep['uid']}):
                    if meta.get('uid'):owner_uids.add(meta['uid'])
                    else:scope_complete=False
        rows=[];scan_complete=False;cursor=None
        for _ in range(self.plan['pages']):
            if not owner_uids:break
            siblings,eid=self.get(client,'Текущие Pod владельца',f'/api/v1/namespaces/{ns}/pods',
                f'kubectl -n {ns} get pods -o json',params,
                project=lambda data:{'items':[pod_facts(p) for p in self.list_items(data)
                    if p.get('metadata',{}).get('namespace',ns)==ns and
                    owned_by(p,owner_uids)],
                    'truncated':is_partial(data)},group='replacements')
            evidence_ids.append(eid)
            if siblings is None:break
            scope_complete=scope_complete and self.complete_record(eid)
            # UID membership is authoritative; a shared label is never ownership.
            for p in siblings.get('items',[]):
                meta=p.get('metadata',{})
                if not owned_by(p,owner_uids):continue
                if meta.get('namespace',ns)!=ns or not meta.get('uid') or not meta.get('name'):
                    scope_complete=False;continue
                rows.append(p)
            cursor=siblings.get('metadata',{}).get('continue')
            if not cursor:
                scan_complete=not bool(siblings.get('truncated'));break
            params={**params,'continue':cursor}
        unique={p['metadata']['uid']:p for p in rows}
        items=[replacement_facts(p) for p in list(unique.values())[:50]]
        truncated=bool(cursor or len(unique)>50)
        self.report['replacements']={'items':items,'complete':bool(scope_complete and scan_complete and not truncated),
            'truncated':truncated or not scan_complete,'evidence_ids':evidence_ids,
            'owner_uids':sorted(uid for uid in owner_uids if uid),'source_type':'live',
            'observed_at':utc()}

    def collect_services(self,client,ns,labels):
        prefix=f'/api/v1/namespaces/{ns}'
        def matching(data):
            return [svc for svc in self.list_items(data) if svc.get('metadata',{}).get('namespace',ns)==ns
                and svc.get('spec',{}).get('selector') and
                all(labels.get(key)==value for key,value in svc['spec']['selector'].items())]
        services,eid=self.get(client,'Services и selectors Pod',prefix+'/services',
            f'kubectl -n {ns} get services -o json',{'limit':100},
            project=lambda data:{'items':matching(data),'truncated':is_partial(data)},group='service')
        selected=matching(services) if services is not None else []
        service_items=[];endpoint_items=[]
        for svc in selected[:5]:
            meta,spec=svc.get('metadata',{}),svc.get('spec',{})
            service=meta.get('name')
            if not service:continue
            service=c._name(service,'service')
            service_items.append({'name':service,'uid':meta.get('uid'),'selector':spec.get('selector'),
                'ports':spec.get('ports',[]),'publish_not_ready':spec.get('publishNotReadyAddresses',False),
                'evidence_id':eid,'source_type':'live'})
            endpoint,endpoint_id=self.get(client,'Endpoints сервиса '+service,prefix+'/endpoints/'+service,
                f'kubectl -n {ns} get endpoints {service} -o json',
                project=lambda data:self.endpoint_facts(data,ns,service),group='endpoints')
            valid=isinstance(endpoint,dict) and isinstance(endpoint.get('subsets'),list)
            addresses=[]
            for subset in (endpoint or {}).get('subsets',[]):
                for key,ready in (('addresses',True),('notReadyAddresses',False)):
                    for address in subset.get(key,[]):
                        ref=address.get('targetRef') or {}
                        same_namespace=ref.get('namespace',ns)==ns
                        addresses.append({'ip':address.get('ip'),'target_uid':ref.get('uid') if same_namespace else None,
                            'target_name':ref.get('name') if same_namespace else None,'ready':ready,
                            'target_kind':ref.get('kind'),'namespace':ref.get('namespace',ns)})
            endpoint_items.append({'service':service,'addresses':addresses[:100],
                'complete':bool(valid and self.complete_record(endpoint_id) and len(addresses)<=100),
                'evidence_id':endpoint_id,'source_type':'live','observed_at':utc()})
        self.report['services']={'items':service_items,'complete':bool(services is not None and
            self.complete_record(eid) and len(selected)<=5 and len(service_items)==len(selected)),
            'evidence_id':eid,'source_type':'live','observed_at':utc()}
        self.report['endpoints']=endpoint_items

    @staticmethod
    def endpoint_facts(data,namespace,service):
        if not isinstance(data,dict) or not isinstance(data.get('subsets'),list):
            raise c.ConnectorError('Endpoints не вернул subsets; доступность не подтверждена')
        meta=data.get('metadata',{})
        if meta.get('namespace',namespace)!=namespace or meta.get('name',service)!=service:
            raise c.ConnectorError('Endpoints принадлежит другому service/namespace')
        for subset in data['subsets']:
            if not isinstance(subset,dict):raise c.ConnectorError('Некорректный subsets Endpoints')
            for key in ('addresses','notReadyAddresses'):
                if not isinstance(subset.get(key,[]),list) or any(not isinstance(row,dict) for row in subset.get(key,[])):
                    raise c.ConnectorError('Некорректные адреса Endpoints')
                if any(row.get('targetRef') is not None and not isinstance(row['targetRef'],dict) for row in subset.get(key,[])):
                    raise c.ConnectorError('Некорректный targetRef Endpoints')
        return data

    def collect_logs(self,ns,name,uid,pod):
        if not self.plan['logs']:return
        status,spec=pod.get('status',{}),pod.get('spec',{})
        containers=status.get('initContainerStatuses',[])+status.get('containerStatuses',[])
        if self.scenario=='deep':
            known={container.get('name') for container in containers}
            containers += [{'name':container['name']} for container in spec.get('initContainers',[])+spec.get('containers',[])
                           if container.get('name') and container['name'] not in known]
        else:
            # A direct EmptyDir eviction already proves the immediate cause. Broad logs
            # add no evidence to a brief result and are often gone after eviction.
            if self.scenario=='brief' and emptydir_cause(self.report):return
            containers=[container for container in containers if container.get('state',{}).get('waiting') or
                container.get('state',{}).get('terminated',{}).get('exitCode',0)!=0 or
                container.get('lastState',{}).get('terminated') or container.get('restartCount',0)>0 or
                (status.get('phase')=='Running' and container.get('ready') is False)]
            before_start={'ImagePullBackOff','ErrImagePull','InvalidImageName','CreateContainerConfigError',
                          'CreateContainerError','PodInitializing','ContainerCreating'}
            containers=[container for container in containers if
                container.get('state',{}).get('waiting',{}).get('reason') not in before_start or
                container.get('restartCount',0)>0 or container.get('lastState',{}).get('terminated')]
        selected=sorted(containers,key=lambda state:not bool(state.get('state',{}).get('waiting') or
            state.get('lastState',{}).get('terminated')))[:self.plan['logs']]
        for container in selected:
            cn=container.get('name')
            if not cn or not uid:continue
            for previous in ([False,True] if container.get('restartCount',0) else [False]):
                command=f'kubectl -n {ns} logs {name} -c {cn} --tail=500 --timestamps'+(' --previous' if previous else '')
                # resource_logs normally has a long interactive timeout. These checks use
                # the collector's deadline and validate UID before and after each read.
                self.run('Логи контейнера '+cn+(' (previous)' if previous else ''),command,'GET',
                    f'/api/v1/namespaces/{ns}/pods/{name}/log',
                    lambda cn=cn,previous=previous:self.read_logs(ns,name,uid,cn,previous),
                    project=lambda value:{'text':value[-5000:],'truncated':len(value)>5000,
                        'scope':'Последние 500 строк; не полная история контейнера'},group='logs')

    def read_logs(self,ns,name,uid,container,previous):
        path=f'/api/v1/namespaces/{ns}/pods/{name}'
        base=c._url(self.config['kube']['server'],https_only=True)
        with c._client(self.config['kube'],timeout=6) as client:
            def check_identity():
                if time.monotonic()>=self.deadline:
                    raise c.ConnectorError('Бюджет сбора исчерпан; логи не подтверждены')
                pod=self.bounded_request(client,'GET',base+path)
                if not isinstance(pod,dict) or pod.get('metadata',{}).get('uid')!=uid:
                    raise c.ConnectorError('UID Pod изменился; логи другого объекта исключены')
            check_identity()
            value=self.bounded_request(client,'GET',base+path+'/log',as_json=False,
                params={'container':c._name(container,'container'),'tailLines':500,'limitBytes':262144,
                        'timestamps':'true','previous':'true' if previous else 'false'})
            check_identity()
            return c.redact_sensitive_text(value)

    def history_window(self,status):
        containers=status.get('initContainerStatuses',[])+status.get('containerStatuses',[])
        finished=[state.get('state',{}).get('terminated',{}).get('finishedAt') or
                  state.get('lastState',{}).get('terminated',{}).get('finishedAt') for state in containers]
        candidate=max((value for value in finished if value),default=None)
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
        return start,end

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
        # Aggregates belong to the full investigation; causes/plan focus on this
        # Pod and node and must not imply that a cluster average explains them.
        if self.plan['metrics']=='all':
            queries.update({'cluster_'+key:expr for key,expr in c.utilization_expressions(source).items()})
        with c.metric_client(source) as (client,base):
            for key,expr in queries.items():
                params={'query':expr,'start':start.timestamp(),'end':end.timestamp(),'step':max(30,int((end-start).total_seconds()/60))}
                def read(params=params):
                    data=self.bounded_request(client,'GET',base+'/api/v1/query_range',params=params)
                    for row in c._metric_series(data,'matrix'):
                        if not isinstance(row.get('values'),list):raise c.ConnectorError('Источник вернул некорректную историю метрик')
                        for value in row['values']:
                            c._metric_point(value,earliest=params['start']-params['step'],latest=params['end']+params['step'])
                    return data
                self.run('История '+key,'PromQL: '+expr,'GET','/api/v1/query_range',read,query=params)

    def finish(self):
        self.report['finished_at']=utc()
        self.report['unavailable']=sum(r['status'] not in ('ok','partial') for r in self.report['records'])
        self.report['partial']=sum(r['status']=='partial' for r in self.report['records'])
        gaps=[]
        if 'owners_complete' in self.report and not self.report['owners_complete']:gaps.append('owner')
        for key,group in (('replacements','replacements'),('services','service')):
            if key in self.report and not self.report[key].get('complete'):gaps.append(group)
        if any(not item.get('complete') for item in self.report.get('endpoints',[])):gaps.append('endpoints')
        if self.report.get('volumes_complete') is False:gaps.append('volume')
        self.report['scope_gaps']=gaps;self.report['scope_complete']=not gaps
        self.report['complete']=not (self.report['unavailable'] or self.report['partial'] or gaps)
        self.save()


def collect_pod(config, namespace, name, uid='', cached=None, progress=None, scenario='deep'):
    return Collector(config,progress,scenario).collect(namespace,name,uid,cached)


def verified_summary(report, mode=None):
    lines=['## Проверенные данные Pulse']
    cause=emptydir_cause(report)
    if cause:
        lines.append('Причина завершения по status.message: превышен лимит EmptyDir `'+cause['volume']+'` — `'+cause['limit']+'`. Это ограничение временного тома Pod; заполнение корневого диска узла этим не доказано.')
    for finding in report.get('findings',[]):lines.append('- ['+finding['source']+'] '+finding['fact'])
    lines.append(f"Проверок: {len(report.get('records',[]))}; недоступны: {report.get('unavailable',0)}; неполные данные: {report.get('partial',0)}. Журнал — в доказательствах расследования.")
    if mode=='impact':
        lines.append('## Последствия: оценка Pulse по собранным фактам')
        if report.get('pod',{}).get('phase')=='Failed':
            lines.append('Подтверждено: выбранный Pod имеет терминальную фазу Failed; он не является работающей репликой. Это относится к состоянию в указанном источнике, а не доказывает текущий простой всего сервиса.')
        if cause:
            lines.append('Возможные последствия: перерыв в работе приложения этой реплики; потеря временных файлов тома '+cause['volume']+' при удалении Pod. Факт потери данных и длительность перерыва не установлены.')
        lines.append('Для оценки текущего влияния нужны готовность Pod-замены, доступные endpoints и история метрик/логов. Успешное выполнение запроса само по себе не подтверждает отсутствие проблем; статус ok не означает непустой результат.')
        lines.append('Постоянная потеря данных, отказ всего кластера и причина роста файлов по этим сведениям не подтверждены. Недоступные логи не считаются проверенными.')
    return c.redact_sensitive_text('\n'.join(lines))


def emptydir_cause(report):
    pod=report.get('pod',{})
    if pod.get('reason')!='Evicted':return None
    match=re.search(r'Usage of EmptyDir volume ["\']([^"\']+)["\'] exceeds the limit ["\']([^"\']+)["\']',pod.get('message') or '',re.I)
    return {'volume':match[1],'limit':match[2]} if match else None


def review_answer(report,answer,documents=None):
    """Flag observable omissions; this is not a semantic correctness guarantee."""
    warnings=[]
    refs=set(re.findall(r'\bE\d{2}\b',answer))
    known={r['id'] for r in report.get('records',[])}
    if known and not refs:warnings.append('Модель не сослалась на журнал E##; сверьте её утверждения с проверенными данными.')
    if refs-known:warnings.append('В ответе есть ссылки на отсутствующие проверки: '+', '.join(sorted(refs-known)))
    if documents is not None:
        doc_refs=set(re.findall(r'\bD\d{2}\b',answer))
        known_docs={d['id'] for d in documents}
        if doc_refs-known_docs:warnings.append('В ответе есть ссылки на отсутствующие документы: '+', '.join(sorted(doc_refs-known_docs)))
    cause=emptydir_cause(report)
    if cause and not all(x.casefold() in answer.casefold() for x in ('emptydir',cause['volume'],cause['limit'])):
        warnings.append('Модель не раскрыла подтверждённое превышение лимита EmptyDir '+cause['volume']+' '+cause['limit']+'. Смотрите проверенные данные Pulse.')
    return warnings


def model_evidence(report, limit=65000):
    """Keep every check/status and the target facts within a model context budget."""
    if not isinstance(limit,int) or limit<2048:raise ValueError('Evidence context limit must be at least 2048 characters')
    output={k:small(v,5000) for k,v in report.items() if k!='records'}
    count=max(1,len(report.get('records',[])))
    output['records']=[{**{k:small(v,800) for k,v in row.items() if k!='data'},
                        **({'data':small(row['data'],min(5000,45000//count))} if 'data' in row else {})}
                       for row in report.get('records',[])]
    # Requests, query labels and top-level findings also consume context. Reduce
    # their largest fields first without removing any evidence ID/status.
    while len(json.dumps(output,ensure_ascii=False))>limit:
        output['context_truncated']=True
        candidates=[(len(json.dumps(v,ensure_ascii=False)),output,k) for k,v in output.items() if k!='records']
        candidates.extend((len(json.dumps(v,ensure_ascii=False)),row,k) for row in output['records']
                          for k,v in row.items() if k not in ('id','check','status'))
        size,parent,key=max(candidates,key=lambda item:item[0])
        if size<=100:raise c.ConnectorError('Журнал проверок не помещается в бюджет контекста')
        parent[key]=small(parent[key],max(100,size//2))
    return output
