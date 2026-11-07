#!/usr/bin/env python3
"""Verify a real incident through Pulse's actual backend, not a synthetic 2+2 call.

Uses one temporary session, performs read-only collection, and deletes its
conversation. Report contains the model answer and safe traces, never keys.
"""
import argparse
import base64
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--cluster',required=True)
    p.add_argument('--problem-id',required=True)
    p.add_argument('--engine',choices=['local','cluster'],default='local')
    p.add_argument('--output',type=Path,default=Path('ai-integration-report.json'))
    a=p.parse_args()
    env=dict(line.split('=',1) for line in (a.runtime/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    origin='http://127.0.0.1:'+env.get('PULSE_PORT','8090')
    password=(a.runtime/'secrets/portal-password').read_text().strip()
    auth=base64.b64encode((env.get('PULSE_USERNAME','pulse')+':'+password).encode()).decode()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def api(method,path,body=None):
        request=urllib.request.Request(origin+'/api'+path,method=method,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Basic '+auth,'Content-Type':'application/json','X-Pulse-Request':'1'})
        with opener.open(request,timeout=30) as response:return json.load(response)
    sid=None
    try:
        sid=api('POST','/sessions',{'cluster_id':a.cluster,'engine':a.engine,'mode':'problem'})['id']
        job=api('POST',f'/sessions/{sid}/messages',{'problem_id':a.problem_id,'mode':'extended','question':'Разбери инцидент: что произошло, анализ, причины, исправление, последствия и профилактика. Дай краткие выводы по фактам и документации.'})
        jid=job['id'];deadline=time.monotonic()+1200
        while time.monotonic()<deadline:
            job=api('GET',f'/sessions/{sid}/jobs/{jid}')
            if job['status'] not in ('queued','running'):break
            time.sleep(2)
        else:raise TimeoutError('Pulse не завершил анализ за 20 минут')
        trace=json.loads(job.get('trace') or '[]')
        # Do not export raw source data or excerpts in this diagnostic report.
        trace=[{k:v for k,v in row.items() if k!='documents'} for row in trace]
        report={'status':job['status'],'run_id':jid,'engine':a.engine,'error':job.get('error',''),'answer':job.get('answer',''),'trace':trace,'semantic_quality_verified':False}
        a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2));a.output.chmod(0o600)
        print('Status:',report['status'],'| Run:',jid,'| Error:',report['error'] or 'none')
        print('Report:',a.output)
        return 0 if report['status']=='done' else 1
    finally:
        if sid:
            try:api('DELETE','/sessions/'+sid)
            except Exception:print('Temporary session cleanup could not be confirmed; it will expire.')


if __name__=='__main__':raise SystemExit(main())
