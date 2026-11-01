"""Versioned documentation in Qdrant, with real model embeddings and scope filters.

SQLite is the authoritative catalogue. Unpublished/old vectors are never cited.
"""
import hashlib
import json
import math
import os
import re
import threading
import uuid
from pathlib import Path

import httpx

from .connectors import ConnectorError


class Knowledge:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.url = os.getenv('PULSE_QDRANT_URL', '').rstrip('/')
        self.embed_url = os.getenv('PULSE_EMBEDDINGS_URL', '').rstrip('/')
        self.model = os.getenv('PULSE_EMBEDDINGS_MODEL', 'bge-m3')
        self.provider = os.getenv('PULSE_EMBEDDINGS_PROVIDER', 'ollama')
        self.revision = os.getenv('PULSE_EMBEDDINGS_REVISION','')
        self.dimension = int(os.getenv('PULSE_EMBEDDINGS_DIMENSION', '1024'))
        self.fingerprint = hashlib.sha256(f'{self.model}:{self.dimension}:{self.provider}:{self.revision}:chunks-v1'.encode()).hexdigest()[:16]
        self.collection = 'pulse_docs_' + self.fingerprint
        with store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS knowledge_documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, cluster_id TEXT NOT NULL,
                source TEXT NOT NULL, product TEXT NOT NULL, version TEXT NOT NULL,
                language TEXT NOT NULL, body TEXT NOT NULL, revision TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''')

    @property
    def configured(self):
        return bool(self.url and self.embed_url)

    def request(self, method, path, body=None, embedding=False):
        url = self.embed_url if embedding else self.url
        file = os.getenv('PULSE_EMBEDDINGS_KEY_FILE' if embedding else 'PULSE_QDRANT_KEY_FILE')
        headers = {}
        if file:
            try:key = Path(file).read_text().strip()
            except OSError:raise ConnectorError('Файл авторизации базы знаний недоступен') from None
            headers['Authorization' if embedding and self.provider=='openai' else 'api-key'] = ('Bearer ' if embedding and self.provider=='openai' else '')+key
        try:
            with httpx.Client(timeout=httpx.Timeout(120,connect=5),trust_env=False,follow_redirects=False) as client:
                r = client.request(method,url+path,json=body,headers=headers)
                if r.status_code==404 and not embedding and (method=='GET' or path.endswith('/points/query')):return None
                r.raise_for_status()
                if len(r.content)>8_000_000:raise ValueError('large response')
                data=r.json()
                if not isinstance(data,dict):raise ValueError('response must be an object')
                if not embedding and data.get('status','ok')!='ok':raise ValueError('Qdrant error response')
                return data
        except httpx.HTTPStatusError as exc:
            service='Embeddings' if embedding else 'Qdrant'
            raise ConnectorError(f'{service} HTTP {exc.response.status_code}; проверьте права и журналы источника') from None
        except (httpx.HTTPError,ValueError,OSError):
            raise ConnectorError('Embeddings недоступны или вернули некорректный ответ' if embedding else 'Qdrant недоступен или вернул некорректный ответ') from None

    def embed(self, texts):
        if self.provider=='ollama':
            if self.revision:
                tags=self.request('GET','/api/tags',embedding=True)
                if not isinstance(tags,dict) or not isinstance(tags.get('models'),list) or any(not isinstance(row,dict) for row in tags['models']):
                    raise ConnectorError('Неподдерживаемый каталог embedding-моделей')
                name=self.model if ':' in self.model else self.model+':latest'
                digest=next((row.get('digest') for row in tags.get('models',[]) if row.get('name')==name),None)
                if digest!=self.revision:raise ConnectorError('Embedding-модель изменилась. Обновите revision и переиндексируйте базу.')
            result=self.request('POST','/api/embed',{'model':self.model,'input':texts,'truncate':False},True)
            vectors=result.get('embeddings',[]) if isinstance(result,dict) else []
        elif self.provider=='openai':
            result=self.request('POST','/embeddings',{'model':self.model,'input':texts},True)
            try:data=sorted(result.get('data',[]),key=lambda r:r['index'])
            except (TypeError,AttributeError,KeyError):raise ConnectorError('Неподдерживаемый ответ embeddings') from None
            if [r['index'] for r in data]!=list(range(len(texts))):raise ConnectorError('Неверные индексы embeddings')
            try:vectors=[r['embedding'] for r in data]
            except (TypeError,KeyError):raise ConnectorError('Неподдерживаемый ответ embeddings') from None
        else:raise ConnectorError('Неизвестный провайдер embeddings')
        if not isinstance(vectors,list) or len(vectors)!=len(texts) or any(not isinstance(v,list) or len(v)!=self.dimension or any(isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) for n in v) or not any(v) for v in vectors):
            raise ConnectorError('Размерность или значения embeddings не соответствуют настройке')
        return vectors

    def collection_info(self, reply):
        try:
            info=reply['result'];vectors=info['config']['params']['vectors']
            compatible=isinstance(vectors,dict) and vectors.get('size')==self.dimension and vectors.get('distance')=='Cosine'
        except (TypeError,KeyError):
            raise ConnectorError('Некорректные метаданные коллекции Qdrant') from None
        if not compatible:raise ConnectorError('Несовместимая коллекция Qdrant')
        return info

    def status(self):
        counts=self.store.rows("SELECT CASE WHEN fingerprint!=? THEN 'needs_reindex' ELSE status END AS status,count(*) AS count FROM knowledge_documents GROUP BY CASE WHEN fingerprint!=? THEN 'needs_reindex' ELSE status END",(self.fingerprint,self.fingerprint))
        result={'configured':self.configured,'model':self.model,'dimension':self.dimension,'model_revision':self.revision,'collection':self.collection,'documents':counts}
        if self.configured:
            try:
                r=self.request('GET','/collections/'+self.collection)
                result['qdrant']='ready' if r is not None else 'collection_missing'
                result['points']=self.collection_info(r).get('points_count',0) if r is not None else 0
            except ConnectorError as e:result['qdrant']='unavailable';result['error']=str(e)
        return result

    def ensure(self):
        path='/collections/'+self.collection
        data=self.request('GET',path)
        if data is None:
            self.request('PUT',path,{'vectors':{'size':self.dimension,'distance':'Cosine'}})
            for key in ('cluster_id','doc_id'):
                self.request('PUT',path+'/index?wait=true',{'field_name':key,'field_schema':'keyword'})
        else:
            self.collection_info(data)

    def ingest(self, document):
        if not self.configured:raise ConnectorError('Настройте Qdrant и embeddings перед загрузкой')
        if not self.lock.acquire(blocking=False):raise ConnectorError('Идёт индексация другого документа. Повторите позже.')
        try:
            fields=['name','cluster_id','source','product','version','language','body']
            d={k:document.get(k,'') for k in fields}
            did=hashlib.sha256(json.dumps([d[k] for k in ('cluster_id','source','product','version','language','name')],ensure_ascii=False).encode()).hexdigest()
            revision=hashlib.sha256(d['body'].encode()).hexdigest()
            size=self.store.rows('SELECT coalesce(sum(length(body)),0) AS n FROM knowledge_documents WHERE id!=?',(did,))[0]['n']
            if size+len(d['body'])>500_000_000:raise ConnectorError('Корпус превышает 500 млн символов')
            with self.store.connect() as c:
                c.execute('INSERT OR REPLACE INTO knowledge_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(did,*[d[k] for k in fields],revision,self.fingerprint,'indexing',''))
            try:
                self.ensure()
                chunks=[d['body'][i:i+1800] for i in range(0,len(d['body']),1550)]
                for start in range(0,len(chunks),16):
                    batch=chunks[start:start+16];vectors=self.embed(batch);points=[]
                    for i,(text,vector) in enumerate(zip(batch,vectors),start):
                        pid=str(uuid.uuid5(uuid.NAMESPACE_URL,f'{did}:{revision}:{i}'))
                        points.append({'id':pid,'vector':vector,'payload':{k:v for k,v in {**d,'body':None,'doc_id':did,'revision':revision,'chunk':i,'text':text}.items() if k!='body'}})
                    self.request('PUT','/collections/'+self.collection+'/points?wait=true',{'points':points})
                self.store.execute("UPDATE knowledge_documents SET status='ready',error='' WHERE id=?",(did,))
                # Remove old revisions only after publishing the new one.
                self.request('POST','/collections/'+self.collection+'/points/delete?wait=true',{'filter':{'must':[{'key':'doc_id','match':{'value':did}}],'must_not':[{'key':'revision','match':{'value':revision}}]}})
                return {'id':did,'chunks':len(chunks),'status':'ready'}
            except Exception as exc:
                self.store.execute("UPDATE knowledge_documents SET status='failed',error=? WHERE id=?",(str(exc)[:200],did))
                raise
        finally:self.lock.release()

    def search(self, query, cid, limit=6, kubernetes_version=None, exclude_saas=False):
        if not self.configured:return [],'Qdrant не настроен'
        vector=self.embed([query[:6000]])[0]
        filter={'should':[{'key':'cluster_id','match':{'value':''}},{'key':'cluster_id','match':{'value':cid}}]}
        if exclude_saas:filter['must_not']=[{'key':'product','match':{'value':'vk-workspace-saas'}}]
        if kubernetes_version:
            filter['must']=[{'should':[{'must_not':[{'key':'product','match':{'value':'kubernetes'}}]},{'key':'version','match':{'value':kubernetes_version}}]}]
        result=self.request('POST','/collections/'+self.collection+'/points/query',{'query':vector,'limit':30,'with_payload':True,'filter':filter})
        if result is None:return [],'Коллекция отсутствует: загрузите или переиндексируйте документы'
        hits=[];seen=set()
        points=result.get('result',{}).get('points') if isinstance(result,dict) and isinstance(result.get('result'),dict) else None
        if not isinstance(points,list):raise ConnectorError('Некорректный ответ поиска Qdrant')
        for row in points:
            if not isinstance(row,dict) or not isinstance(row.get('payload'),dict):raise ConnectorError('Некорректный документ в ответе Qdrant')
            p=row['payload'];did=p.get('doc_id')
            if not isinstance(did,str) or not isinstance(p.get('revision'),str) or not isinstance(p.get('chunk'),int) or isinstance(p['chunk'],bool) or p['chunk']<0:
                raise ConnectorError('Некорректная идентичность документа Qdrant')
            if p.get('cluster_id') not in ('',cid):continue
            if exclude_saas and p.get('product')=='vk-workspace-saas':continue
            if kubernetes_version and p.get('product')=='kubernetes' and p.get('version')!=kubernetes_version:continue
            valid=self.store.rows("SELECT id FROM knowledge_documents WHERE id=? AND revision=? AND fingerprint=? AND status='ready' AND cluster_id IN ('',?)",(did,p.get('revision'),self.fingerprint,cid))
            if not valid or (did,p.get('chunk')) in seen:continue
            seen.add((did,p.get('chunk')))
            hits.append({'id':f'D{len(hits)+1:02d}','name':p.get('name'),'source':p.get('source'),'product':p.get('product'),'version':p.get('version'),'language':p.get('language'),'chunk':p.get('chunk'),'score':row.get('score'),'excerpt':str(p.get('text',''))[:1800]})
            if len(hits)>=limit:break
        return hits,('Семантический поиск: совпадений '+str(len(hits)))

    def delete(self, did):
        # Delete authoritative row first: orphan vectors cannot be cited even if Qdrant is down.
        count=self.store.execute('DELETE FROM knowledge_documents WHERE id=?',(did,))
        if not count:return False
        if self.configured:
            try:self.request('POST','/collections/'+self.collection+'/points/delete?wait=true',{'filter':{'must':[{'key':'doc_id','match':{'value':did}}]}})
            except ConnectorError:pass
        return True
