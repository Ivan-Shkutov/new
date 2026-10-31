from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest
from pulse_app.dashboard import summarize,combine
from pulse_app.health import assess,aggregate
from test_api import client,runtime,add,MUTATION
from pulse_app import connectors


def stamp(seconds=0):
    return (datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat()


def snapshot():
    kinds=('nodes','pods','daemonsets','deployments','jobs','cronjobs','replicasets','statefulsets','persistentvolumeclaims','events')
    s={'observed_at':stamp(),'complete':True,'errors':[],'resources':{k:{'items':[],'error':None,'truncated':False} for k in kinds}}
    s['resources']['nodes']['items']=[{'metadata':{'name':f'node-{i}'},'status':{'conditions':[{'type':'Ready','status':'True'},{'type':'MemoryPressure','status':'False'}]}} for i in range(6)]
    return s


def health(s):return summarize(s,'one','PeopleHub')['health']


def pod(phase='Running',ready='True',seconds=0,reason=None):
    p={'metadata':{'name':'web','namespace':'app','creationTimestamp':stamp(seconds)},'status':{'phase':phase,'conditions':[{'type':'Ready','status':ready,'lastTransitionTime':stamp(seconds)}],'containerStatuses':[{'restartCount':37}]}}
    if phase=='Succeeded':p['status']['containerStatuses'][0]['state']={'terminated':{'exitCode':0,'reason':'Completed'}}
    if reason:p['status']['containerStatuses'][0]['state']={'waiting':{'reason':reason}}
    return p


def test_six_of_six_is_healthy_and_five_of_six_is_critical():
    s=snapshot();h=health(s)
    assert h['status']=='healthy' and h['nodes']=={'ready':6,'total':6,'health':'healthy'}
    s['resources']['nodes']['items'][2]['status']['conditions'][0]['status']='False'
    h=health(s)
    assert h['status']=='critical' and h['nodes']=={'ready':5,'total':6,'health':'critical'}
    assert h['issues'][0]['name']=='node-2'


@pytest.mark.parametrize('condition',['DiskPressure','MemoryPressure','PIDPressure','NetworkUnavailable'])
def test_pressure_is_critical_even_when_six_nodes_ready(condition):
    s=snapshot();s['resources']['nodes']['items'][0]['status']['conditions']=[{'type':'Ready','status':'True'},{'type':condition,'status':'True'}]
    h=health(s)
    assert h['nodes']['ready']==6 and h['nodes']['health']=='critical'
    assert h['issues'][0]['title']==condition


@pytest.mark.parametrize('case',['stale','missing','forbidden','partial','no-ready','no-nodes'])
def test_missing_and_stale_data_never_become_green(case):
    s=snapshot()
    if case=='stale':s['observed_at']=stamp(3600)
    elif case=='missing':s.pop('observed_at')
    elif case=='forbidden':s['resources']['nodes']['error']='403'
    elif case=='partial':s['resources']['nodes']['truncated']=True
    elif case=='no-ready':s['resources']['nodes']['items'][0]['status']['conditions']=[]
    elif case=='no-nodes':s['resources']['nodes']['items']=[]
    h=health(s)
    assert h['status']!='healthy'
    assert h['nodes']['health']!='healthy'


def test_paused_does_not_claim_current_health():
    s=snapshot();workloads=summarize(s,'one','One')['workloads']
    h=assess(s,'one','One',workloads,enabled=False)
    assert h['status']=='paused' and h['nodes']['health']=='unknown'
    assert all(r['health']=='unknown' for r in workloads.values())


@pytest.mark.parametrize('reason',['CrashLoopBackOff','ImagePullBackOff','ErrImagePull','CreateContainerConfigError'])
def test_current_waiting_errors_are_critical(reason):
    s=snapshot();s['resources']['pods']['items']=[pod(ready='False',reason=reason)]
    h=health(s)
    assert h['status']=='critical' and h['issues'][0]['title']==reason
    assert h['pods']['ready']==0 and h['pods']['active']==1


@pytest.mark.parametrize('seconds,phase,expected',[(10,'Pending','warning'),(600,'Pending','critical'),(10,'Running','warning'),(200,'Running','critical')])
def test_startup_grace_periods(seconds,phase,expected):
    s=snapshot();s['resources']['pods']['items']=[pod(phase,ready='False',seconds=seconds)]
    assert health(s)['issues'][0]['severity']==expected


def test_completed_pods_and_cumulative_restarts_are_not_critical():
    s=snapshot();s['resources']['pods']['items']=[pod(),pod('Succeeded')]
    h=health(s)
    assert h['status']=='healthy' and h['pods']['ready']==h['pods']['active']==1
    assert h['pods']['restarts']==74


@pytest.mark.parametrize('seconds,severity',[(10,'warning'),(600,'critical')])
def test_terminating_pod_is_visible_and_cannot_make_health_green(seconds,severity):
    s=snapshot();terminating=pod()
    terminating['metadata']['deletionTimestamp']=stamp(seconds)
    s['resources']['pods']['items']=[terminating]
    result=summarize(s,'one','One');h=result['health']
    assert h['status']==h['pods']['health']==result['workloads']['pods']['health']==severity
    assert h['pods']['active']==h['pods']['terminating']==h['pods']['total']==1
    assert h['pods']['healthy']==h['pods']['ready']==result['workloads']['pods']['positive']==0
    assert result['workloads']['pods']['states']=={'terminating':1}
    assert h['issues'][0]['severity']==severity


def test_running_jobs_and_suspended_cron_are_normal_not_75_percent_error():
    s=snapshot();s['resources']['jobs']['items']=[{'status':{'active':1}}]
    s['resources']['cronjobs']['items']=[{'spec':{'suspend':True}}]
    d=summarize(s,'one','One')
    assert d['health']['status']=='healthy'
    assert d['workloads']['jobs']['health']=='healthy'
    assert d['workloads']['cronjobs']['health']=='healthy'


def test_failed_job_is_warning_and_rollout_deadline_is_critical():
    s=snapshot();s['resources']['jobs']['items']=[{'status':{'conditions':[{'type':'Failed','status':'True'}]}}]
    assert health(s)['status']=='warning'
    s['resources']['deployments']['items']=[{'spec':{'replicas':2},'status':{'readyReplicas':1,'conditions':[{'type':'Progressing','status':'False','reason':'ProgressDeadlineExceeded'}]}}]
    assert health(s)['status']=='critical'
    assert health(s)['issues'][0]['severity']=='critical'


def test_recent_warning_event_is_context_not_current_failure():
    s=snapshot();s['resources']['events']['items']=[{'type':'Warning','reason':'Unhealthy','lastTimestamp':stamp(60)},{'type':'Warning','lastTimestamp':stamp(90000)}]
    h=health(s)
    assert h['status']=='healthy' and len(h['events'])==1


def test_one_healthy_and_one_unavailable_cluster_is_not_all_green():
    a=health(snapshot());b=health({})
    assert aggregate([a,b])['status']=='unknown'
    assert aggregate([a,b])['nodes']['health']=='unknown'


def test_node_critical_survives_many_workload_warnings_and_limit():
    s=snapshot();s['resources']['pods']['items']=[pod('Pending',ready='False') for _ in range(80)]
    s['resources']['nodes']['items'][-1]['status']['conditions'][0]['status']='False'
    h=health(s)
    assert len(h['issues'])==40 and h['issues_total']==81
    assert h['issues'][0]['kind']=='nodes'


def test_api_exposes_real_health_and_survives_invalid_timestamp(client,monkeypatch):
    add(client);s=snapshot()
    monkeypatch.setattr(connectors,'collect_cluster',lambda cfg:deepcopy(s))
    client.post('/api/clusters/one/refresh',headers=MUTATION)
    d=client.get('/api/dashboard').json()
    assert d['health']['nodes']['ready']==6 and len(d['clusters'][0]['health']['node_rows'])==6
    s['observed_at']='invalid'
    client.post('/api/clusters/one/refresh',headers=MUTATION)
    response=client.get('/api/dashboard')
    assert response.status_code==200 and response.json()['health']['status']=='unknown'


def test_long_unavailable_deployment_and_lost_pvc_are_critical():
    s=snapshot();s['resources']['deployments']['items']=[{'spec':{'replicas':2},'status':{'readyReplicas':1,'conditions':[{'type':'Available','status':'False','lastTransitionTime':stamp(600)}]}}]
    assert health(s)['status']=='critical'
    s=snapshot();s['resources']['persistentvolumeclaims']['items']=[{'metadata':{'name':'db-volume'},'status':{'phase':'Lost'}}]
    assert health(s)['issues'][0]['title']=='PVC Lost'


def test_stale_critical_is_not_counted_as_a_current_incident():
    s=snapshot();s['resources']['nodes']['items'][0]['status']['conditions'][0]['status']='False'
    s['observed_at']=stamp(10000)
    h=health(s)
    assert h['status']=='stale' and h['issues'][0]['stale']
    assert aggregate([h])['critical']==0


def test_partial_pause_and_missing_ready_condition_are_unknown():
    s=snapshot();s['resources']['nodes']['items'][0]['status']['conditions']=[]
    assert health(s)['status']=='unknown'
    good=health(snapshot());paused={**good,'status':'paused','nodes':{**good['nodes'],'health':'unknown'}}
    assert aggregate([good,paused])['status']=='unknown'
