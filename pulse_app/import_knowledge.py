"""Admin-only import tool: local JSONL/directory or the explicitly scoped VK docs.

Run on the Pulse server, or inside its image. Not callable by the language model.
"""
import argparse
import base64
from collections import deque
import gzip
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


class Page(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.text=[];self.skip=0;self.article=0;self.content=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style','nav','footer'):self.skip+=1
        if tag in ('main','article'):self.article+=1
        if tag=='a' and attrs.get('href'):self.links.append(attrs['href'])
        if tag in ('p','h1','h2','h3','li','pre','br','tr'):
            self.text.append('\n');self.content.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','nav','footer'):self.skip=max(0,self.skip-1)
        if tag in ('main','article'):self.article=max(0,self.article-1)
    def handle_data(self,data):
        if not self.skip:
            self.text.append(data)
            if self.article:self.content.append(data)
    def body(self):return '\n'.join(line.strip() for line in ''.join(self.content or self.text).splitlines() if line.strip())


def crawl(root, product, version, maximum=3000):
    """HTTPS, exact official host and path subtree; bounded and never submits forms."""
    parsed=urllib.parse.urlsplit(root);pending=deque([root]);seen=set();failures=[];skipped=[];documents=[]
    prefix=parsed.path.rstrip('/')
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            dest=urllib.parse.urlsplit(newurl)
            if dest.scheme!='https' or dest.netloc!=parsed.netloc or not (dest.path.rstrip('/')==prefix or dest.path.startswith(prefix+'/')):
                raise ValueError('Redirect outside documentation scope')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    opener=urllib.request.build_opener(Redirect())
    while pending and len(seen)<maximum:
        url=pending.popleft()
        if url in seen:continue
        seen.add(url)
        if len(seen)%25==0:print(f'VK docs: visited {len(seen)}, captured {len(documents)}, failures {len(failures)}',flush=True)
        try:
            with opener.open(urllib.request.Request(url,headers={'User-Agent':'PulseDocumentationImporter/0.13'}),timeout=30) as r:
                if 'text/html' not in r.headers.get('Content-Type',''):
                    skipped.append({'url':url,'reason':'Non-HTML: import an official export separately'});continue
                raw=r.read(4_000_001)
                if len(raw)>4_000_000:raise ValueError('Page exceeds 4 MB')
                page=Page();page.feed(raw.decode('utf-8'))
            text=page.body()
            if len(text)<150:raise ValueError('No usable server-rendered text; use an official export instead')
            documents.append(dict(name=urllib.parse.urlsplit(url).path,source=url,product=product,version=version,language='ru',cluster_id='',body=text))
            for link in page.links:
                u=urllib.parse.urlsplit(urllib.parse.urljoin(url,link))
                if u.scheme=='https' and u.netloc==parsed.netloc and not u.query and (u.path.rstrip('/')==prefix or u.path.startswith(prefix+'/')):
                    clean=urllib.parse.urlunsplit((u.scheme,u.netloc,u.path,'',''))
                    if clean not in seen:pending.append(clean)
        except (urllib.error.URLError,ValueError,UnicodeError) as e:failures.append({'url':url,'error':str(e)[:160]})
        time.sleep(.15)
    return documents,{'root':root,'visited':len(seen),'documents':len(documents),'failures':failures,'skipped':skipped,'limit_reached':bool(pending),'complete_discovery':not failures and not skipped and not pending,'note':'Only linked pages inside this subtree. Hidden/JS-only/unlinked pages require an official export.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--jsonl',type=Path)
    source.add_argument('--directory',type=Path)
    source.add_argument('--vk',choices=['on-premises','saas'])
    source.add_argument('--reindex',action='store_true')
    p.add_argument('--version',default='unconfirmed')
    p.add_argument('--product',default='local')
    p.add_argument('--source',default='local-export')
    p.add_argument('--cluster',default='')
    p.add_argument('--base-url',default='http://127.0.0.1:8090')
    p.add_argument('--password-file',type=Path,default=Path(os.getenv('PULSE_PASSWORD_FILE','runtime/secrets/portal-password')))
    p.add_argument('--username',default=os.getenv('PULSE_USERNAME','pulse'))
    p.add_argument('--output',type=Path,help='Export downloaded documentation to JSONL instead of indexing')
    p.add_argument('--report',type=Path,default=Path('import-report.json'))
    a=p.parse_args()
    origin=urllib.parse.urlsplit(a.base_url)
    if origin.scheme!='https' and not (origin.scheme=='http' and origin.hostname in ('127.0.0.1','localhost','::1')):p.error('Use HTTPS or loopback for authenticated API calls')
    auth=None
    def api(method,path,body=None):
        nonlocal auth
        if auth is None:auth='Basic '+base64.b64encode((a.username+':'+a.password_file.read_text().strip()).encode()).decode()
        data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        req=urllib.request.Request(a.base_url.rstrip('/')+'/api'+path,data=data,method=method,headers={'Authorization':auth,'X-Pulse-Request':'1','Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=600) as r:return json.load(r)
    report={'documents':0,'indexed':0,'failed':[]}
    if a.vk:
        root='https://workspace.vk.ru/docs/'+('on-premises/vk-teams' if a.vk=='on-premises' else 'saas')
        docs,coverage=crawl(root,'vk-teams-on-premises' if a.vk=='on-premises' else 'vk-workspace-saas',a.version)
        report['coverage']=coverage
    elif a.jsonl:
        opener=gzip.open if a.jsonl.suffix=='.gz' else open
        with opener(a.jsonl,'rt',encoding='utf-8') as f:docs=[json.loads(line) for line in f if line.strip()]
    elif a.directory:
        docs=[]
        from .documents import extract
        for file in sorted(a.directory.rglob('*')):
            if file.is_file() and not file.is_symlink() and file.suffix.lower() in ('.md','.txt','.rst','.pdf','.docx','.html'):
                body=extract(file.name,file.read_bytes())
                docs.append(dict(name=str(file.relative_to(a.directory)),source=a.source,product=a.product,version=a.version,language='ru',cluster_id=a.cluster,body=body))
    else:
        docs=[];offset=0
        while True:
            page=api('GET','/knowledge/documents?'+urllib.parse.urlencode({'cluster_id':a.cluster,'offset':offset,'limit':100}))
            for row in page['items']:docs.append(api('GET','/knowledge/documents/'+row['id']))
            offset+=len(page['items'])
            if offset>=page['total']:break
    report['documents']=len(docs)
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True)
        with a.output.open('w') as f:
            for doc in docs:f.write(json.dumps(doc,ensure_ascii=False)+'\n')
    else:
        for index,doc in enumerate(docs,1):
            allowed={k:doc[k] for k in ('name','cluster_id','source','product','version','language','body') if k in doc}
            try:api('POST','/knowledge/documents',allowed);report['indexed']+=1
            except (urllib.error.URLError,ValueError) as e:report['failed'].append({'source':doc.get('source'),'error':str(e)[:120]})
            if index%25==0:print(f'Processed {index}/{len(docs)}; indexed {report["indexed"]}',flush=True)
    a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(f'Documents {len(docs)}; indexed {report["indexed"]}; failures {len(report["failed"])}; report {a.report}')
    if report['failed'] or report.get('coverage',{}).get('failures') or report.get('coverage',{}).get('limit_reached') or report.get('coverage',{}).get('skipped') or not docs:raise SystemExit(1)


if __name__=='__main__':main()
