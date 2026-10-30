#!/usr/bin/env python3
"""Deploy optional local knowledge and domain HTTPS services; no SSH tunnel needed."""
import argparse,json,os,re,subprocess,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from importlib.machinery import SourceFileLoader
pulse=SourceFileLoader('pulse_cli',str(ROOT/'pulse')).load_module()
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--runtime',type=Path,default=ROOT/'runtime')
p.add_argument('command',choices=['knowledge-start','knowledge-model','knowledge-check','knowledge-stop','https-init','https-start','https-stop','https-ca'])
p.add_argument('--offline',action='store_true',help='Use verified loaded image tags, without pulling')
p.add_argument('--domain',default='pulse-clusters.plagate.ru')
p.add_argument('--tls',choices=['internal','public'],default='internal')
a=p.parse_args();runtime=a.runtime.resolve();env=pulse.load_env(runtime)
if not env:raise SystemExit('Run ./pulse init first')
port=env.get('PULSE_PORT','8090');group='knowledge' if a.command.startswith('knowledge') else 'https'
command=['docker','compose','--env-file',str(runtime/'.env'),'--project-name',f'pulse-{group}-{port}','--project-directory',str(ROOT),'-f',str(ROOT/'deploy'/f'compose.{group}.yaml')]
if a.offline:command+=['-f',str(ROOT/'deploy'/f'compose.{group}.offline.yaml')]
process_env=dict(os.environ,**env,PULSE_RUNTIME_HOST=str(runtime))
def compose(*args,**kw):return subprocess.run(command+list(args),env=process_env,check=True,**kw)
if a.command=='knowledge-start':
    additions={'PULSE_QDRANT_URL':'http://127.0.0.1:16333','PULSE_EMBEDDINGS_URL':'http://127.0.0.1:11435','PULSE_EMBEDDINGS_MODEL':'bge-m3','PULSE_EMBEDDINGS_PROVIDER':'ollama','PULSE_EMBEDDINGS_DIMENSION':'1024'}
    with (runtime/'.env').open('a') as f:
        for key,value in additions.items():
            if key not in env:f.write(f'{key}={value}\n')
    compose('up','-d')
    for url in ((env.get('PULSE_QDRANT_URL') or 'http://127.0.0.1:16333')+'/readyz',(env.get('PULSE_EMBEDDINGS_URL') or 'http://127.0.0.1:11435')+'/api/version'):
        ready=False
        for _ in range(45):
            try:
                with urllib.request.urlopen(url,timeout=2) as response:ready=response.status==200
                if ready:break
            except OSError:pass
            time.sleep(1)
        if not ready:raise SystemExit('Service did not become ready; inspect docker compose logs before continuing.')
    print('Services started. Next: knowledge-model, knowledge-check, then ./pulse start-offline to apply environment.')
elif a.command=='knowledge-model':
    model=env.get('PULSE_EMBEDDINGS_MODEL','bge-m3')
    if not a.offline:compose('exec','-T','embeddings','ollama','pull',model)
    with urllib.request.urlopen(env.get('PULSE_EMBEDDINGS_URL','http://127.0.0.1:11435')+'/api/tags',timeout=15) as r:tags=json.load(r)
    name=model if ':' in model else model+':latest'
    digest=next((row['digest'] for row in tags['models'] if row['name']==name),None)
    if not digest:raise SystemExit('Model digest unavailable')
    lines=[line for line in (runtime/'.env').read_text().splitlines() if not line.startswith('PULSE_EMBEDDINGS_REVISION=')]
    (runtime/'.env').write_text('\n'.join(lines)+'\nPULSE_EMBEDDINGS_REVISION='+digest+'\n')
    print('Embedding revision pinned. Run ./pulse start-offline before indexing.')
elif a.command=='knowledge-check':
    def get(url):
        with urllib.request.urlopen(url,timeout=10) as r:return r.read()
    get(env.get('PULSE_QDRANT_URL','http://127.0.0.1:16333')+'/readyz')
    tags=json.loads(get(env.get('PULSE_EMBEDDINGS_URL','http://127.0.0.1:11435')+'/api/tags'))
    model=env.get('PULSE_EMBEDDINGS_MODEL','bge-m3')
    assert any(r['name'].split(':')[0]==model.split(':')[0] for r in tags['models']),'Embedding model is not loaded'
    print('Qdrant ready; embedding model present. First real search also checks dimensions and embedding API.')
elif a.command.endswith('-stop'):compose('stop')
elif a.command=='https-init':
    if not re.fullmatch(r'(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}',a.domain):raise SystemExit('Invalid domain')
    folder=runtime/'https';folder.mkdir(mode=0o700,exist_ok=True)
    config=folder/'Caddyfile'
    if config.exists():raise SystemExit('Caddyfile exists; review/edit the existing file instead of overwriting it.')
    tls='\n    tls internal' if a.tls=='internal' else ''
    config.write_text(f'''{{
    admin off
}}
{a.domain} {{{tls}
    encode gzip
    header Strict-Transport-Security "max-age=31536000"
    reverse_proxy 127.0.0.1:{port}
}}
''')
    config.chmod(0o644)
    print(f'Created {config}. DNS and access to TCP 443 must be configured before https-start. Internal CA must be trusted on clients.')
elif a.command=='https-start':
    if not (runtime/'https/Caddyfile').is_file():raise SystemExit('Run https-init first')
    compose('run','--rm','--no-deps','https','caddy','validate','--config','/etc/caddy/Caddyfile')
    compose('up','-d')
elif a.command=='https-ca':
    destination=runtime/'https/pulse-root.crt'
    compose('cp','https:/data/caddy/pki/authorities/local/root.crt',str(destination))
    print('Public CA certificate: '+str(destination))
