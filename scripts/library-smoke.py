#!/usr/bin/env python3
"""Actual archive watcher -> Pulse API/catalogue -> real Qdrant, fixture embeddings."""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pulse_app.library import api_sender,import_path
from pulse_app.knowledge import Knowledge
from pulse_app.store import Store


class Embeddings(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        payload=json.dumps({'embeddings':[[1,0,0] for _ in body['input']]}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(payload)


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Embeddings)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with tempfile.TemporaryDirectory(prefix='pulse-library-check-') as directory:
        root=Path(directory);runtime=root/'runtime';(runtime/'secrets').mkdir(parents=True)
        password='fixture-'+uuid.uuid4().hex;secret=runtime/'secrets/portal-password';secret.write_text(password)
        inbox=root/'inbox';product=inbox/'kubernetes';product.mkdir(parents=True)
        metadata={'product':'kubernetes','version':'1.30','source':'https://kubernetes.io/docs/','language':'en'}
        (product/'manifest.json').write_text(json.dumps(metadata))
        archive=product/'library.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('guide.md','# EmptyDir\nEvicted because EmptyDir data exceeded the sizeLimit. This is controlled test documentation.')
            z.writestr('ignored.html','Not Markdown')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin='http://127.0.0.1:'+str(port)
        env=dict(os.environ,PULSE_RUNTIME=str(runtime),PULSE_PASSWORD_FILE=str(secret),PULSE_QDRANT_URL='http://127.0.0.1:16333',PULSE_EMBEDDINGS_URL='http://127.0.0.1:'+str(server.server_port),PULSE_EMBEDDINGS_MODEL='library-fixture-'+uuid.uuid4().hex,PULSE_EMBEDDINGS_DIMENSION='3',PULSE_EMBEDDINGS_REVISION='')
        headers={'Authorization':'Basic '+base64.b64encode(('pulse:'+password).encode()).decode()}
        def api(path):
            with urllib.request.urlopen(urllib.request.Request(origin+'/api'+path,headers=headers),timeout=10) as r:return json.load(r)
        log=(root/'process.log').open('w');proc=watch=None
        try:
            proc=subprocess.Popen([sys.executable,'-m','uvicorn','pulse_app.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            for _ in range(100):
                try:api('/knowledge/status');break
                except OSError:time.sleep(.1)
            else:raise AssertionError('Pulse did not start')
            reports=runtime/'library-reports'
            watch=subprocess.Popen([sys.executable,'-m','pulse_app.library','--watch','--path',str(inbox),'--reports',str(reports),'--base-url',origin,'--password-file',str(secret)],cwd=ROOT,env=env,stdout=log,stderr=log)
            deadline=time.monotonic()+50
            while time.monotonic()<deadline:
                items=api('/knowledge/imports')['items']
                if items and items[0].get('status')=='complete':break
                if watch.poll() is not None:raise AssertionError('Watcher exited: '+(root/'process.log').read_text()[-800:])
                time.sleep(.5)
            else:raise AssertionError('Watcher did not publish the archive')
            assert items[0]['indexed']==1 and items[0]['skipped']==1,items
            docs=api('/knowledge/documents')['items'];assert len(docs)==1 and docs[0]['status']=='ready'
            found=api('/knowledge/search?q=EmptyDir')['items'];assert found and found[0]['name']=='guide.md'
            again=import_path(archive,metadata,api_sender(origin,secret,'pulse'))
            assert again['unchanged']==1 and again['indexed']==0,again
            print('PASS: real ZIP watcher, Markdown-only import, report API, publication catalogue, real Qdrant, search, idempotent repeat. Embeddings are a numerical fixture, not a relevance benchmark.')
        finally:
            if watch:watch.terminate();watch.wait(timeout=10)
            if proc:proc.terminate();proc.wait(timeout=15)
            log.close()
            os.environ.update({k:v for k,v in env.items() if k.startswith('PULSE_')})
            kb=Knowledge(Store(runtime));kb.request('DELETE','/collections/'+kb.collection)
            server.shutdown();thread.join()


if __name__=='__main__':main()
