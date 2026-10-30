#!/usr/bin/env python3
"""Configure PeopleHub metrics via the local Pulse API; preserve other connections."""
import argparse
import base64
import json
import sys
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
    def request(path,body=None,method=None):
        data=None if body is None else json.dumps(body).encode()
        req=urllib.request.Request(f'http://127.0.0.1:{port}/api'+path,data=data,headers=headers,method=method or ('GET' if body is None else 'PUT'))
        with opener.open(req,timeout=120) as response:return json.load(response)
    metrics={'transport':'kubernetes','namespace':'monitoring','service':'kube-prom-kube-prometheus-prometheus','port':9090,'mode':'dedicated','labels':{},'url':''}
    rows=request('/clusters')['items']
    if not any(r['id']==args.cluster for r in rows):raise ValueError('Кластер не найден; проверьте --cluster')
    from urllib.parse import quote
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from pulse_app.build import identity
    health=request('/version');expected=identity()
    if any(health.get(k)!=expected[k] for k in ('version','build_id')):raise ValueError('Запущена другая сборка Pulse. Сначала обновите и выполните ./pulse check.')
    result=request('/clusters/'+quote(args.cluster,safe='')+'/metrics-connect',{'source':metrics},'POST')
    if not result.get('saved'):
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise ValueError('Источник НЕ сохранён: '+result.get('action','проверка не пройдена'))
    row=next(r for r in request('/clusters')['items'] if r['id']==args.cluster)
    if row.get('metrics') is None or any(row['metrics'].get(k)!=v for k,v in metrics.items()):
        raise ValueError('API не подтвердил настройки метрик. Проверьте версию Pulse.')
    print('Настройки метрик подтверждены:',json.dumps(row['metrics'],ensure_ascii=False))
    print('Сборка:',health['version'],health['build_id'])
    for key,value in result['metrics'].items():print(key,value['percent'],'%',str(value['covered'])+'/'+str(value['expected']),'узлов')
    print('CPU, RAM и диски проверены. Обновите Обзор.')


if __name__=='__main__':
    try:main()
    except urllib.error.HTTPError as exc:raise SystemExit('Pulse API HTTP '+str(exc.code)+'; настройки не подтверждены')
    except (OSError,ValueError,KeyError) as exc:raise SystemExit(str(exc))
