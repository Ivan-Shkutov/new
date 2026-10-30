#!/usr/bin/env python3
"""Read-only local diagnostic. Prints no passwords, tokens, kubeconfig or log bodies."""
import argparse,base64,json,subprocess,sys,urllib.error,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pulse_app.build import identity
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime',type=Path);p.add_argument('--cluster',default='peoplehub');a=p.parse_args()
if a.runtime is None:
 value=subprocess.check_output(['docker','inspect','pulse-clusters-8090-pulse-1','--format','{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}'],text=True).strip()
 if not value:raise SystemExit('Runtime mount not found; specify --runtime')
 a.runtime=Path(value)
runtime=a.runtime.resolve()
env=dict(line.split('=',1) for line in (runtime/'.env').read_text().splitlines() if line and not line.startswith('#') and '=' in line)
base='http://127.0.0.1:'+env.get('PULSE_PORT','8090')
password=(runtime/'secrets/portal-password').read_text().strip()
headers={'Authorization':'Basic '+base64.b64encode((env.get('PULSE_USERNAME','pulse')+':'+password).encode()).decode(),'X-Pulse-Request':'1'}
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def api(path):
 try:
  with opener.open(urllib.request.Request(base+path,headers=headers),timeout=120) as r:return json.load(r)
 except urllib.error.HTTPError as e:return {'http_error':e.code}
health=api('/healthz');expected=identity();match=all(health.get(k)==expected[k] for k in ('version','build_id'))
print('Runtime:',runtime)
print('Expected code:',expected['version'],expected['build_id'])
print('Running code:',health.get('version'),health.get('build_id','not provided by this release'),'MATCH' if match else 'MISMATCH')
from urllib.parse import quote
cid=quote(a.cluster,safe='')
rows=api('/api/clusters').get('items',[]);row=next((r for r in rows if r['id']==a.cluster),None)
if not row:raise SystemExit('Cluster not found; check --cluster')
print('Cluster:',row['id'],'metrics_configured:',row['has_metrics'])
print('Saved source:',json.dumps(row.get('metrics',{}),ensure_ascii=False))
snapshot=api('/api/clusters/'+cid+'/snapshot');pods=snapshot.get('resources',{}).get('pods',{})
from collections import Counter
print('Snapshot:',snapshot.get('observed_at'),'pods:',len(pods.get('items',[])),'phases:',dict(Counter(x.get('status',{}).get('phase','Unknown') for x in pods.get('items',[]))),'list_error:',pods.get('error'),'truncated:',pods.get('truncated',False))
report=api('/api/clusters/'+cid+'/metrics-diagnostics')
if report.get('http_error')==404:
 report=api('/api/clusters/'+cid+'/dashboard-metrics')
 report={k:v for k,v in report.items() if k in ('configured','error','note','metrics')}
 report['metrics']={k:{key:v.get(key) for key in ('percent','observed_at','error','nodes_error')} for k,v in report.get('metrics',{}).items()}
print('Metrics:',json.dumps(report,ensure_ascii=False))
if not match or not report.get('ok'):raise SystemExit(1)
print('PASS: exact build; current CPU/RAM/root filesystem for all expected nodes')
