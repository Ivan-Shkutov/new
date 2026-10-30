#!/usr/bin/env python3
"""Export the official Kubernetes website docs checkout as a versioned text corpus."""
import argparse,gzip,hashlib,json,subprocess
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--checkout',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--version',required=True);a=p.parse_args()
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.checkout,text=True).strip()
origin=subprocess.check_output(['git','remote','get-url','origin'],cwd=a.checkout,text=True).strip()
if origin.removesuffix('.git')!='https://github.com/kubernetes/website':raise SystemExit('Expected official kubernetes/website checkout')
counts={};records=[]
for language in ('en','ru'):
 files=sorted((a.checkout/'content'/language/'docs').rglob('*.md'));counts[language]=len(files)
 for file in files:
  if file.is_symlink():continue
  body=file.read_text();relative=str(file.relative_to(a.checkout))
  if len(body.strip())<20:continue
  records.append({'name':relative,'source':f'https://github.com/kubernetes/website/blob/{commit}/{relative}','product':'kubernetes','version':a.version,'language':language,'cluster_id':'','body':body})
a.output.parent.mkdir(parents=True,exist_ok=True)
with a.output.open('wb') as raw:
 with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0,filename='') as gz:
  for record in records:gz.write((json.dumps(record,ensure_ascii=False)+'\n').encode())
manifest={'origin':origin,'commit':commit,'version':a.version,'languages':counts,'documents':len(records),'sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),'license':'CC BY 4.0; Kubernetes Authors','scope':'All Markdown files in content/en/docs and content/ru/docs. Not images, generated external API references, translations outside these directories, or examples fetched from external links.'}
Path(str(a.output)+'.manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n');print(json.dumps(manifest,ensure_ascii=False))
