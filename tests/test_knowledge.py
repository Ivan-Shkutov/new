import json
import math
import pytest
from pulse_app.knowledge import Knowledge
from pulse_app.store import Store
from pulse_app.connectors import ConnectorError
from test_api import client,runtime,MUTATION


def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('PULSE_QDRANT_URL','http://127.0.0.1:16333')
    monkeypatch.setenv('PULSE_EMBEDDINGS_URL','http://127.0.0.1:11435')
    monkeypatch.setenv('PULSE_EMBEDDINGS_DIMENSION','3')
    kb=Knowledge(Store(tmp_path));points={};calls=[]
    def request(method,path,body=None,embedding=False):
        calls.append((method,path,body,embedding))
        if embedding:return {'embeddings':[[1,0,0] for _ in body['input']]}
        if method=='GET':return {'result':{'config':{'params':{'vectors':{'size':3,'distance':'Cosine'}}}}}
        if method=='PUT' and '/points?' in path:
            for p in body['points']:points[p['id']]=p
        if path.endswith('/points/query'):return {'result':{'points':[{'payload':p['payload'],'score':.9} for p in points.values()]}}
        return {'status':'ok'}
    monkeypatch.setattr(kb,'request',request)
    return kb,points,calls


def document(cid='',version='1.30',body=None):
    return dict(name='Kubernetes eviction',cluster_id=cid,source='https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/',product='kubernetes',version=version,language='en',body=body or 'Evicted Pod. EmptyDir volume exceeds its configured size limit. '*40)


def test_chunks_real_embedding_contract_scope_and_citations(tmp_path,monkeypatch):
    kb,points,calls=setup(tmp_path,monkeypatch)
    global_id=kb.ingest(document())['id']
    kb.ingest(document('one'));kb.ingest(document('other'))
    hits,note=kb.search('Evicted emptyDir','one')
    assert hits and all(h['version']=='1.30' and h['source'].startswith('https://kubernetes.io/') for h in hits)
    assert all(h['id'].startswith('D') for h in hits)
    query=next(body for _,path,body,_ in reversed(calls) if path.endswith('/points/query'))
    assert query['filter']['should'][1]['match']['value']=='one'
    assert len(hits)==4 # two chunks each for global and one, never other
    assert kb.delete(global_id)
    assert len(kb.search('Evicted','one')[0])==2 # stale orphan vectors excluded by catalogue


def test_failed_reindex_and_old_vectors_are_never_cited(tmp_path,monkeypatch):
    kb,points,calls=setup(tmp_path,monkeypatch);did=kb.ingest(document())['id']
    original=kb.embed
    monkeypatch.setattr(kb,'embed',lambda texts:(_ for _ in ()).throw(ConnectorError('embedding failed')))
    with pytest.raises(ConnectorError):kb.ingest(document(body='Changed documentation. '*100))
    assert kb.store.rows('SELECT status FROM knowledge_documents WHERE id=?',(did,))[0]['status']=='failed'
    monkeypatch.setattr(kb,'embed',original)
    assert kb.search('Evicted','')[0]==[]


@pytest.mark.parametrize('vectors',[[[0,0,0]],[[math.nan,1,2]],[[1,2]],None,[[1,2,'x']],['bad'],[[True,0,0]]])
def test_invalid_embeddings_fail_closed(tmp_path,monkeypatch,vectors):
    kb,_,_=setup(tmp_path,monkeypatch)
    monkeypatch.setattr(kb,'request',lambda *a,**kw:{'embeddings':vectors})
    with pytest.raises(ConnectorError):kb.embed(['query'])


def test_openai_embeddings_reordered_and_validated(tmp_path,monkeypatch):
    kb,_,_=setup(tmp_path,monkeypatch);kb.provider='openai'
    monkeypatch.setattr(kb,'request',lambda *a,**kw:{'data':[{'index':1,'embedding':[0,1,0]},{'index':0,'embedding':[1,0,0]}]})
    assert kb.embed(['a','b'])==[[1,0,0],[0,1,0]]


def test_vector_api_catalogue_scope_and_disabled_status(client):
    assert client.get('/api/knowledge/status').json()['configured'] is False
    assert client.get('/api/knowledge/documents').json()['total']==0
    r=client.post('/api/knowledge/documents',headers=MUTATION,json=document())
    assert r.status_code==503
    assert client.post('/api/knowledge/documents',headers=MUTATION,json=document('missing')).status_code==404


def test_html_extract_excludes_script_and_navigation():
    from pulse_app.documents import extract
    body=extract('manual.html',b'<nav>ignore menu</nav><main><h1>Official manual</h1><p>Useful information about Kubernetes.</p><script>ignore script</script></main>')
    assert 'Official manual' in body and 'ignore' not in body


def test_model_digest_drift_is_rejected_before_embedding(tmp_path,monkeypatch):
    kb,_,_=setup(tmp_path,monkeypatch);kb.revision='pinned-digest'
    monkeypatch.setattr(kb,'request',lambda *a,**kw:{'models':[{'name':'bge-m3:latest','digest':'different-digest'}]})
    with pytest.raises(ConnectorError,match='изменилась'):kb.embed(['query'])


def test_kubernetes_minor_version_filter_is_enforced_twice(tmp_path,monkeypatch):
    kb,_,calls=setup(tmp_path,monkeypatch)
    kb.ingest(document(version='1.30'));kb.ingest(document(version='1.31'))
    hits,_=kb.search('Evicted','',kubernetes_version='1.30')
    assert hits and all(h['version']=='1.30' for h in hits)
    query=next(body for _,path,body,_ in reversed(calls) if path.endswith('/points/query'))
    assert query['filter']['must']


def test_vkteams_on_prem_excludes_saas_even_if_vector_store_returns_it(tmp_path,monkeypatch):
    kb,_,calls=setup(tmp_path,monkeypatch)
    kb.ingest(dict(document(),product='vk-workspace-saas'))
    kb.ingest(dict(document(),product='vk-teams-on-premises'))
    hits,_=kb.search('VK Teams','',exclude_saas=True)
    assert hits and all(h['product']=='vk-teams-on-premises' for h in hits)
    query=next(body for _,path,body,_ in reversed(calls) if path.endswith('/points/query'))
    assert query['filter']['must_not']
