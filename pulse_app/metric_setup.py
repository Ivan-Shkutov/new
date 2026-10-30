"""Probe a proposed source before saving it; distinguish configuration from data."""
import math
import time
from urllib.parse import urlsplit

from . import connectors as c
from .cluster_chat import live_list


def public_source(source):
    return {k:v for k,v in source.items() if k in ('url','transport','namespace','service','port','mode','labels')}


def failure_action(error, transport='direct'):
    if 'HTTP 401' in error:return 'Источник вернул 401: проверьте срок reader token Kubernetes.' if transport=='kubernetes' else 'Источник вернул 401: проверьте его авторизацию.'
    if 'HTTP 403' in error:return 'Нет права GET services/proxy для выбранного сервиса. Примените узкий Role/RoleBinding из инструкции.' if transport=='kubernetes' else 'Источник запретил запрос (403). Проверьте права его учётной записи.'
    if 'HTTP 404' in error:return 'Проверьте namespace, имя сервиса, порт и путь Prometheus API.'
    if 'Shared metrics require' in error:return 'Для общего источника задайте cluster labels; для отдельного Prometheus этого кластера выберите dedicated.'
    if 'network/TLS' in error:return 'Проверьте доступ из Pulse к Kubernetes API/Prometheus и доверенный CA.'
    return 'Проверьте node_exporter, scrape targets, labels и наличие метрик выбранного кластера.'


def candidates(services):
    items=[]
    for service in services:
        meta=service.get('metadata') or {};name=meta.get('name','')
        if not any(t in name for t in ('prometheus','victoria','vmauth','vmselect')) or any(t in name for t in ('operator','alertmanager','exporter','operated','coredns','kubelet','controller','scheduler','etcd','proxy')):continue
        for port in service.get('spec',{}).get('ports',[]):
            if port.get('port') in (9090,8428):items.append({'transport':'kubernetes','namespace':meta.get('namespace'),'service':name,'port':port['port']})
    return items


def probe(config, source=None):
    proposed=dict(source if source is not None else config.get('metrics',{}))
    if not c.metrics_configured(proposed):
        return {'ok':False,'status':'unconfigured','configured':False,'source':public_source(proposed),'metrics':{},'action':'Источник метрик не сохранён. Нажмите «Подключить метрики»; для PeopleHub используйте monitoring/kube-prom-kube-prometheus-prometheus:9090.'}
    settings=c.metrics_config({**config,'metrics':proposed})
    try:data=c.dashboard_metrics(settings)
    except c.ConnectorError as exc:
        return {'ok':False,'status':'error','configured':True,'source':public_source(proposed),'metrics':{},'error':str(exc),'action':failure_action(str(exc),proposed.get('transport'))}
    nodes=live_list(config['kube'],'nodes')
    aliases={};expected=set()
    for item in nodes['items']:
        meta=item.get('metadata',{});name=meta.get('name')
        if not name:continue
        expected.add(name);aliases[name.casefold()]=name;aliases[name.split('.')[0].casefold()]=name
        for a in item.get('status',{}).get('addresses',[]):
            if a.get('type') in ('InternalIP','Hostname') and a.get('address'):aliases[a['address'].casefold()]=name
    checks={};now=time.time()
    for key in ('cpu','ram','disk'):
        value=data.get('metrics',{}).get(key,{})
        seen=set();foreign=[]
        for row in value.get('nodes',[]):
            if not isinstance(row.get('percent'),(int,float)) or not math.isfinite(row['percent']) or not 0<=row['percent']<=100 or not isinstance(row.get('observed_at'),(int,float)) or not now-120<=row.get('observed_at',0)<=now+30:continue
            instance=str(row.get('instance','')).casefold()
            try:host=urlsplit('http://'+instance).hostname
            except ValueError:host=None
            match=aliases.get(instance) or aliases.get(host or '')
            if match:seen.add(match)
            else:foreign.append(instance)
        fresh=isinstance(value.get('percent'),(int,float)) and math.isfinite(value['percent']) and 0<=value['percent']<=100 and isinstance(value.get('observed_at'),(int,float)) and now-120<=value['observed_at']<=now+30
        complete=nodes['complete'] and bool(expected) and seen==expected and not foreign
        error=value.get('error') or value.get('nodes_error') or (None if fresh else 'Актуальное значение не получено')
        checks[key]={'ok':fresh and complete and not error,'percent':value.get('percent'),'observed_at':value.get('observed_at'),
                     'covered':len(seen),'expected':len(expected),'foreign':foreign[:10],'missing':sorted(expected-seen)[:20],'error':error,
                     'history_points':len(value.get('history',[])),'history_error':value.get('history_error')}
    ok=all(v['ok'] for v in checks.values())
    errors='; '.join(str(v['error']) for v in checks.values() if v['error'])
    action='CPU, RAM и корневая ФС получены для всех узлов.' if ok else failure_action(errors,proposed.get('transport'))
    if not nodes['complete']:action='Не удалось прочитать полный список узлов: '+str(nodes['error'])
    elif any(v['foreign'] for v in checks.values()):action='Источник содержит неизвестные узлы. Уточните cluster labels; данные другого кластера не сохраняются как dedicated.'
    elif not errors and not ok:action='Не все узлы представлены node_exporter-метриками. Проверьте missing/foreign в отчёте.'
    return {'ok':ok,'status':'ok' if ok else 'partial','configured':True,'source':public_source(proposed),'metrics':checks,'action':action,'node_error':nodes['error']}
