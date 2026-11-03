#!/usr/bin/env python3
"""Disposable runtime only: scenario Astra 401 and explicit local structured retry.

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
cid='analysis-fix-'+uuid.uuid4().hex[:6];calls={'astra':0,'local':0,'invalid':False}
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
  if '/events?' in self.path:return self.respond({'items':[],'metadata':{'continue':'another-page'}})
  return self.respond({'items':[]})
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path.endswith('/astra'):
   calls['astra']+=1;return self.respond({'error':'fixture unauthorized'},401)
  assert self.path=='/v1/chat/completions' and body['stream'] is False
  calls['local']+=1
  if len(body['messages'])>1:
   assert 'EmptyDir' in body['messages'][-1]['content'] and '1Gi' in body['messages'][-1]['content']
  answer='UNTRUSTED-PARTIAL-DRAFT' if calls['invalid'] else json.dumps({'hypotheses':[],'checks':[{'title':'Уточнить рост временных файлов','detail':'Сопоставьте назначение тома data и правила ротации.','expected':'Источник роста подтверждён логами приложения.','evidence_ids':['E01']}]},ensure_ascii=False)
  return self.respond({'choices':[{'message':{'content':answer},'finish_reason':'stop'}]})
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
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(a.base_url);page.locator('#cluster-select').select_option(cid);page.get_by_role('button',name='Диагностика: bad',exact=True).click()
    expect(page.locator('#scenario-result')).to_have_attribute('aria-busy','false',timeout=30000)
    expect(page.locator('#scenario-status')).to_contain_text('ИИ не завершил',timeout=30000)
    expect(page.locator('#scenario-result-body')).to_contain_text('EmptyDir')
    assert page.locator('#ai-dialog').is_visible() is False and not calls['local']
    assert not page.locator('#scenario-sources').evaluate('(e)=>e.open')
    page.locator('#scenario-sources>summary').click()
    expect(page.locator('#scenario-sources')).to_contain_text('Astra AI HTTP 401')
    expect(page.locator('#scenario-sources')).to_contain_text('неполные данные')
    assert calls['astra']==1 and not calls['local']
    # Deep profile additionally obtains container logs; 404 is visible as unavailable.
    page.locator('[data-scenario=deep]').click()
    expect(page.locator('#scenario-result')).to_have_attribute('aria-busy','false',timeout=30000)
    assert not page.locator('#scenario-sources').evaluate('(e)=>e.open')
    page.locator('#scenario-sources>summary').click()
    expect(page.locator('#scenario-sources')).to_contain_text('недоступно')
    expect(page.locator('#scenario-sources')).to_contain_text('Логи контейнера')
    assert calls['astra']==2 and not calls['local']
    page.locator('#scenario-provider').select_option('local')
    expect(page.locator('#scenario-result-body')).to_contain_text('Движок изменён')
    page.locator('[data-scenario=brief]').click()
    expect(page.locator('#scenario-result')).to_have_attribute('aria-busy','false',timeout=30000)
    expect(page.locator('#scenario-status')).not_to_contain_text('ИИ не завершил')
    expect(page.locator('#scenario-result-body')).to_contain_text('EmptyDir')
    assert calls['local']==1 and calls['astra']==2
    assert page.locator('#scenario-provider').input_value()=='local'
    assert not page.locator('#scenario-sources').evaluate('(e)=>e.open')
    page.locator('#scenario-sources>summary').click()
    expect(page.locator('#scenario-sources')).to_contain_text('Ответ ИИ завершён · формат проверен')
    # Invalid model prose cannot enter the primary report or silently change provider.
    calls['invalid']=True;page.locator('#scenario-rerun').click()
    expect(page.locator('#scenario-result')).to_have_attribute('aria-busy','false',timeout=30000)
    expect(page.locator('#scenario-status')).to_contain_text('ИИ не завершил')
    expect(page.locator('#scenario-result-body')).to_contain_text('EmptyDir')
    assert 'UNTRUSTED-PARTIAL-DRAFT' not in page.locator('#scenario-result').inner_text()
    page.locator('#scenario-sources>summary').click()
    expect(page.locator('#scenario-sources')).to_contain_text('Ответ ИИ не прошёл проверку формата')
    assert calls['local']==2 and calls['astra']==2
    rows=context.request.get(a.base_url+'/api/clusters').json()['items'];assert next(r for r in rows if r['id']==cid)['astra_ai']['enabled']
    page.screenshot(path='/tmp/pulse-0150-astra-local-scenarios.png',full_page=True)
    page.get_by_role('link',name='Обзор',exact=True).click()
    expect(page.locator('#scenario-result-body')).not_to_contain_text('EmptyDir')
    assert not errors,errors
    print('PASS: real API/TLS/browser; Astra 401 preserves EmptyDir facts, 404 logs and partial checks in closed sources; no silent fallback; explicit local scenario uses valid JSON; malformed model prose stays hidden; provider configuration unchanged; leaving clears context.')
   finally:
    context.request.delete(a.base_url+'/api/clusters/'+cid,headers=headers);context.request.put(a.base_url+'/api/settings',data={'llm':{'url':before['url'],'model':before['model']}},headers=headers);browser.close()
 finally:
  for server in (kube,model):server.shutdown();server.server_close()
