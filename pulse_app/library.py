"""Bounded Markdown-only archive ingestion through the authenticated Pulse API.

No archive member is extracted or executed. The Pulse catalogue remains the
authority for Qdrant publications. Supports one-shot import and a watched inbox.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MAX_MEMBERS=30000
MAX_EXPANDED=2*1024**3
MAX_DOCUMENT=2_000_000


def safe_name(name):
    value=PurePosixPath(name)
    if not name or '\\' in name or any(ord(c)<32 for c in name) or value.is_absolute() or '..' in value.parts or (value.parts and ':' in value.parts[0]):
        raise ValueError('Недопустимый путь в архиве')
    return value.as_posix()


def markdown_files(path, report):
    """Preflight all member metadata before reading the first document."""
    path=Path(path)
    if path.is_symlink():raise ValueError('Символические ссылки не импортируются')
    if path.is_dir():
        files=[];total=0
        for file in sorted(path.rglob('*')):
            if file.is_symlink():raise ValueError('Символическая ссылка в библиотеке')
            if not file.is_file():continue
            total+=file.stat().st_size
            files.append(file)
            if len(files)>MAX_MEMBERS or total>MAX_EXPANDED:raise ValueError('Библиотека превышает лимит размера или файлов')
        for file in files:
            name=safe_name(file.relative_to(path).as_posix())
            if file.suffix.lower()!='.md':report['skipped']+=1;continue
            if file.stat().st_size>MAX_DOCUMENT:raise ValueError('Markdown превышает 2 MB: '+name)
            yield name,file.read_bytes()
        return
    if path.suffix.lower()=='.md':
        if path.stat().st_size>MAX_DOCUMENT:raise ValueError('Markdown превышает 2 MB')
        yield path.name,path.read_bytes();return
    zipped=path.suffix.lower()=='.zip'
    if not zipped and not (path.name.endswith('.tar.gz') or path.name.endswith('.tgz') or path.name.endswith('.tar')):
        raise ValueError('Поддерживаются MD, ZIP, TAR и TAR.GZ')
    opener=zipfile.ZipFile if zipped else tarfile.open
    with opener(path) as archive:
        members=archive.infolist() if zipped else archive
        accepted=[];seen=set();total=0;count=0
        for member in members:
            count+=1
            if count>MAX_MEMBERS:raise ValueError('Слишком много файлов в архиве')
            name=safe_name(member.filename if zipped else member.name)
            directory=member.is_dir() if zipped else member.isdir()
            if directory:continue
            if name in seen:raise ValueError('Повторяющийся путь в архиве')
            seen.add(name)
            if zipped:
                mode=member.external_attr>>16
                if stat.S_ISLNK(mode) or member.flag_bits&1:raise ValueError('Ссылки и зашифрованные файлы не поддерживаются')
                length=member.file_size
            else:
                if not member.isreg():raise ValueError('Ссылки и специальные файлы не поддерживаются')
                length=member.size
            total+=length
            if total>MAX_EXPANDED:raise ValueError('Распакованный архив превышает 2 GiB')
            if not name.lower().endswith('.md'):report['skipped']+=1;continue
            if length>MAX_DOCUMENT:raise ValueError('Markdown превышает 2 MB: '+name)
            accepted.append((name,member))
        for name,member in accepted:
            stream=archive.open(member) if zipped else archive.extractfile(member)
            with stream:
                content=stream.read(MAX_DOCUMENT+1)
            if len(content)>MAX_DOCUMENT:raise ValueError('Размер Markdown не соответствует метаданным')
            yield name,content


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):digest.update(data)
    return digest.hexdigest()


def validate_metadata(metadata):
    if not isinstance(metadata,dict):raise ValueError('manifest.json должен содержать JSON-объект')
    allowed={'product','version','source','cluster_id','language'}
    if set(metadata)-allowed or any(not isinstance(v,str) for v in metadata.values()):raise ValueError('Неверный manifest.json')
    for key in ('product','version','source'):
        if not metadata.get(key):raise ValueError('manifest.json: нужно поле '+key)
    if len(metadata['product'])>80 or len(metadata['version'])>80 or len(metadata['source'])>2000:raise ValueError('Слишком длинные метаданные')
    return {'cluster_id':'','language':'ru',**metadata}


def import_path(path, metadata, send):
    metadata=validate_metadata(metadata)
    report={'status':'running','documents':0,'indexed':0,'unchanged':0,'skipped':0,'failed_count':0,'failed':[]}
    def failure(value):
        report['failed_count']+=1
        if len(report['failed'])<100:report['failed'].append(value)
    try:
        for name,raw in markdown_files(path,report):
            report['documents']+=1
            try:
                body=raw.decode('utf-8-sig')
                if len(body.strip())<20 or '\x00' in body:report['skipped']+=1;continue
                result=send({**metadata,'name':name,'body':body})
                report['unchanged' if result.get('unchanged') else 'indexed']+=1
            except (ValueError,UnicodeError,urllib.error.URLError) as exc:
                failure({'name':name[:300],'error':str(exc)[:180]})
        report['status']='complete' if not report['failed_count'] and report['indexed']+report['unchanged'] else 'failed'
        if not report['indexed']+report['unchanged']:report['error']='Нет успешно обработанных Markdown-документов'
    except (ValueError,OSError,tarfile.TarError,zipfile.BadZipFile) as exc:
        failure({'error':str(exc)[:180]});report['status']='failed'
    return report


def api_sender(base_url,password_file,username):
    origin=urllib.parse.urlsplit(base_url)
    if origin.scheme!='https' and not (origin.scheme=='http' and origin.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('Для импорта нужен HTTPS или loopback')
    # Never follow an HTTP redirect with administrator credentials.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def send(document):
        auth=base64.b64encode((username+':'+Path(password_file).read_text().strip()).encode()).decode()
        request=urllib.request.Request(base_url.rstrip('/')+'/api/knowledge/documents',data=json.dumps(document,ensure_ascii=False).encode(),headers={'Authorization':'Basic '+auth,'X-Pulse-Request':'1','Content-Type':'application/json'})
        try:
            with opener.open(request,timeout=900) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ValueError('Pulse import HTTP '+str(exc.code)) from None
    def info():
        auth=base64.b64encode((username+':'+Path(password_file).read_text().strip()).encode()).decode()
        request=urllib.request.Request(base_url.rstrip('/')+'/api/knowledge/status',headers={'Authorization':'Basic '+auth})
        with opener.open(request,timeout=20) as response:return json.load(response)
    send.info=info
    return send


def save_report(path,report):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    temporary.replace(path)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',type=Path,required=True)
    parser.add_argument('--watch',action='store_true',help='Watch product folders containing manifest.json')
    parser.add_argument('--reports',type=Path,default=Path('/library/reports'))
    parser.add_argument('--base-url',default='http://127.0.0.1:8090')
    parser.add_argument('--password-file',type=Path,default=Path('/run/secrets/portal-password'))
    parser.add_argument('--username',default='pulse')
    args=parser.parse_args(argv)
    args.reports.mkdir(parents=True,exist_ok=True)
    send=api_sender(args.base_url,args.password_file,args.username)
    if not args.watch:
        metadata=json.loads((args.path.parent/'manifest.json').read_text())
        report=import_path(args.path,metadata,send);save_report(args.reports/'import.json',report)
        print(json.dumps({k:v for k,v in report.items() if k!='failed'}),flush=True)
        return 0 if report['status']=='complete' else 1
    observed={};completed={}
    while True:
        try:collection=send.info().get('collection','')
        except (OSError,ValueError):time.sleep(15);continue
        for folder in sorted(args.path.iterdir()):
            if not folder.is_dir() or folder.is_symlink():continue
            manifest=folder/'manifest.json'
            pending_report=args.reports/('metadata-'+hashlib.sha256(str(folder).encode()).hexdigest()[:16]+'.json')
            if not manifest.is_file() or manifest.is_symlink():
                if not pending_report.exists():save_report(pending_report,{'status':'awaiting_metadata','file':folder.name,'error':'Добавьте manifest.json с product, version и source'})
                continue
            if pending_report.exists():pending_report.unlink()
            for path in sorted(folder.iterdir()):
                if not path.is_file() or path.is_symlink() or path==manifest or not (path.suffix.lower() in ('.zip','.md') or path.name.endswith(('.tar.gz','.tar','.tgz'))):continue
                key=str(path);stamp=(path.stat().st_size,path.stat().st_mtime_ns,manifest.stat().st_mtime_ns)
                if observed.get(key)!=stamp:observed[key]=stamp;continue
                if completed.get(key)==(stamp,collection):continue
                report_path=args.reports/(hashlib.sha256(key.encode()).hexdigest()[:20]+'.json')
                try:
                    digest=sha256(path);metadata=json.loads(manifest.read_text());signature=hashlib.sha256(json.dumps(metadata,sort_keys=True).encode()).hexdigest()
                    if report_path.exists():
                        previous=json.loads(report_path.read_text())
                        if previous.get('status')=='complete' and previous.get('sha256')==digest and previous.get('metadata_sha256')==signature and previous.get('collection')==collection:
                            completed[key]=(stamp,collection);continue
                        # Failed imports retry after 5 minutes, never in a tight loop.
                        if previous.get('status')=='failed' and time.time()-previous.get('attempted_at',0)<300:continue
                    started=time.time()
                    save_report(report_path,{'status':'running','file':path.name,'attempted_at':started})
                    report=import_path(path,metadata,send)
                    after=(path.stat().st_size,path.stat().st_mtime_ns,manifest.stat().st_mtime_ns)
                    if after!=stamp:report.update(status='failed',error='Входной файл изменился при импорте; будет повторная попытка')
                    report.update(file=path.name,sha256=digest,metadata_sha256=signature,attempted_at=started,collection=collection)
                    save_report(report_path,report)
                    if report['status']=='complete':completed[key]=(stamp,collection)
                    print(path.name,report['status'],'indexed',report['indexed'],'unchanged',report['unchanged'],flush=True)
                except (OSError,ValueError) as exc:
                    save_report(report_path,{'status':'failed','file':path.name,'attempted_at':time.time(),'error':str(exc)[:180]})
        time.sleep(15)


if __name__=='__main__':raise SystemExit(main())
