#!/usr/bin/env python3
"""Disposable Pulse only: real TLS Kubernetes/proxy fixture, persistence and reported chat case."""
import argparse,base64,datetime,ipaddress,json,ssl,subprocess,sys,tempfile,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pulse_app.connectors import RESOURCE_PATHS,utilization_expressions
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-url',default='http://127.0.0.1:18090');p.add_argument('--password-file',type=Path,required=True);p.add_argument('--screenshots',type=Path,default=Path('/tmp/pulse-0140-shots'));a=p.parse_args();a.screenshots.mkdir(parents=True,exist_ok=True)
source={'transport':'kubernetes','namespace':'monitoring','service':'kube-prom-kube-prometheus-prometheus','port':9090,'mode':'dedicated','labels':{},'url':''}
expressions=utilization_expressions(source);node_expressions=utilization_expressions(source,True)
resources={k:{'items':[]} for k in RESOURCE_PATHS};resources['namespaces']['items']=[{'metadata':{'name':'ns-'+str(i)}} for i in range(14)]
for i in range(6):resources['nodes']['items'].append({'metadata':{'name':'test-node-'+str(i+1),'uid':'node-uid-'+str(i)},'status':{'capacity':{'cpu':'4','memory':'16Gi'},'allocatable':{'cpu':'4','memory':'16Gi'},'addresses':[{'type':'InternalIP','address':'10.0.0.'+str(i+1)}],'conditions':[{'type':'Ready','status':'True','message':'large node payload '*2000}],'nodeInfo':{'kubeletVersion':'v1.30.14'}}})
for i in range(163):
 status={'phase':'Running','conditions':[{'type':'Ready','status':'True'}],'containerStatuses':[{'name':'app','state':{'running':{}},'restartCount':0}]}
 if 145<=i<162:status={'phase':'Succeeded','containerStatuses':[{'name':'app','state':{'terminated':{'exitCode':0,'reason':'Completed'}}}]}
 if i==162:status={'phase':'Failed','reason':'Evicted','message':'Usage of EmptyDir volume data exceeds the limit 1Gi.'}
 resources['pods']['items'].append({'metadata':{'name':'test-pod-'+str(i),'namespace':'am-agent' if i==162 else 'work','uid':'pod-uid-'+str(i)},'spec':{'nodeName':'test-node-'+str(i%6+1),'containers':[{'name':'app'}]},'status':status})
resources['services']['items']=[{'metadata':{'namespace':source['namespace'],'name':source['service']},'spec':{'ports':[{'port':9090}]}}]
state={'deny_proxy':True,'requests':[],'model_calls':[]}
class Kubernetes(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def respond(self,code,value):
  data=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  u=urlsplit(self.path);q=parse_qs(u.query);state['requests'].append((u.path,q))
  if self.headers.get('Authorization')!='Bearer test-reader':return self.respond(401,{})
  if u.path=='/version':return self.respond(200,{'gitVersion':'v1.30.8'})
  if '/proxy/' in u.path:
   if state['deny_proxy']:return self.respond(403,{})
   query=q['query'][0];now=time.time()
   key=next((k for k in expressions if query in (expressions[k],node_expressions[k])),None)
   if key is None:return self.respond(400,{})
   percent={'cpu':34,'ram':64,'disk':72}[key]
   if u.path.endswith('query_range'):data={'resultType':'matrix','result':[{'values':[[now-60,percent],[now,percent]]}]}
   else:
    rows=[{'value':[now,percent]}] if query==expressions[key] else [{'metric':{'instance':f'10.0.0.{i}:9100'},'value':[now,percent+i-3]} for i in range(1,7)]
    data={'resultType':'vector','result':rows}
   return self.respond(200,{'status':'success','data':data})
  kind=next((k for k,path in RESOURCE_PATHS.items() if path==u.path),None)
  if kind:
   data=resources[kind]
   if kind=='pods':
    second=q.get('continue')==['next'];data={'items':data['items'][100:] if second else data['items'][:100],'metadata':{'continue':'' if second else 'next','resourceVersion':'fixture-123'}}
   return self.respond(200,data)
  return self.respond(404,{})
class Model(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));state['model_calls'].append(body)
  text=body['messages'][-1]['content'];raw=text.split('Доказательства и выполненные проверки: ',1)[1].split('\nСтатус поиска документации:',1)[0]
  parsed=json.loads(raw);pods=parsed['resources']['pods']
  assert pods['count']==163 and pods['unhealthy']==1 and any(x['reason']=='Evicted' for x in pods['items'])
  data=json.dumps({'choices':[{'message':{'content':'Структурированный контекст содержит все счётчики и Evicted Pod; это тестовый ответ модели.'},'finish_reason':'stop'}]},ensure_ascii=False).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
folder=Path(tempfile.mkdtemp(prefix='pulse-regression-tls-'));key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Pulse fixture')]);now=datetime.datetime.now(datetime.timezone.utc)
cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),True).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False).sign(key,hashes.SHA256());pem=cert.public_bytes(serialization.Encoding.PEM)
(folder/'ca.pem').write_bytes(pem);(folder/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));(folder/'key.pem').chmod(0o600)
kube=ThreadingHTTPServer(('127.0.0.1',0),Kubernetes);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(folder/'ca.pem',folder/'key.pem');kube.socket=ctx.wrap_socket(kube.socket,server_side=True)
model=ThreadingHTTPServer(('127.0.0.1',0),Model);threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (kube,model)]
for thread in threads:thread.start()
kubeconfig={'apiVersion':'v1','kind':'Config','current-context':'test','contexts':[{'name':'test','context':{'cluster':'test','user':'reader'}}],'clusters':[{'name':'test','cluster':{'server':f'https://127.0.0.1:{kube.server_port}','certificate-authority-data':base64.b64encode(pem).decode()}}],'users':[{'name':'reader','user':{'token':'test-reader'}}]};cid='regression-'+uuid.uuid4().hex[:8];headers={'X-Pulse-Request':'1'}
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);context=browser.new_context(http_credentials={'username':'pulse','password':a.password_file.read_text().strip()},viewport={'width':1366,'height':900});before=context.request.get(a.base_url+'/api/settings').json();assert not before['llm']['has_key'],'Disposable runtime only'
 try:
  assert context.request.post(a.base_url+'/api/clusters',data={'id':cid,'name':'PeopleHub (TLS-стенд)','enabled':True,'kubeconfig':json.dumps(kubeconfig)},headers=headers).status==201
  assert context.request.post(a.base_url+f'/api/clusters/{cid}/refresh',headers=headers).ok
  page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(a.base_url);page.locator('#cluster-select').select_option(cid)
  page.get_by_role('button',name='Подключить метрики',exact=True).click();source_select=page.get_by_label('Источник метрик в кластере');expect(source_select).to_contain_text(source['service']);source_select.select_option('0');page.get_by_role('button',name='Проверить и сохранить',exact=True).click()
  expect(page.locator('.metric-probe-report')).to_contain_text('services/proxy',timeout=30000)
  row=next(x for x in context.request.get(a.base_url+'/api/clusters').json()['items'] if x['id']==cid);assert not row['has_metrics']
  state['deny_proxy']=False;page.get_by_role('button',name='Проверить и сохранить',exact=True).click();expect(page.locator('#dashboard-detail')).not_to_be_visible(timeout=30000)
  for k,v in [('cpu','34%'),('ram','64%'),('disk','72%')]:expect(page.locator('[data-testid=utilization-'+k+'] .donut-number')).to_have_text(v,timeout=30000)
  page.reload();expect(page.locator('[data-testid=utilization-cpu] .donut-number')).to_have_text('34%');page.screenshot(path=str(a.screenshots/'metrics-restored.png'),full_page=True)
  page.locator('#navigation a[data-page=chat]').click();expect(page.locator('#ai-form button[type=submit]')).to_be_enabled();question='выведи все проблемные pods, все что не в статусе Running';page.locator('#ai-form textarea').fill(question);page.locator('#ai-form button[type=submit]').click();expect(page.locator('.chat-answer')).to_contain_text('test-pod-162',timeout=15000);expect(page.locator('.chat-answer')).to_contain_text('Pods не в фазе Running: 18');assert page.locator('.chat-answer tbody tr').count()==18;assert not state['model_calls'];page.screenshot(path=str(a.screenshots/'all-non-running-pods.png'),full_page=True)
  assert context.request.put(a.base_url+'/api/settings',data={'llm':{'url':f'http://127.0.0.1:{model.server_port}/v1','model':'fixture'}},headers=headers).ok
  page.locator('#ai-form textarea').fill('Расскажи о состоянии workloads');page.locator('#ai-form button[type=submit]').click();expect(page.locator('.chat-answer').last).to_contain_text('Структурированный контекст',timeout=15000);assert len(state['model_calls'])==1
  page.get_by_role('button',name='Закрыть анализ',exact=True).click();assert not errors,errors
  report=context.request.get(a.base_url+f'/api/clusters/{cid}/metrics-diagnostics').json();assert report['ok'] and all(x['covered']==6 for x in report['metrics'].values())
  runtime=a.password_file.resolve().parents[1]
  helper=subprocess.run([sys.executable,str(ROOT/'scripts/configure-peoplehub-metrics.py'),'--runtime',str(runtime),'--cluster',cid],capture_output=True,text=True)
  assert helper.returncode==0,helper.stdout+helper.stderr
  doctor=subprocess.run([str(ROOT/'pulse'),'--runtime',str(runtime),'doctor','--cluster',cid],capture_output=True,text=True)
  assert doctor.returncode==0 and 'PASS:' in doctor.stdout,doctor.stdout+doctor.stderr
  print('PASS: CLI repair helper verifies and persists all three metrics; ./pulse doctor verifies exact build and live metrics without exposing credentials.')
  print('PASS: real browser + Pulse Docker/API + verified TLS Kubernetes + real service proxy path; 403 does not save; CPU/RAM/disk 6/6 verified and persist/reload; exact reported question lists all 18 non-Running Pods of 163 (17 Completed, 1 Evicted); no model needed for inventory; large JSON context includes Pods before LLM.')
 finally:
  context.request.delete(a.base_url+'/api/clusters/'+cid,headers=headers)
  old=before['llm']
  if old.get('url') and old.get('model'):context.request.put(a.base_url+'/api/settings',data={'llm':{'url':old['url'],'model':old['model']}},headers=headers)
  browser.close()
for server in (kube,model):server.shutdown()
for thread in threads:thread.join()
