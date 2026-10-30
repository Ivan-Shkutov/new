#!/usr/bin/env python3
"""Disposable UI + real API + real Qdrant. Embeddings deliberately use a local fixture."""
import base64,json,os,socket,subprocess,sys,tempfile,threading,time,urllib.request,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pulse_app.knowledge import Knowledge
from pulse_app.store import Store
class Embedding(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  d=json.loads(self.rfile.read(int(self.headers['Content-Length'])));out=json.dumps({'embeddings':[[1,0,0] for _ in d['input']]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(out)
emb=ThreadingHTTPServer(('127.0.0.1',0),Embedding);thread=threading.Thread(target=emb.serve_forever,daemon=True);thread.start()
folder=Path(tempfile.mkdtemp(prefix='pulse-vector-ui-'));runtime=folder/'runtime';(runtime/'secrets').mkdir(parents=True);password='fixture-'+uuid.uuid4().hex;(runtime/'secrets/portal-password').write_text(password)
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
env=dict(os.environ,PULSE_RUNTIME=str(runtime),PULSE_PASSWORD_FILE=str(runtime/'secrets/portal-password'),PULSE_QDRANT_URL='http://127.0.0.1:16333',PULSE_EMBEDDINGS_URL=f'http://127.0.0.1:{emb.server_port}',PULSE_EMBEDDINGS_DIMENSION='3',PULSE_EMBEDDINGS_REVISION='',PULSE_EMBEDDINGS_MODEL='ui-fixture-'+uuid.uuid4().hex)
log=(folder/'server.log').open('w');proc=subprocess.Popen([sys.executable,'-m','uvicorn','pulse_app.app:app','--host','127.0.0.1','--port',str(port)],env=env,cwd=ROOT,stdout=log,stderr=log)
base=f'http://127.0.0.1:{port}'
try:
 for _ in range(100):
  try:
   with urllib.request.urlopen(base+'/healthz',timeout=1) as r:break
  except Exception:time.sleep(.1)
 else:raise AssertionError('API did not start')
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);context=browser.new_context(http_credentials={'username':'pulse','password':password},viewport={'width':1366,'height':900});page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(base+'/#knowledge');form=page.locator('#vector-upload-form');form.locator('[name=file]').set_input_files({'name':'official-test.md','mimeType':'text/markdown','buffer':b'Kubernetes Evicted. EmptyDir exceeds sizeLimit. This is an explicit integration test fixture.'});form.locator('[name=source]').fill('https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/');form.locator('[name=version]').fill('1.30');form.get_by_role('button',name='Индексировать в Qdrant').click();expect(form.locator('.form-message')).to_contain_text('проиндексирован',timeout=20000)
  page.locator('#vector-documents').get_by_role('button',name='Открыть',exact=True).click();expect(page.locator('#detail-body')).to_contain_text('EmptyDir exceeds sizeLimit');page.get_by_role('button',name='Закрыть dashboard',exact=True).click()
  search=page.locator('#vector-search-form');search.locator('[name=q]').fill('Evicted EmptyDir');search.get_by_role('button',name='Найти в Qdrant',exact=True).click();expect(page.locator('#vector-documents')).to_contain_text('official-test.md');expect(page.locator('#vector-documents')).to_contain_text('EmptyDir exceeds sizeLimit')
  page.screenshot(path='/tmp/pulse-0130-knowledge.png',full_page=True)
  search.locator('[name=q]').fill('');search.get_by_role('button',name='Найти в Qdrant',exact=True).click();page.once('dialog',lambda d:d.accept());page.locator('#vector-documents').get_by_role('button',name='Удалить',exact=True).click();expect(page.locator('#vector-documents')).to_contain_text('Документов: 0');assert not errors,errors;browser.close()
 print('PASS: browser + real API + real Qdrant upload/index; metadata; full document view; semantic endpoint with fixture vectors; search; delete; no JS errors. Embedding relevance is not measured.')
finally:
 proc.terminate();proc.wait(timeout=15);log.close()
 os.environ.update({k:env[k] for k in env if k.startswith('PULSE_')});kb=Knowledge(Store(runtime));kb.request('DELETE','/collections/'+kb.collection);emb.shutdown();thread.join()
