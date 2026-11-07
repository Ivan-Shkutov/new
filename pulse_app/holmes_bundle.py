"""Generate a per-cluster, read-only HolmesGPT bundle. Never execute it here."""
import json
import os
import tempfile
import shutil
import time
from pathlib import Path
from .runbooks import RUNBOOKS

AGENT_STEPS = 8
AGENT_TOKENS = 4096
AGENT_TIMEOUT = 180


def write_skills(root):
    skills=root/'skills';skills.mkdir(mode=0o700,exist_ok=True)
    workflow=Path(__file__).with_name('ai_rules').joinpath('INVESTIGATION.md').read_text()
    rows={**RUNBOOKS,'investigation':('Полный процесс расследования',(),workflow)}
    for name,(title,_,instructions) in rows.items():
        folder=skills/name;folder.mkdir(mode=0o700,exist_ok=True)
        path=folder/'SKILL.md'
        path.write_text('---\nname: pulse-'+name+'\ndescription: '+title+'\n---\n\n'+instructions+'\n')
        path.chmod(0o600)

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


def write_bundle(runtime,cid,portal_port,port,image,llm,agent_token,api_key,*,max_steps=AGENT_STEPS,max_tokens=AGENT_TOKENS,timeout_seconds=AGENT_TIMEOUT):
    from .ai_limits import validate
    validate(dict(max_steps=max_steps,max_tokens=max_tokens,timeout_seconds=timeout_seconds))
    root=Path(runtime)/'holmes'/cid
    root.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    if root.exists():raise FileExistsError('Holmes bundle already exists')
    with tempfile.TemporaryDirectory(prefix='.'+cid+'-',dir=root.parent) as staging:
        pending=Path(staging)/'bundle'
        _write_bundle(pending,cid,portal_port,port,image,llm,agent_token,api_key,max_steps,max_tokens,timeout_seconds)
        pending.rename(root)
    return root


def _write_bundle(root,cid,portal_port,port,image,llm,agent_token,api_key,max_steps,max_tokens,timeout_seconds):
    root.mkdir(parents=True,mode=0o700,exist_ok=False)
    os.chmod(root,0o700)
    def save(name,value):
        path=root/name
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))
    origin=f'http://127.0.0.1:{portal_port}'
    prefix=f'/agent/{cid}'
    save('config.json',{'model':'pulse-local','max_steps':max_steps,'custom_skill_paths':['/bundle/skills'],'toolsets':{},'cluster_name':cid})
    save('http-toolset.json',{'type':'http','enabled':True,'description':'Pulse read-only tools for cluster '+cid,
         'llm_instructions':f'First read the investigation skill. Reuse supplied evidence before calling tools. Only use GET {origin}{prefix}/diagnostics (namespace,pod,uid) for live evidence, /snapshot (kind, namespace, name), /logs (namespace,pod,container,previous), /astra-logs (namespace,pod,start,end), /metrics (metric allowlist), /documents (q), /incidents. Search /documents for the exact symptom and product version before the final plan. Do not repeat a tool call without a new reason. Stop when evidence suffices or a source is unavailable; report gaps. Cite actual evidence and its UID/time. Never use another cluster or execute changes.',
         'config':{'timeout_seconds':120,'verify_ssl':True,'endpoints':[{'hosts':[origin],'paths':[prefix+'/'+p for p in ('snapshot','logs','metrics','documents','incidents','challenge','diagnostics','astra-logs')],'methods':['GET']}],
                   'default_headers':{'Authorization':'Bearer '+agent_token}}})
    save('model_list.json',{'pulse-local':{'model':'openai/'+llm['model'],'api_base':llm['url'].rstrip('/')+('' if llm['url'].rstrip('/').endswith('/v1') else '/v1'),'api_key':llm.get('api_key') or 'local-no-key','custom_args':{'max_tokens':max_tokens,'temperature':0.1,'timeout':timeout_seconds}}})
    save('start.py',WRAPPER)
    save('.env','HOLMES_API_KEY='+api_key+'\n')
    write_skills(root)
    # YAML accepts JSON; no PyYAML dependency is required on the host CLI.
    save('compose.yaml',{'services':{'holmes':{'image':image,'network_mode':'host','user':f'{os.getuid()}:{os.getgid()}',
        'entrypoint':['python','/bundle/start.py'],'working_dir':'/app','env_file':['.env'],
        'environment':{'HOLMES_HOST':'127.0.0.1','HOLMES_PORT':str(port),'HOME':'/tmp/pulse-holmes','MODEL_LIST_FILE_LOCATION':'/bundle/model_list.json','LOAD_CONFIG_FROM_ENV':'false','PYTHONDONTWRITEBYTECODE':'1'},
        'volumes':['.:/bundle:ro'],'read_only':True,'tmpfs':['/tmp:rw,nosuid,size=256m,mode=1777'],
        'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'restart':'unless-stopped',
        'healthcheck':{'test':['CMD','python','-c',f'import urllib.request;urllib.request.urlopen("http://127.0.0.1:{port}/healthz",timeout=5)'],'interval':'15s','timeout':'6s','retries':6}}}})
    return root


def refresh_bundle(runtime, cid, *, max_steps=AGENT_STEPS, max_tokens=AGENT_TOKENS, timeout_seconds=AGENT_TIMEOUT):
    """Update owned rules/budgets only. Preserve model credentials, tools and Compose."""
    from .ai_limits import validate
    validate(dict(max_steps=max_steps,max_tokens=max_tokens,timeout_seconds=timeout_seconds))
    root=Path(runtime)/'holmes'/cid
    if not root.is_dir() or root.is_symlink():raise ValueError('Сначала подготовьте конфигурацию Holmes через Pulse')
    if any(path.is_symlink() for path in root.rglob('*')):raise ValueError('Bundle содержит неподдерживаемые ссылки')
    config_path=root/'config.json';model_path=root/'model_list.json';tool_path=root/'http-toolset.json'
    config=json.loads(config_path.read_text());models=json.loads(model_path.read_text())
    tool=json.loads(tool_path.read_text())
    if config.get('model')!='pulse-local' or not isinstance(models.get('pulse-local'),dict):
        raise ValueError('Этот bundle не соответствует конфигурации Pulse')
    backup=root.parent/(cid+'-before-refresh-'+str(time.time_ns()))
    shutil.copytree(root,backup,symlinks=True)
    backup.chmod(0o700)
    for path in backup.rglob('*'):path.chmod(0o700 if path.is_dir() else 0o600)
    try:
        config['max_steps']=max_steps
        args=models['pulse-local'].setdefault('custom_args',{})
        args.update(max_tokens=max_tokens,timeout=timeout_seconds,temperature=0.1)
        old_instructions=tool.get('llm_instructions','')
        if not isinstance(old_instructions,str):raise ValueError('Неверный HTTP toolset')
        if 'First read the investigation skill.' not in old_instructions:
            tool['llm_instructions']='First read the investigation skill. Reuse supplied evidence; search /documents before the final plan. Do not repeat calls without a new reason; stop when evidence suffices or access/time is exhausted. '+old_instructions
        for path,value in ((config_path,config),(model_path,models),(tool_path,tool)):
            temporary=path.with_suffix('.tmp')
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)
            temporary.replace(path)
        write_skills(root)
    except Exception:
        for name in ('config.json','model_list.json','http-toolset.json'):
            shutil.copy2(backup/name,root/name)
        if (backup/'skills').is_dir():
            shutil.rmtree(root/'skills',ignore_errors=True)
            shutil.copytree(backup/'skills',root/'skills')
        for path in root.glob('*.tmp'):path.unlink()
        raise
    return backup
