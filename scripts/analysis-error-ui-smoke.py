#!/usr/bin/env python3
"""Disposable runtime only: reproduce Astra 401 and explicitly retry one Pod locally.

Actual Docker/API/browser and TLS transport; model text is a controlled fixture.
This checks this incident's data path, not the accuracy of a production model.
"""
import argparse,ipaddress,json,ssl,sys,tempfile,threading,uuid
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pulse_app.store import Store
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-url',default='http://127.0.0.1:18091');p.add_argument('--runtime',type=Path,required=True);a=p.parse_args()
cid='analysis-fix-'+uuid.uuid4().hex[:6];calls={'astra':0,'local':0}
pod={'metadata':{'name':'bad','namespace':'am-agent','uid':'old-uid'},'spec':{'nodeName':'worker-2','containers':[{'name':'agent'}],'volumes':[{'name':'data','emptyDir':{'sizeLimit':'1Gi'}}]},'status':{'phase':'Failed','reason':'Evicted','message':'Usage of EmptyDir volume "data" exceeds the limit "1Gi".'}}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def respond(self,body,status=200):
  payload=json.dumps(body).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(payload)
 def do_GET(self):
  assert self.headers.get('Authorization')=='Bearer fixture-reader'
  if self.path=='/api/v1/namespaces/am-agent/pods/bad':return self.respond(pod)
  if self.path=='/api/v1/nodes/worker-2':return self.respond({'metadata':{'name':'worker-2'},'status':{'conditions':[{'type':'Ready','status':'True'}]}})
  if '/log' in self.path:return self.respond({},404)
  return self.respond({'items':[]})
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path.endswith('/astra'):
   calls['astra']+=1;return self.respond({'error':'fixture unauthorized'},401)
  assert self.path=='/v1/chat/completions' and body['stream'] is False
  calls['local']+=1
  if len(body['messages'])>1:
   assert 'EmptyDir' in body['messages'][-1]['content'] and '1Gi' in body['messages'][-1]['content']
  return self.respond({'choices':[{'message':{'content':'Причина: EmptyDir data превысил 1Gi [E01]. Причина роста файлов не доказана; нужны логи приложения.'},'finish_reason':'stop'}]})
with tempfile.TemporaryDirectory(prefix='pulse-analysis-tls-') as folder:
 root=Path(folder);key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Loopback test fixture')]);now=datetime.now(timezone.utc)
 cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),True).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False).sign(key,hashes.SHA256())
 pem=cert.public_bytes(serialization.Encoding.PEM).decode();(root/'ca.pem').write_text(pem);(root/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
 tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(root/'ca.pem',root/'key.pem')
 kube=ThreadingHTTPServer(('127.0.0.1',0),Handler);kube.socket=tls.wrap_socket(kube.socket,server_side=True);model=ThreadingHTTPServer(('127.0.0.1',0),Handler)
 for server in (kube,model):threading.Thread(target=server.serve_forever,daemon=True).start()
 config={'apiVersion':'v1','kind':'Config','current-context':'test','contexts':[{'name':'test','context':{'cluster':'test','user':'reader'}}],'clusters':[{'name':'test','cluster':{'server':f'https://127.0.0.1:{kube.server_port}','certificate-authority-data':__import__('base64').b64encode(pem.encode()).decode()}}],'users':[{'name':'reader','user':{'token':'fixture-reader'}}]}
 try:
  with sync_playwright() as pw:
   browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);context=browser.new_context(http_credentials={'username':'pulse','password':(a.runtime/'secrets/portal-password').read_text().strip()},viewport={'width':1366,'height':900});headers={'X-Pulse-Request':'1'}
   before=context.request.get(a.base_url+'/api/settings').json()['llm'];assert not before['has_key'],'Use a disposable runtime'
   assert context.request.put(a.base_url+'/api/settings',data={'llm':{'url':f'http://127.0.0.1:{model.server_port}/v1','model':'qwen-fixture'}},headers=headers).ok
   created=context.request.post(a.base_url+'/api/clusters',data={'id':cid,'name':cid,'enabled':False,'kubeconfig':json.dumps(config),'astra_ai':{'enabled':True,'url':f'http://127.0.0.1:{model.server_port}/astra','model':'qwen-fixture','api_key':'fixture-rejected'}},headers=headers);assert created.status==201,created.text()
   try:
    Store(a.runtime).execute('INSERT INTO snapshots VALUES (?,?)',(cid,json.dumps({'observed_at':now.isoformat(),'complete':True,'resources':{'pods':{'items':[pod]}}})))
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(a.base_url);page.locator('#cluster-select').select_option(cid);page.get_by_role('button',name='Анализ ИИ: bad',exact=True).click()
    page.get_by_role('button',name='Проверить ИИ',exact=True).click();expect(page.locator('#analysis-engine-result')).to_contain_text('401',timeout=15000)
    page.get_by_role('button',name='Сжатый анализ',exact=True).click();expect(page.locator('#ai-conversation')).to_contain_text('Astra AI HTTP 401',timeout=15000);expect(page.locator('.chat-answer')).to_contain_text('превышен лимит EmptyDir');expect(page.locator('#ai-conversation')).to_contain_text('Полнота данных:');assert not calls['local']
    page.get_by_role('button',name='Повторить через локальную LLM',exact=True).click();expect(page.locator('#ai-status')).to_contain_text('Ответ получен',timeout=15000);expect(page.locator('.chat-answer')).to_contain_text('Причина роста файлов не доказана');assert calls['local']==1
    assert page.locator('#analysis-engine').input_value()=='local'
    page.get_by_role('button',name='Проверить ИИ',exact=True).click();expect(page.locator('#analysis-engine-result')).to_contain_text('Авторизация и завершение ответа: OK',timeout=15000)
    rows=context.request.get(a.base_url+'/api/clusters').json()['items'];assert next(r for r in rows if r['id']==cid)['astra_ai']['enabled']
    page.screenshot(path='/tmp/pulse-0141-analysis-fixed.png');page.get_by_role('button',name='Закрыть анализ',exact=True).click();expect(page.locator('#ai-conversation')).to_be_empty();assert not errors,errors
    print('PASS: actual Docker/API/TLS/browser; Astra 401 is actionable; verified EmptyDir facts and unavailable checks stay visible; no silent fallback; explicit local retry keeps the same problem; local completion probe; no provider configuration changes; closing clears dialog.')
   finally:
    context.request.delete(a.base_url+'/api/clusters/'+cid,headers=headers);context.request.put(a.base_url+'/api/settings',data={'llm':{'url':before['url'],'model':before['model']}},headers=headers);browser.close()
 finally:
  for server in (kube,model):server.shutdown();server.server_close()
