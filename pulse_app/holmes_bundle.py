"""Generate a per-cluster, read-only HolmesGPT bundle. Never execute it here."""
import json
import os
from pathlib import Path
from .runbooks import RUNBOOKS

WRAPPER = '''import json,os,runpy
from pathlib import Path
from holmes.plugins.toolsets import load_builtin_toolsets
home=Path(os.environ["HOME"])/".holmes"
home.mkdir(parents=True,exist_ok=True)
config=json.loads(Path("/bundle/config.json").read_text())
# Disable every builtin provided by the installed image, including future ones.
config["toolsets"]={t.name:{"enabled":False} for t in load_builtin_toolsets()}
config["toolsets"]["skills"]={"enabled":True}
config["toolsets"]["pulse-readonly"]=json.loads(Path("/bundle/http-toolset.json").read_text())
(home/"config.yaml").write_text(json.dumps(config))
os.chmod(home/"config.yaml",0o600)
runpy.run_path("/app/server.py",run_name="__main__")
'''


def write_bundle(runtime,cid,portal_port,port,image,llm,agent_token,api_key):
    root=Path(runtime)/'holmes'/cid
    root.mkdir(parents=True,mode=0o700,exist_ok=False)
    os.chmod(root,0o700)
    def save(name,value):
        path=root/name
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))
    origin=f'http://127.0.0.1:{portal_port}'
    prefix=f'/agent/{cid}'
    save('config.json',{'model':'pulse-local','max_steps':6,'custom_skill_paths':['/bundle/skills'],'toolsets':{},'cluster_name':cid})
    save('http-toolset.json',{'type':'http','enabled':True,'description':'Pulse read-only tools for cluster '+cid,
         'llm_instructions':f'Only use GET {origin}{prefix}/snapshot (kind, namespace, name), /logs (namespace,pod,container,previous), /metrics (metric allowlist), /documents (q), /incidents. Never use another cluster or execute changes.',
         'config':{'timeout_seconds':15,'verify_ssl':True,'endpoints':[{'hosts':[origin],'paths':[prefix+'/'+p for p in ('snapshot','logs','metrics','documents','incidents','challenge')],'methods':['GET']}],
                   'default_headers':{'Authorization':'Bearer '+agent_token}}})
    save('model_list.json',{'pulse-local':{'model':'openai/'+llm['model'],'api_base':llm['url'].rstrip('/')+('' if llm['url'].rstrip('/').endswith('/v1') else '/v1'),'api_key':llm.get('api_key') or 'local-no-key','custom_args':{'max_tokens':1800,'temperature':0.1,'timeout':60}}})
    save('start.py',WRAPPER)
    save('.env','HOLMES_API_KEY='+api_key+'\n')
    skills=root/'skills';skills.mkdir(mode=0o700)
    for name,(title,_,instructions) in RUNBOOKS.items():
        folder=skills/name;folder.mkdir(mode=0o700)
        path=folder/'SKILL.md';path.write_text('---\nname: pulse-'+name+'\ndescription: '+title+'\n---\n\n'+instructions+'\n');path.chmod(0o600)
    # YAML accepts JSON; no PyYAML dependency is required on the host CLI.
    save('compose.yaml',{'services':{'holmes':{'image':image,'network_mode':'host','user':f'{os.getuid()}:{os.getgid()}',
        'entrypoint':['python','/bundle/start.py'],'working_dir':'/app','env_file':['.env'],
        'environment':{'HOLMES_HOST':'127.0.0.1','HOLMES_PORT':str(port),'HOME':'/tmp/pulse-holmes','MODEL_LIST_FILE_LOCATION':'/bundle/model_list.json','LOAD_CONFIG_FROM_ENV':'false','PYTHONDONTWRITEBYTECODE':'1'},
        'volumes':['.:/bundle:ro'],'read_only':True,'tmpfs':['/tmp:rw,nosuid,size=256m,mode=1777'],
        'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'restart':'unless-stopped',
        'healthcheck':{'test':['CMD','python','-c',f'import urllib.request;urllib.request.urlopen("http://127.0.0.1:{port}/healthz",timeout=5)'],'interval':'15s','timeout':'6s','retries':6}}}})
    return root
