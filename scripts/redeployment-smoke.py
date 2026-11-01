#!/usr/bin/env python3
"""Disposable Docker upgrade/restore test, including real Caddy TLS and retained data.

Requires preloaded Pulse images for the current and previous sources and Caddy.
Uses generated test credentials, random loopback ports, and an isolated runtime.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pulse_app.build import identity


def run(*args):
    result=subprocess.run([str(x) for x in args],text=True,capture_output=True,timeout=120)
    if result.returncode:raise RuntimeError(f'Command failed: {args[:2]}\n{result.stderr[-2000:]}')
    return result.stdout


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-source',type=Path,required=True)
    a=parser.parse_args();old=a.previous_source.resolve()
    assert (old/'pulse').is_file(),'Previous source CLI missing'
    with tempfile.TemporaryDirectory(prefix='pulse-redeploy-') as directory:
        folder=Path(directory);runtime=folder/'runtime';port=free_port();tls_port=free_port()
        while tls_port==port:tls_port=free_port()
        name='pulse-redeploy-'+uuid.uuid4().hex[:12];volume=name+'-ca';started=False
        def cli(source,*args):return run(source/'pulse','--runtime',runtime,*args)
        run('docker','volume','create',volume)
        try:
            cli(old,'init','--port',port)
            password=(runtime/'secrets/portal-password').read_text().strip()
            key=hashlib.sha256((runtime/'registry.key').read_bytes()).hexdigest()
            headers={'Authorization':'Basic '+base64.b64encode(('pulse:'+password).encode()).decode(),
                     'Content-Type':'application/json','X-Pulse-Request':'1'}
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def api(path,body=None,https=False,method=None):
                base=f'https://localhost:{tls_port}' if https else f'http://127.0.0.1:{port}'
                req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
                with opener.open(req,timeout=15) as reply:return json.load(reply)
            started=True;cli(old,'start-offline')
            kube=json.dumps({'current-context':'read','contexts':[{'name':'read','context':{'cluster':'one','user':'reader'}}],
                'clusters':[{'name':'one','cluster':{'server':'https://localhost:1'}}],
                'users':[{'name':'reader','user':{'token':'fixture-reader'}}]})
            api('/api/clusters',{'id':'restore-test','name':'Restore test','enabled':False,'kubeconfig':kube})
            api('/api/settings',{'llm':{'url':'http://localhost:1/v1','model':'qwen','api_key':'fixture-key'}},method='PUT')
            with sqlite3.connect(runtime/'pulse.sqlite3') as conn:
                conn.execute("INSERT INTO investigations(id,cluster_id,question,status,answer,created_at) VALUES ('retained','restore-test','fixture question','done','fixture answer','2026-11-01')")
            config=runtime/'https/Caddyfile';config.parent.mkdir()
            config.write_text(f'{{\n admin off\n auto_https disable_redirects\n}}\nhttps://localhost:{tls_port} {{\n bind 127.0.0.1\n tls internal\n reverse_proxy 127.0.0.1:{port}\n}}\n');config.chmod(0o644)
            def caddy():
                run('docker','run','-d','--name',name,'--network=host','--read-only','--cap-drop=ALL','--cap-add=NET_BIND_SERVICE',
                    '--tmpfs','/config:rw,noexec,nosuid,size=16m','--tmpfs','/tmp:rw,noexec,nosuid,size=16m',
                    '--mount',f'type=volume,src={volume},dst=/data',
                    '--mount',f'type=bind,src={config},dst=/etc/caddy/Caddyfile,readonly',
                    'caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb')
            caddy()
            cert=folder/'root.crt'
            for _ in range(30):
                try:
                    run('docker','cp',name+':/data/caddy/pki/authorities/local/root.crt',cert);break
                except RuntimeError:time.sleep(.25)
            if not cert.exists():raise RuntimeError('Test CA not created: '+run('docker','logs',name)[-2000:])
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))
            assert api('/healthz',https=True)['version']!=identity()['version'],'Use an actual previous version'
            cli(old,'backup','--output-dir',folder/'backups');cli(old,'stop')
            archive=next((folder/'backups').glob('*.tar.gz'))
            cli(ROOT,'restore',archive,'--confirm-restore')
            assert hashlib.sha256((runtime/'registry.key').read_bytes()).hexdigest()==key
            assert (runtime/'secrets/portal-password').read_text().strip()==password
            # Recreate Caddy so the bind mount points to the restored inode.
            run('docker','rm','-f',name);caddy()
            cli(ROOT,'start-offline');cli(ROOT,'check')
            health=api('/healthz',https=True)
            assert all(health[k]==identity()[k] for k in ('version','build_id'))
            assert api('/api/clusters',https=True)['items'][0]['id']=='restore-test'
            assert api('/api/settings',https=True)['llm']['has_key']
            assert api('/api/investigations/retained',https=True)['answer']=='fixture answer'
            print('PASS: previous Docker release -> backup -> validated restore -> current exact build; encrypted connections, password/key, LLM and history retained; recreated Caddy reads restored config; trusted TLS and authenticated API succeed.')
        except Exception:
            logs=subprocess.run(['docker','logs','--tail','12',name],text=True,capture_output=True,timeout=10)
            print((logs.stdout+logs.stderr)[-2500:],file=sys.stderr)
            raise
        finally:
            if started:
                subprocess.run(['docker','compose',*['--env-file',str(runtime/'.env'),'--project-name',f'pulse-clusters-{port}',
                    '--project-directory',str(ROOT),'-f',str(ROOT/'compose.yaml')],'down'],env={**__import__('os').environ,'PULSE_RUNTIME_HOST':str(runtime)},capture_output=True,timeout=30)
            subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)
            subprocess.run(['docker','volume','rm',volume],capture_output=True,timeout=15)


if __name__=='__main__':main()
