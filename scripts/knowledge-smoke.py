#!/usr/bin/env python3
"""Real Qdrant REST integration. Embeddings fixture is explicit, not a semantic-quality test."""
import argparse,hashlib,json,os,sys,tempfile,threading,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pulse_app.knowledge import Knowledge
from pulse_app.store import Store
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qdrant',default='http://127.0.0.1:16333');a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  assert self.path=='/api/embed'
  payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  vectors=[]
  for text in payload['input']:
   vector=[1.,0.,0.] if 'Evicted' in text else [0.,1.,0.]
   vectors.append(vector)
  body=json.dumps({'embeddings':vectors}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
os.environ.update(PULSE_QDRANT_URL=a.qdrant,PULSE_EMBEDDINGS_URL='http://127.0.0.1:'+str(server.server_port),PULSE_EMBEDDINGS_MODEL='test-fixture-'+uuid.uuid4().hex,PULSE_EMBEDDINGS_DIMENSION='3')
root=Path(tempfile.mkdtemp(prefix='pulse-qdrant-check-'));kb=Knowledge(Store(root))
def doc(cid,version):return dict(name='Eviction '+version,cluster_id=cid,source='https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/',product='kubernetes',version=version,language='en',body='Evicted: EmptyDir exceeds sizeLimit. Diagnostic test fixture. '*40)
try:
 one=kb.ingest(doc('one','1.30'));kb.ingest(doc('other','1.30'));kb.ingest(doc('','1.31'))
 hits,note=kb.search('Evicted','one',kubernetes_version='1.30')
 assert hits and all(r['version']=='1.30' for r in hits),hits
 assert kb.status()['qdrant']=='ready'
 original=kb.store.rows('SELECT * FROM knowledge_documents WHERE id=?',(one['id'],))[0]
 kb.request('DELETE','/collections/'+kb.collection)
 assert kb.search('Evicted','one')[0]==[]
 kb.ingest(original)
 assert kb.search('Evicted','one',kubernetes_version='1.30')[0]
 kb.delete(one['id']);assert kb.search('Evicted','one',kubernetes_version='1.30')[0]==[]
 print('PASS: real Qdrant create/upsert/query; model HTTP embedding contract (fixture); cluster + Kubernetes-version filters; catalogue; canonical reindex; deletion. Real embedding quality is NOT measured.')
finally:
 kb.request('DELETE','/collections/'+kb.collection);server.shutdown();thread.join()
