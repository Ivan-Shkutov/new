#!/usr/bin/env python3
"""Configure PeopleHub metrics via the local Pulse API; preserve other connections."""
import argparse
import base64
import json
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=Path(__file__).resolve().parents[1]/'runtime')
    parser.add_argument('--cluster',default='peoplehub')
    args=parser.parse_args()
    env=dict(line.split('=',1) for line in (args.runtime/'.env').read_text().splitlines() if line and not line.startswith('#') and '=' in line)
    port=int(env.get('PULSE_PORT','8090'));username=env.get('PULSE_USERNAME','pulse')
    password=(args.runtime/'secrets/portal-password').read_text().strip()
    headers={'Authorization':'Basic '+base64.b64encode((username+':'+password).encode()).decode(),'X-Pulse-Request':'1','Content-Type':'application/json'}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path,body=None):
        data=None if body is None else json.dumps(body).encode()
        req=urllib.request.Request(f'http://127.0.0.1:{port}/api'+path,data=data,headers=headers,method='GET' if body is None else 'PUT')
        with opener.open(req,timeout=20) as response:return json.load(response)
    metrics={'transport':'kubernetes','namespace':'monitoring','service':'kube-prom-kube-prometheus-prometheus','port':9090,'mode':'dedicated','labels':{},'url':''}
    rows=request('/clusters')['items']
    if not any(r['id']==args.cluster for r in rows):raise ValueError('Кластер не найден; проверьте --cluster')
    from urllib.parse import quote
    request('/clusters/'+quote(args.cluster,safe=''),{'metrics':metrics})
    row=next(r for r in request('/clusters')['items'] if r['id']==args.cluster)
    if row.get('metrics') is None or any(row['metrics'].get(k)!=v for k,v in metrics.items()):
        raise ValueError('API не подтвердил настройки метрик. Проверьте версию Pulse.')
    print('Настройки метрик подтверждены:',json.dumps(row['metrics'],ensure_ascii=False))
    print('Обновите браузер; в настройках кластера проверьте сохранённый источник.')


if __name__=='__main__':
    try:main()
    except urllib.error.HTTPError as exc:raise SystemExit('Pulse API HTTP '+str(exc.code)+'; настройки не подтверждены')
    except (OSError,ValueError,KeyError) as exc:raise SystemExit(str(exc))
