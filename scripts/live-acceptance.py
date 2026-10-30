#!/usr/bin/env python3
"""Check a deployed Pulse through verified HTTPS; optionally run a read-only AI check."""
import argparse,base64,json,ssl,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',required=True);p.add_argument('--ca-file',type=Path);p.add_argument('--password-file',type=Path,required=True);p.add_argument('--username',default='pulse');p.add_argument('--cluster',default='');p.add_argument('--question');p.add_argument('--problem-id');a=p.parse_args()
u=urllib.parse.urlsplit(a.url)
if u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','::1')):p.error('HTTPS required outside loopback')
ctx=ssl.create_default_context(cafile=str(a.ca_file) if a.ca_file else None)
headers={'Authorization':'Basic '+base64.b64encode((a.username+':'+a.password_file.read_text().strip()).encode()).decode(),'X-Pulse-Request':'1','Content-Type':'application/json','Origin':a.url.rstrip('/')}
def api(method,path,body=None):
 req=urllib.request.Request(a.url.rstrip('/')+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers=headers)
 with urllib.request.urlopen(req,context=ctx,timeout=30) as r:return json.load(r)
health=api('GET','/healthz');assert health['status']=='ok';print('Pulse',health['version'],'HTTPS/API OK')
clusters=api('GET','/api/clusters')['items'];print('Clusters:',len(clusters))
kb=api('GET','/api/knowledge/status');print('Qdrant:',kb.get('qdrant','unconfigured'),'model:',kb['model'],'catalogue:',kb['documents'])
if kb.get('qdrant')=='ready':
 hits=api('GET','/api/knowledge/search?'+urllib.parse.urlencode({'q':'Evicted EmptyDir sizeLimit','cluster_id':a.cluster}))
 print('Knowledge hits:',len(hits['items']),'sources:',[r.get('source') for r in hits['items'][:2]])
if a.question:
 sid=api('POST','/api/sessions',{'cluster_id':a.cluster,'mode':'problem' if a.problem_id else 'chat','engine':'cluster' if a.cluster else 'local'})['id']
 try:
  body={'question':a.question}
  if a.problem_id:body['problem_id']=a.problem_id
  jid=api('POST',f'/api/sessions/{sid}/messages',body)['id'];deadline=time.monotonic()+420
  while time.monotonic()<deadline:
   result=api('GET',f'/api/sessions/{sid}/jobs/{jid}')
   if result['status'] not in ('queued','running'):break
   time.sleep(2)
  assert result['status']=='done',result.get('error') or result['status']
  print('AI response complete. Trace:')
  for row in json.loads(result.get('trace') or '[]'):print(row['tool'],row['status'])
  report=json.loads(result.get('evidence') or '{}');print('Evidence checks:',len(report.get('records',[])),'unavailable:',report.get('unavailable',0))
  print(result['answer'])
 finally:api('DELETE','/api/sessions/'+sid)
 print('Temporary conversation removed.')
