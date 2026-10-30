import json,time
import httpx
import pytest
from test_api import client,runtime,add,MUTATION
from test_connectors import install_transport
from pulse_app import cluster_chat as chat,connectors as c
from test_sessions import session,message


def pods():
    result=[]
    for i in range(163):
        status={'phase':'Running','conditions':[{'type':'Ready','status':'True'}],'containerStatuses':[{'name':'app','state':{'running':{}},'restartCount':0}]}
        if 145<=i<162:status={'phase':'Succeeded','containerStatuses':[{'name':'app','state':{'terminated':{'exitCode':0,'reason':'Completed'}}}]}
        if i==162:status={'phase':'Failed','reason':'Evicted','message':'Usage of EmptyDir volume data exceeds the limit 1Gi.'}
        result.append({'metadata':{'name':f'pod-{i:03d}','namespace':'am-agent' if i==162 else 'work','uid':f'uid-{i}'},'spec':{'containers':[{'name':'app'}],'nodeName':'node-1'},'status':status})
    return result


@pytest.mark.parametrize('question,intent',[
 ('выведи все проблемные pods, все что не в статусе Running','pods_non_running'),
 ('покажи все нездоровые поды','pods_unhealthy'),('show all pods not Running','pods_non_running'),
 ('сколько всего pods','pods_all'),('покажи ноды','nodes_all'),
 ('Покажи логи pod backend',None),('Почему Pod перезапускается?',None),('Объясни requests и limits',None)])
def test_explicit_readonly_intents(question,intent):assert chat.inventory_intent(question)==intent


def test_large_snapshot_keeps_bad_pod_and_valid_json():
    snap={'observed_at':'2026-10-30T10:00:00Z','complete':True,'resources':{'nodes':{'items':[{'metadata':{'name':'node-1'},'status':{'conditions':[{'type':'Ready','status':'True','message':'x'*50000}]}}]},'pods':{'items':pods()}}}
    context=chat.compact_snapshot(snap)
    text=json.dumps(context,ensure_ascii=False);assert len(text)<=30000
    parsed=json.loads(text);entry=parsed['resources']['pods']
    assert entry['count']==163 and entry['phases']=={'Running':145,'Succeeded':17,'Failed':1}
    assert entry['healthy']==162 and entry['unhealthy']==1 and entry['items'][0]['reason']=='Evicted'
    assert parsed['resources']['nodes']['items'][0]['name']=='node-1'


def paginated(request):
    assert request.method=='GET' and request.url.path=='/api/v1/pods'
    values=pods();second=request.url.params.get('continue')=='next'
    return httpx.Response(200,json={'items':values[100:] if second else values[:100],'metadata':{'continue':'' if second else 'next','resourceVersion':'123'}})


def test_full_pagination_counts_completed_and_failed_last(monkeypatch):
    seen=[]
    def handler(r):seen.append(r);return paginated(r)
    install_transport(monkeypatch,handler)
    report=chat.inventory_report({'server':'https://cluster.test','token':'reader'},'pods_non_running')
    assert report['complete'] and len(seen)==2 and report['matched']==18
    assert report['counts']['total']==163 and report['counts']['healthy']==162
    assert report['rows'][0]['reason']=='Evicted'
    assert sum(x['healthy'] for x in report['rows'])==17
    answer=chat.inventory_answer(report)
    assert 'pod-162' in answer and 'pod-145' in answer and 'Succeeded' in answer


def test_chat_reads_all_pages_without_calling_model_or_saving_history(client,monkeypatch):
    add(client)
    install_transport(monkeypatch,paginated)
    monkeypatch.setattr(c,'llm_chat',lambda *args:pytest.fail('The model must not invent an inventory'))
    sid=session(client,'one')
    job=message(client,sid,'выведи все проблемные pods, все что не в статусе Running')
    assert job['status']=='done' and 'pod-162' in job['answer'] and '163' in job['answer']
    report=json.loads(job['evidence']);assert report['matched']==18 and report['complete']
    assert client.get('/api/investigations').json()['items']==[]


def test_failed_list_never_claims_cluster_has_no_bad_pods(client,monkeypatch):
    add(client);install_transport(monkeypatch,lambda r:httpx.Response(403,json={}))
    sid=session(client,'one');job=message(client,sid,'Покажи все проблемные pods')
    assert job['status']=='failed' and 'HTTP 403' in job['answer']
    assert 'отсутствие объектов не доказано' in job['answer']
    assert 'По выбранному фильтру объектов нет' not in job['answer']


def test_live_inventory_rest_scope_and_paging(client,monkeypatch):
    add(client);install_transport(monkeypatch,paginated)
    result=client.get('/api/clusters/one/inventory?filter=non_running&offset=0&limit=10').json()
    assert result['matched']==18 and len(result['rows'])==10 and result['complete']
    assert client.get('/api/clusters/missing/inventory').status_code==404
    assert client.get('/api/clusters/one/inventory?kind=secrets').status_code==422


def test_missing_pod_source_is_unknown_instead_of_zero_healthy():
    entry=chat.compact_snapshot({'resources':{}})['resources']['pods']
    assert entry['count'] is None and entry['healthy'] is None and entry['error']
