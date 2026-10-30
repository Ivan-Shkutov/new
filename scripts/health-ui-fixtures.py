from datetime import datetime,timedelta,timezone
from pulse_app.dashboard import summarize,combine
from pulse_app.health import aggregate

def payload(case):
    ts=(datetime.now(timezone.utc)-timedelta(hours=1 if case=='stale' else 0)).isoformat()
    kinds=('nodes','pods','daemonsets','deployments','replicasets','statefulsets','jobs','cronjobs','events','persistentvolumeclaims')
    snap={'observed_at':ts,'complete':True,'errors':[],'resources':{k:{'items':[],'error':None,'truncated':False} for k in kinds}}
    for i in range(6):
        bad=case=='critical' and i==4
        snap['resources']['nodes']['items'].append({'metadata':{'name':f'peoplehub-node-0{i+1}'},'status':{'conditions':[{'type':'Ready','status':'False' if bad else 'True','reason':'KubeletNotReady' if bad else 'KubeletReady'},{'type':'DiskPressure','status':'False'}],'nodeInfo':{'kubeletVersion':'v1.30.14'},'addresses':[{'type':'InternalIP','address':f'10.128.73.{i+5}'}]}})
    for i in range(163):
        bad=case=='critical' and i==0
        snap['resources']['pods']['items'].append({'metadata':{'name':'web-api' if bad else f'pod-{i:03d}','namespace':'application','creationTimestamp':ts},'spec':{'nodeName':f'peoplehub-node-0{i%6+1}'},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'False' if bad else 'True','lastTransitionTime':ts}],'containerStatuses':[{'restartCount':2 if bad else 0,'state':{'waiting':{'reason':'CrashLoopBackOff'}} if bad else {'running':{}}}]}})
    for kind,n in [('daemonsets',4),('deployments',14),('replicasets',20),('statefulsets',4)]:
        snap['resources'][kind]['items']=[{'metadata':{'name':f'{kind}-{i}'},'spec':{'replicas':2},'status':{'readyReplicas':2,'desiredNumberScheduled':6,'numberReady':6}} for i in range(n)]
    snap['resources']['jobs']['items']=[{'status':{'active':1}},{'status':{'conditions':[{'type':'Complete','status':'True'}]}}]
    snap['resources']['cronjobs']['items']=[{'spec':{'suspend':False}},{'spec':{'suspend':True}}]
    summary=summarize(snap,'peoplehub','PeopleHub (тест)')
    row={'id':'peoplehub','name':'PeopleHub (тест)','enabled':True,'observed_at':ts,'status':'ok','counts':{'nodes':6,'pods':163,'namespaces':14},'sources':{'metrics':True,'astra':False},'error':'',**summary}
    return {'version':'0.8.0','clusters':[row],'workloads':combine([summary]),'issues':summary['issues'],'issues_total':summary['issues_total'],'health':aggregate([summary['health']]),'llm_configured':True}
