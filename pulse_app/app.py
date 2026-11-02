"""Pulse: one administrator, one process, read-only infrastructure integrations."""
import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import __version__
from .store import Store
from .documents import extract, MAX_BYTES
from . import connectors


def now():
    return datetime.now(timezone.utc).isoformat()


def create_app(runtime=None, polling=True):
    runtime = Path(runtime or os.environ.get('PULSE_RUNTIME', 'runtime')).resolve()
    auth = HTTPBasic(auto_error=False)
    stop = threading.Event()
    refresh_lock = threading.Lock()
    slots = threading.BoundedSemaphore(4)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='pulse-investigation')
    state = {}
    from .sessions import Sessions, SessionGone
    sessions = Sessions()
    interval = max(10, int(os.environ.get('PULSE_POLL_SECONDS', '30')))

    def db():
        return state['store']

    def require_admin(credentials: Optional[HTTPBasicCredentials] = Depends(auth)):
        password_path = Path(os.environ.get('PULSE_PASSWORD_FILE', str(runtime / 'secrets/portal-password')))
        if not password_path.is_file():
            raise HTTPException(503, 'Выполните ./pulse init перед запуском')
        password = password_path.read_text().strip()
        if len(password) < 16:
            raise HTTPException(503, 'Пароль установки должен содержать не менее 16 символов')
        valid = credentials is not None and secrets.compare_digest(credentials.username.encode(), os.environ.get('PULSE_USERNAME', 'pulse').encode()) and secrets.compare_digest(credentials.password.encode(), password.encode())
        if not valid:
            raise HTTPException(401, 'Требуется вход', headers={'WWW-Authenticate': 'Basic realm="Pulse", charset="UTF-8"'})

    def cluster(cid):
        item = db().cluster(cid)
        if not item:
            raise HTTPException(404, 'Кластер не найден')
        return item

    def snapshot(cid):
        rows = db().rows('SELECT value FROM snapshots WHERE cluster_id=?', (cid,))
        return json.loads(rows[0]['value']) if rows else {'observed_at': None, 'resources': {}, 'errors': ['Первичный сбор ещё не выполнен'], 'complete': False}

    def refresh(cid):
        item = cluster(cid)
        if not item['enabled']:
            raise HTTPException(409, 'Сбор кластера приостановлен')
        if not refresh_lock.acquire(blocking=False):
            raise HTTPException(409, 'Сбор уже выполняется; повторите позднее')
        try:
            try:
                result = connectors.collect_cluster(item['config']['kube'])
            except Exception:
                # Preserve last observations; failed attempt is not a new healthy snapshot.
                result = snapshot(cid)
                result['errors'] = ['Не удалось получить данные Kubernetes. Проверьте endpoint, CA и права.']
                result['complete'] = False
                result['last_attempt_at'] = now()
            with db().connect() as c:
                current = c.execute('SELECT config FROM clusters WHERE id=?', (cid,)).fetchone()
                if current and db().decrypt(current['config']) == item['config']:
                    c.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)', (cid, json.dumps(result)))
            return result
        finally:
            refresh_lock.release()

    def collector():
        while not stop.is_set():
            for row in db().rows('SELECT id FROM clusters WHERE enabled=1'):
                if stop.is_set():
                    break
                try:
                    refresh(row['id'])
                except Exception:
                    pass  # Last successful snapshot remains visible; next cycle retries.
            stop.wait(interval)

    @asynccontextmanager
    async def lifespan(app):
        runtime.mkdir(parents=True, exist_ok=True)
        lock = open(runtime / 'controller.lock', 'a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError('Only one Pulse process may use this runtime')
        try:
            state['store'] = Store(runtime)
            from .knowledge import Knowledge
            state['knowledge'] = Knowledge(state['store'])
            def reap_sessions():
                while not stop.wait(30):
                    with sessions.lock:sessions.prune()
            reaper=threading.Thread(target=reap_sessions,daemon=True)
            reaper.start()
            thread = threading.Thread(target=collector, daemon=True) if polling else None
            if thread:
                thread.start()
            yield
        finally:
            stop.set()
            with sessions.lock:sessions.items.clear()
            if 'reaper' in locals():reaper.join(timeout=2)
            pool.shutdown(wait=True, cancel_futures=True)
            if polling and 'thread' in locals() and thread:
                # Wait until any bounded connector request has ended before releasing runtime.
                await asyncio.to_thread(thread.join)
            lock.close()

    app = FastAPI(title='Pulse Clusters', version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default error response echoes submitted credentials.
        return JSONResponse({'detail': '; '.join('.'.join(str(x) for x in e['loc']) + ': ' + e['msg'] for e in exc.errors())}, status_code=422)

    @app.middleware('http')
    async def guard(request: Request, call_next):
        from fastapi.responses import JSONResponse
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if request.headers.get('x-pulse-request') != '1':
                return JSONResponse({'detail': 'Требуется X-Pulse-Request: 1'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Недопустимый Origin'}, status_code=403)
        try:
            if int(request.headers.get('content-length', '0')) > MAX_BYTES + 65536:
                return JSONResponse({'detail': 'Запрос слишком большой'}, status_code=413)
        except ValueError:
            return JSONResponse({'detail': 'Некорректная длина запроса'}, status_code=400)
        if request.method in ('POST', 'PUT', 'PATCH'):
            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BYTES + 65536:
                    return JSONResponse({'detail': 'Запрос слишком большой'}, status_code=413)
                chunks.append(chunk)
            request._body = b''.join(chunks)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @app.get('/healthz')
    def health():
        from .build import identity
        return {'status':'ok',**identity()}

    @app.get('/', dependencies=[Depends(require_admin)])
    def index():
        return FileResponse(Path(__file__).parent / 'static/index.html')

    @app.get('/static/{name}', dependencies=[Depends(require_admin)])
    def static(name: str):
        if name not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / 'static' / name)

    from fastapi import APIRouter
    api = APIRouter(prefix='/api', dependencies=[Depends(require_admin)])

    @api.get('/version')
    def version_info():
        from .build import identity
        return identity()

    def public_cluster(row):
        cfg = row['config']
        astra={k:v for k,v in cfg.get('astra',{}).items() if k in ('url','cluster_field','cluster_value','namespace_field','pod_field')}
        astra['has_token']=bool(cfg.get('astra',{}).get('token'))
        return {'astra':astra,'id': row['id'], 'name': row['name'], 'enabled': bool(row['enabled']), 'has_metrics': connectors.metrics_configured(cfg.get('metrics', {})), 'has_astra': bool(cfg.get('astra', {}).get('url')), 'metrics':{k:v for k,v in cfg.get('metrics',{}).items() if k in ('url','transport','namespace','service','port','mode','labels')}, 'holmes':{'enabled':bool(cfg.get('holmes',{}).get('enabled')), 'url':cfg.get('holmes',{}).get('url',''),'model':cfg.get('holmes',{}).get('model',''),'has_key':bool(cfg.get('holmes',{}).get('api_key'))}, 'astra_ai':{'enabled':bool(cfg.get('astra_ai',{}).get('enabled')),'url':cfg.get('astra_ai',{}).get('url',''),'model':cfg.get('astra_ai',{}).get('model',''),'has_key':bool(cfg.get('astra_ai',{}).get('api_key'))}}

    from . import dashboard, health
    metric_cache = {}
    metric_cache_lock = threading.Lock()

    @api.get('/dashboard')
    def dashboard_route(cluster_id: str=''):
        if cluster_id:
            cluster(cluster_id)
        rows = overview()['clusters']
        selected = [row for row in rows if not cluster_id or row['id'] == cluster_id]
        summaries = []
        for row in selected:
            summary = dashboard.summarize(snapshot(row['id']), row['id'], row['name'], interval)
            if not row['enabled']:
                summary['health'] = health.assess(snapshot(row['id']),row['id'],row['name'],summary['workloads'],interval,enabled=False)
            summary['issues'] = summary['health']['issues']
            summaries.append(summary)
            row.update(summary)
            cfg = cluster(row['id'])['config']
            row['sources'] = {'metrics':connectors.metrics_configured(cfg.get('metrics',{})), 'astra':bool(cfg.get('astra',{}).get('url'))}
        from .build import identity
        return {**identity(), 'clusters':selected, 'workloads':dashboard.combine(summaries),
                'health':health.aggregate([s['health'] for s in summaries]),
                'issues':sorted([issue for summary in summaries for issue in summary['issues']],key=lambda i:health.SEVERITY_ORDER[i['severity']])[:40],
                'issues_total':sum(summary['issues_total'] for summary in summaries),
                'llm_configured':bool(db().settings().get('llm',{}).get('model'))}

    @api.get('/clusters/{cid}/dashboard-metrics')
    def dashboard_metric_route(cid: str, window: str='1h'):
        import time
        if window not in ('1h','6h','24h'):
            raise HTTPException(422, 'Допустимые интервалы: 1h, 6h, 24h')
        item = cluster(cid)
        cfg = connectors.metrics_config(item['config'])
        if not connectors.metrics_configured(cfg):
            return {'configured':False,'status':'unconfigured','metrics':{},'note':'Источник метрик не сохранён','action':'Подключить метрики'}
        key = (cid, hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest(), window)
        with metric_cache_lock:
            cached = metric_cache.get(key)
            if cached and time.monotonic()-cached[0] < 30:
                return cached[1]
        try:
            result = {'configured':True, **connectors.dashboard_metrics(cfg,window)}
            from .cluster_chat import live_list
            from .metric_setup import apply_coverage
            result=apply_coverage(result,live_list(item['config']['kube'],'nodes'))
        except connectors.ConnectorError as e:
            result = {'configured':True,'metrics':{},'error':str(e)}
        with metric_cache_lock:
            if len(metric_cache) >= 60:
                metric_cache.clear()
            metric_cache[key] = (time.monotonic(), result)
        return result

    def problem_items(cid=''):
        rows=db().rows('SELECT id,name,enabled FROM clusters ORDER BY name')
        items=[];coverage=[]
        for row in rows:
            if cid and row['id']!=cid:
                continue
            summary=dashboard.summarize(snapshot(row['id']),row['id'],row['name'],interval,issue_limit=None)
            h=summary['health']
            coverage.append({'cluster_id':row['id'],'stale':h['status']=='stale','incomplete':h['incomplete'],'enabled':bool(row['enabled'])})
            for issue in summary['issues']:
                issue=dict(issue)
                issue['stale']=issue['stale'] or not bool(row['enabled'])
                raw='|'.join(str(issue.get(k,'')) for k in ('cluster_id','kind','uid','namespace','name','title'))
                issue['id']=hashlib.sha256(raw.encode()).hexdigest()[:32]
                items.append(issue)
        items.sort(key=lambda i:(health.SEVERITY_ORDER[i['severity']],i['cluster_id'],i['kind'],i['name']))
        return items,coverage

    @api.get('/problems')
    def problems(cluster_id: str='', severity: str='all', q: str='', offset: int=0, limit: int=25):
        if cluster_id:
            cluster(cluster_id)
        if severity not in ('all','critical','warning') or offset<0 or not 1<=limit<=100 or len(q)>300:
            raise HTTPException(422,'Некорректные фильтры списка')
        items,coverage=problem_items(cluster_id)
        query=q.lower().strip()
        selected=[i for i in items if (severity=='all' or i['severity']==severity) and (not query or query in ' '.join(str(i.get(k,'')) for k in ('name','namespace','title','detail','cluster_name','kind')).lower())]
        return {'items':selected[offset:offset+limit],'total':len(selected),'offset':offset,'limit':limit,'coverage':coverage}

    @api.get('/clusters/{cid}/metrics-detail')
    def metric_detail(cid: str, metric: str='cpu', window: str='1h'):
        from concurrent.futures import ThreadPoolExecutor
        cfg=connectors.metrics_config(cluster(cid)['config'])
        mapping={'cpu':('node_cpu','cpu'),'ram':('node_memory','node_memory_total','memory'),'disk':('filesystem','filesystem_size','pvc_used','pvc_capacity')}
        if metric not in mapping:
            raise HTTPException(422,'Метрика должна быть cpu, ram или disk')
        summary=dashboard_metric_route(cid,window)
        def read(key):
            try:return key,connectors.query_metrics(cfg,key)
            except connectors.ConnectorError as e:return key,{'error':str(e)}
        series={}
        if connectors.metrics_configured(cfg):
            with ThreadPoolExecutor(max_workers=3) as workers:
                series=dict(workers.map(read,mapping[metric]))
        return {'metric':metric,'summary':summary,'series':series,'nodes':snapshot(cid).get('resources',{}).get('nodes',{}),'units':{'node_cpu':'cores (non-idle)','cpu':'cores','node_memory':'bytes available','node_memory_total':'bytes total','memory':'bytes working set','filesystem':'bytes available','filesystem_size':'bytes total','pvc_used':'bytes used','pvc_capacity':'bytes capacity'}}

    @api.get('/clusters')
    def clusters():
        return {'items': [public_cluster(cluster(r['id'])) for r in db().rows('SELECT id FROM clusters ORDER BY name')]}

    @api.get('/overview')
    def overview():
        items = []
        for row in clusters()['items']:
            snap = snapshot(row['id'])
            observed = snap.get('observed_at')
            elapsed = health.age(observed,datetime.now(timezone.utc))
            stale = elapsed is None or elapsed > max(120,interval*3)
            row.update(observed_at=observed, status='paused' if not row['enabled'] else 'waiting' if not observed else 'stale' if stale else 'degraded' if snap.get('errors') or snap.get('complete') is False or any(r.get('truncated') for r in snap.get('resources', {}).values()) else 'ok', counts={k: len(snap.get('resources', {}).get(k, {}).get('items', [])) if k in snap.get('resources', {}) and not snap['resources'][k].get('error') else None for k in ('nodes','pods','namespaces')}, error='; '.join(snap.get('errors', [])))
            items.append(row)
        return {'version': __version__, 'clusters': items}

    class ClusterInput(BaseModel):
        id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,47}$')
        name: str = Field(min_length=1, max_length=120)
        kubeconfig: str = Field(min_length=1, max_length=200000)
        enabled: bool = True
        metrics: dict = Field(default_factory=dict)
        astra: dict = Field(default_factory=dict)
        holmes: dict = Field(default_factory=dict)
        astra_ai: dict = Field(default_factory=dict)

    def checked_kube(text):
        try:
            return connectors.validate_kubeconfig(text)
        except Exception as e:
            raise HTTPException(422, str(e) if isinstance(e, connectors.ConnectorError) else 'Некорректный kubeconfig')

    def validate_source(source):
        from urllib.parse import urlparse
        # Reject invalid configuration while saving, before background workers
        # construct headers, SSL contexts or PromQL. Never echo secret values.
        for field, maximum in {'url': 4096, 'model': 200, 'username': 1024,
                               'password': 65536, 'token': 65536, 'api_key': 65536,
                               'ca_pem': 1048576, 'cluster_value': 1024,
                               'cluster_field': 200, 'namespace_field': 200,
                               'pod_field': 200}.items():
            if field in source and (not isinstance(source[field], str) or len(source[field]) > maximum):
                raise HTTPException(422, f'Некорректный тип или размер поля {field}')
        for field in ('token', 'api_key', 'username', 'password'):
            if any(char in source.get(field, '') for char in ('\r', '\n', '\0')):
                raise HTTPException(422, f'Недопустимый символ в поле {field}')
        if 'enabled' in source and not isinstance(source['enabled'],bool):
            raise HTTPException(422,'enabled должен быть boolean')
        if source.get('transport','direct') not in ('direct','kubernetes'):
            raise HTTPException(422,'Транспорт метрик должен быть direct или kubernetes')
        if source.get('transport') == 'kubernetes':
            for field in ('namespace','service'):
                if not isinstance(source.get(field),str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?',source[field]):
                    raise HTTPException(422,'Нужны корректные namespace и service метрик')
            if type(source.get('port')) is not int or not 1 <= source['port'] <= 65535:
                raise HTTPException(422,'Нужен порт сервиса метрик')
        url = source.get('url', '')
        if url:
            try:
                parsed = urlparse(url)
                port = parsed.port
                invalid = (parsed.scheme not in ('http', 'https') or not parsed.hostname
                           or parsed.username is not None or parsed.password is not None
                           or parsed.query or parsed.fragment or port == 0
                           or any(char.isspace() or ord(char) < 32 for char in url))
            except ValueError:
                invalid = True
            if invalid:
                raise HTTPException(422, 'Нужен HTTP(S) URL без credentials/query/fragment')
        if 'mode' in source and source['mode'] not in ('shared', 'dedicated'):
            raise HTTPException(422, 'Режим метрик должен быть shared или dedicated')
        if 'labels' in source:
            labels = source['labels']
            if (not isinstance(labels, dict) or len(labels) > 16
                    or any(not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', key)
                           or not isinstance(value, str) or len(value) > 1024
                           for key, value in labels.items())):
                raise HTTPException(422, 'Некорректные метки кластера')
        for field in ('cluster_field', 'namespace_field', 'pod_field'):
            if source.get(field) and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', source[field]):
                raise HTTPException(422, f'Некорректное имя поля {field}')
        return source

    def merge_source(previous, patch):
        # Endpoint changes must not forward a credential to a different service.
        previous = dict(previous)
        patch = dict(patch)
        credential_fields = ('token', 'password', 'username', 'api_key', 'ca_pem')
        clear = patch.pop('clear_credentials', False)
        if not isinstance(clear, bool):
            raise HTTPException(422, 'clear_credentials должен быть boolean')
        endpoint_changed = 'url' in patch and patch['url'].rstrip('/') != previous.get('url', '').rstrip('/') if isinstance(patch.get('url', ''), str) else False
        if endpoint_changed or clear:
            for key in credential_fields:
                previous.pop(key, None)
        else:
            for key in credential_fields:
                if patch.get(key) == '' and previous.get(key):
                    patch.pop(key)
        return validate_source({**previous, **patch})

    @api.post('/clusters/{cid}/metrics-test')
    def test_metrics_source(cid: str):
        cfg=connectors.metrics_config(cluster(cid)['config'])
        try:
            data=connectors.query_metrics(cfg,'node_memory_total')
            return {'ok':not data['empty'],'series':len(data['data'].get('result',[])),
                    'note':'node_exporter доступен' if not data['empty'] else 'Нет node_memory_MemTotal_bytes: проверьте node_exporter и labels'}
        except connectors.ConnectorError as e:
            raise HTTPException(502,str(e)+'; для Kubernetes proxy проверьте GET services/proxy в namespace monitoring')

    @api.get('/clusters/{cid}/metrics-discovery')
    def discover_metrics(cid: str):
        from .cluster_chat import live_list
        from .metric_setup import candidates
        result=live_list(cluster(cid)['config']['kube'],'services')
        cached=False
        if not result['complete']:
            entry=snapshot(cid).get('resources',{}).get('services',{})
            if not result['items'] and not entry.get('error'):
                result['items']=entry.get('items',[]);cached=True
        return {'items':candidates(result['items']),'note':'Выберите источник этого кластера; сохранение выполняется после проверки CPU/RAM/дисков.',
                'observed_at':result['observed_at'],'error':result['error'],'cached':cached}

    @api.get('/clusters/{cid}/metrics-diagnostics')
    def metrics_diagnostics(cid: str):
        from .metric_setup import probe
        return probe(cluster(cid)['config'])

    class ConnectMetrics(BaseModel):
        source: dict

    @api.post('/clusters/{cid}/metrics-connect')
    def connect_metrics(cid: str,body: ConnectMetrics):
        from .metric_setup import probe
        item=cluster(cid);original=item['config']
        source=merge_source(original.get('metrics',{}),body.source)
        report=probe(original,source)
        if not report['ok']:return {'saved':False,**report}
        with db().connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row=connection.execute('SELECT config FROM clusters WHERE id=?',(cid,)).fetchone()
            if not row or db().decrypt(row['config'])!=original:
                raise HTTPException(409,'Подключение изменилось во время проверки. Повторите проверку.')
            config={**original,'metrics':source}
            connection.execute('UPDATE clusters SET config=? WHERE id=?',(db().encrypt(config),cid))
        with metric_cache_lock:metric_cache.clear()
        return {'saved':True,**report,'cluster':public_cluster(cluster(cid))}

    def validate_engines(cfg):
        if cfg.get('astra_ai',{}).get('enabled') and cfg.get('holmes',{}).get('enabled'):
            raise HTTPException(422,'Выберите один движок: Astra AI или Holmes')
        source=cfg.get('astra_ai',{})
        if source.get('enabled') and (not source.get('url') or not source.get('model') or not source.get('api_key')):
            raise HTTPException(422,'Для Astra AI нужны полный URL, model и Bearer token')

    def selected_engine(cfg,llm):
        if cfg.get('astra_ai',{}).get('enabled'):return cfg['astra_ai']
        if cfg.get('holmes',{}).get('enabled'):return cfg['holmes']
        return llm

    @api.post('/clusters/{cid}/astra-ai-test')
    def probe_astra_ai(cid: str):
        from .astra_ai import chat
        settings=cluster(cid)['config'].get('astra_ai',{})
        if not settings.get('url') or not settings.get('api_key'):
            raise HTTPException(409,'Сначала сохраните URL и Bearer token Astra AI')
        try:
            answer=chat(settings,[{'role':'system','content':'Проверка соединения. Никаких инструментов и действий с инфраструктурой.'},{'role':'user','content':'Кратко подтверди, что анализатор отвечает.'}])
            return {'ok':True,'answer':answer,'stream_completed':True}
        except connectors.ConnectorError as e:
            raise HTTPException(502,str(e))

    @api.post('/clusters', status_code=201)
    def add_cluster(body: ClusterInput):
        if db().cluster(body.id):
            raise HTTPException(409, 'ID уже существует')
        if len(db().rows('SELECT id FROM clusters')) >= 20:
            raise HTTPException(409, 'Лимит этого выпуска: 20 подключений')
        cfg = {'generation': uuid.uuid4().hex, 'kube': checked_kube(body.kubeconfig), 'metrics': validate_source(body.metrics), 'astra': validate_source(body.astra), 'holmes': validate_source(body.holmes),'astra_ai':validate_source(body.astra_ai)}
        validate_engines(cfg)
        db().execute('INSERT INTO clusters VALUES (?,?,?,?)', (body.id, body.name, body.enabled, db().encrypt(cfg)))
        return public_cluster(cluster(body.id))

    class ClusterUpdate(BaseModel):
        name: Optional[str] = Field(default=None, min_length=1, max_length=120)
        kubeconfig: Optional[str] = Field(default=None, max_length=200000)
        enabled: Optional[bool] = None
        metrics: Optional[dict] = None
        astra: Optional[dict] = None
        holmes: Optional[dict] = None
        astra_ai: Optional[dict] = None

    @api.put('/clusters/{cid}')
    def update_cluster(cid: str, body: ClusterUpdate):
        item = cluster(cid)
        cfg = item['config']
        original=json.dumps(cfg,sort_keys=True)
        if body.kubeconfig:
            cfg['kube'] = checked_kube(body.kubeconfig)
            cfg['generation'] = uuid.uuid4().hex
        for field in ('metrics','astra','holmes','astra_ai'):
            val = getattr(body, field)
            if val is not None:
                cfg[field] = merge_source(cfg.get(field, {}), val)
        validate_engines(cfg)
        with db().connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current=connection.execute('SELECT config,name,enabled FROM clusters WHERE id=?',(cid,)).fetchone()
            if not current or json.dumps(db().decrypt(current['config']),sort_keys=True)!=original or current['name']!=item['name'] or current['enabled']!=item['enabled']:
                raise HTTPException(409,'Подключение изменилось. Обновите настройки перед сохранением.')
            connection.execute('UPDATE clusters SET name=?,enabled=?,config=? WHERE id=?',(body.name or item['name'],item['enabled'] if body.enabled is None else body.enabled,db().encrypt(cfg),cid))
            if body.kubeconfig:connection.execute('DELETE FROM snapshots WHERE cluster_id=?',(cid,))
        with metric_cache_lock:metric_cache.clear()
        return public_cluster(cluster(cid))

    @api.delete('/clusters/{cid}')
    def delete_cluster(cid: str):
        cluster(cid)
        if db().rows("SELECT id FROM investigations WHERE cluster_id=? AND status IN ('queued','running')", (cid,)):
            raise HTTPException(409, 'Дождитесь завершения расследования')
        with db().connect() as c:
            for table, col in [('clusters','id'),('snapshots','cluster_id'),('investigations','cluster_id'),('incidents','cluster_id')]:
                c.execute(f'DELETE FROM {table} WHERE {col}=?', (cid,))
            c.execute('DELETE FROM document_fts WHERE id IN (SELECT id FROM documents WHERE cluster_id=?)', (cid,))
            c.execute('DELETE FROM documents WHERE cluster_id=?', (cid,))
        return {'ok': True}

    @api.post('/clusters/{cid}/holmes-test')
    def probe_holmes(cid: str):
        settings=cluster(cid)['config'].get('holmes',{})
        try:
            answer,trace=connectors.holmes_chat(settings,[{'role':'system','content':'Ответь по-русски. Не используй инструменты для этого теста.'},{'role':'user','content':'Коротко подтверди, что связь с HolmesGPT работает.'}])
            return {'ok':True,'answer':answer,'tools':trace}
        except connectors.ConnectorError as e:
            raise HTTPException(502,str(e))

    @api.get('/clusters/{cid}/snapshot')
    def get_snapshot(cid: str):
        cluster(cid)
        return snapshot(cid)

    @api.post('/clusters/{cid}/refresh')
    def refresh_route(cid: str):
        return refresh(cid)

    @api.get('/clusters/{cid}/logs')
    def logs(cid: str, namespace: str, pod: str, container: Optional[str]=None, previous: bool=False, source: str='kubernetes'):
        cfg = cluster(cid)['config']
        try:
            if source == 'astra':
                return connectors.astra_logs(cfg.get('astra', {}), namespace, pod)
            if source != 'kubernetes':
                raise HTTPException(422, 'Неизвестный источник')
            return {'text': connectors.resource_logs(cfg['kube'], namespace, pod, container, previous)}
        except connectors.ConnectorError as e:
            raise HTTPException(502, str(e))

    @api.get('/clusters/{cid}/metrics')
    def metrics(cid: str):
        cfg = connectors.metrics_config(cluster(cid)['config'])
        result = {}
        for metric in ('cpu','memory','node_cpu','node_memory','filesystem','pvc_used','pvc_capacity'):
            try:
                result[metric] = connectors.query_metrics(cfg, metric)
            except connectors.ConnectorError as e:
                result[metric] = {'error': str(e)}
        return result

    @api.get('/settings')
    def settings():
        llm = db().settings().get('llm', {})
        return {'llm': {'url':llm.get('url',''), 'model':llm.get('model',''), 'has_key': bool(llm.get('api_key'))}, 'poll_seconds': interval}

    class SettingsInput(BaseModel):
        llm: dict

    @api.put('/settings')
    def set_settings(body: SettingsInput):
        current = db().settings()
        llm = merge_source(current.get('llm', {}), body.llm)
        current['llm'] = llm
        db().execute('INSERT OR REPLACE INTO settings VALUES (1,?)', (db().encrypt(current),))
        return settings()

    @api.post('/settings/test')
    def test_llm():
        try:
            answer = connectors.llm_chat(db().settings().get('llm', {}), [{'role':'user','content':'Ответь коротко: соединение с моделью работает.'}])
            return {'ok': True, 'answer': answer}
        except connectors.ConnectorError as e:
            raise HTTPException(502, str(e))

    def scope(cid):
        if cid:
            cluster(cid)

    @api.get('/documents')
    def documents(cluster_id: str=''):
        scope(cluster_id)
        return {'items': db().rows('SELECT id,name,cluster_id,created_at,sha256 FROM documents WHERE cluster_id IN (?,?) ORDER BY created_at DESC', ('', cluster_id))}

    @api.post('/documents', status_code=201)
    def upload(file: UploadFile=File(...), cluster_id: str=Form('')):
        scope(cluster_id)
        data = file.file.read(MAX_BYTES + 1)
        text = extract(file.filename or 'document.txt', data)
        item = {'id':uuid.uuid4().hex,'name':Path(file.filename or 'document.txt').name[:200], 'cluster_id':cluster_id,'created_at':now(),'sha256':hashlib.sha256(data).hexdigest()}
        with db().connect() as c:
            total = c.execute('SELECT coalesce(sum(length(body)),0) FROM documents').fetchone()[0]
            if total + len(text) > 20_000_000:
                raise HTTPException(409, 'Корпус превышает 20 млн символов. Удалите ненужные документы.')
            c.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)', (item['id'],item['name'],cluster_id,text,item['created_at'],item['sha256']))
            c.execute('INSERT INTO document_fts VALUES (?,?)', (item['id'], text))
        return item

    @api.delete('/documents/{did}')
    def delete_document(did: str):
        with db().connect() as c:
            c.execute('DELETE FROM document_fts WHERE id=?', (did,))
            count = c.execute('DELETE FROM documents WHERE id=?', (did,)).rowcount
        if not count:
            raise HTTPException(404, 'Документ не найден')
        return {'ok':True}

    def search_docs(query, cid):
        terms = re.findall(r'\w+', query, flags=re.UNICODE)[:15]
        if not terms:
            return []
        match = ' OR '.join('"' + t + '"' for t in terms)
        return db().rows('SELECT d.id,d.name,snippet(document_fts,1,\'\',\'\',\' … \',50) AS excerpt FROM document_fts JOIN documents d ON d.id=document_fts.id WHERE document_fts MATCH ? AND d.cluster_id IN (?,?) ORDER BY rank LIMIT 5', (match,'',cid))

    @api.get('/documents/search')
    def search(q: str, cluster_id: str=''):
        scope(cluster_id)
        return {'items': search_docs(q[:4000], cluster_id)}

    @api.get('/clusters/{cid}/inventory')
    def live_inventory(cid: str,kind: str='pods',filter: str='all',offset: int=0,limit: int=200):
        if kind not in ('pods','nodes') or filter not in ('all','non_running','unhealthy'):raise HTTPException(422,'Некорректный фильтр инвентаря')
        from .cluster_chat import inventory_report
        intent='nodes_all' if kind=='nodes' else 'pods_'+filter
        report=inventory_report(cluster(cid)['config']['kube'],intent)
        report['rows']=report['rows'][max(0,offset):max(0,offset)+max(1,min(300,limit))]
        return report

    @api.get('/knowledge/status')
    def knowledge_status():
        return state['knowledge'].status()

    @api.get('/knowledge/documents')
    def knowledge_documents(cluster_id: str='',offset: int=0,limit: int=100):
        scope(cluster_id)
        limit=max(1,min(100,limit));offset=max(0,offset)
        rows=db().rows("SELECT id,name,cluster_id,source,product,version,language,revision,fingerprint,status,error FROM knowledge_documents WHERE cluster_id IN ('',?) ORDER BY product,version,name LIMIT ? OFFSET ?",(cluster_id,limit,offset))
        for row in rows:
            if row['fingerprint']!=state['knowledge'].fingerprint:row['status']='needs_reindex'
        total=db().rows("SELECT count(*) AS n FROM knowledge_documents WHERE cluster_id IN ('',?)",(cluster_id,))[0]['n']
        return {'items':rows,'total':total,'offset':offset,'limit':limit}

    @api.get('/knowledge/documents/{did}')
    def knowledge_document(did: str):
        rows=db().rows('SELECT * FROM knowledge_documents WHERE id=?',(did,))
        if not rows:raise HTTPException(404,'Документ не найден')
        return rows[0]

    class KnowledgeDocument(BaseModel):
        name: str = Field(min_length=1,max_length=300)
        cluster_id: str = Field(default='',max_length=48)
        source: str = Field(min_length=1,max_length=2000)
        product: str = Field(min_length=1,max_length=80)
        version: str = Field(min_length=1,max_length=80)
        language: str = Field(default='ru',max_length=20)
        body: str = Field(min_length=20,max_length=2_000_000)

    @api.post('/knowledge/documents',status_code=201)
    def import_knowledge(body: KnowledgeDocument):
        scope(body.cluster_id)
        try:return state['knowledge'].ingest(body.model_dump())
        except connectors.ConnectorError as e:raise HTTPException(503,str(e))

    @api.delete('/knowledge/documents/{did}')
    def delete_knowledge(did: str):
        if not state['knowledge'].delete(did):raise HTTPException(404,'Документ не найден')
        return {'ok':True}

    @api.get('/knowledge/search')
    def vector_search(q: str,cluster_id: str=''):
        scope(cluster_id)
        try:
            rows,note=state['knowledge'].search(q[:6000],cluster_id)
            return {'items':rows,'note':note}
        except connectors.ConnectorError as e:raise HTTPException(503,str(e))

    class Investigate(BaseModel):
        cluster_id: str
        question: str = Field(min_length=3, max_length=4000)
        previous_id: Optional[str] = Field(default=None,max_length=64)
        session_id: Optional[str] = Field(default=None,max_length=64)
        chat: bool = False
        problem_id: Optional[str] = Field(default=None,max_length=64)
        metric: Optional[str] = Field(default=None,pattern='^(cpu|ram|disk)$')
        mode: str = Field(default='extended',pattern='^(brief|extended|causes|impact|commands)$')
        namespace: Optional[str] = Field(default=None,max_length=253)
        pod: Optional[str] = Field(default=None,max_length=253)

    def investigate(jid, body, cfg, llm, resolved_context):
        live_report={}
        def update(**fields):
            if body.session_id:
                sessions.update(body.session_id, jid, fields)
            else:
                allowed={'status','answer','error','trace','evidence','context'}
                assert set(fields) <= allowed
                db().execute('UPDATE investigations SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(),jid))
        def previous_job(jid):
            return sessions.job(body.session_id,jid) if body.session_id else get_investigation(jid)
        try:
            update(status='running')
            from .cluster_chat import inventory_intent,inventory_report,inventory_answer,compact_snapshot
            intent=inventory_intent(body.question) if body.cluster_id and not body.pod and not body.namespace else None
            if intent:
                if cluster(body.cluster_id)['config'].get('generation')!=cfg.get('generation'):
                    raise connectors.ConnectorError('Подключение изменилось. Повторите запрос.')
                def progress_list(report):
                    update(evidence=json.dumps({k:v for k,v in report.items() if k!='items'},ensure_ascii=False))
                report=inventory_report(cfg['kube'],intent,progress_list)
                update(evidence=json.dumps(report,ensure_ascii=False),trace=json.dumps([{'tool':'Kubernetes live inventory','status':intent},{'tool':'engine','status':'Точная таблица API; генерация моделью не используется'}],ensure_ascii=False),answer=inventory_answer(report),status='done' if report['complete'] else 'failed',error=report.get('error') or '')
                return
            from .runbooks import build_system_prompt,select_runbooks,POLICY_ID,POLICY_VERSION
            trace=[]
            if body.cluster_id and cluster(body.cluster_id)['config'].get('generation')!=cfg.get('generation'):
                raise connectors.ConnectorError('Подключение изменилось. Запустите анализ заново.')
            snap = snapshot(body.cluster_id) if body.cluster_id else {'resources':{},'complete':False}
            trace.append({'tool':'Kubernetes snapshot','status':'ok' if snap.get('complete') else 'partial','observed_at':snap.get('observed_at')})
            facts = compact_snapshot(snap,body.namespace)
            if body.metric:
                metric_data=dashboard_metric_route(body.cluster_id,'1h')
                facts={'metric_context':metric_data,**facts}
                trace.append({'tool':'Prometheus cluster metrics','status':'available' if metric_data.get('configured') else 'unconfigured'})
            evidence = json.dumps(connectors.redact_structure(facts), ensure_ascii=False)
            context = evidence
            trace.append({'tool':'context scope','status':'cluster '+body.cluster_id})
            live_report={}
            if body.pod and body.namespace:
                from .diagnostics import collect_pod,model_evidence
                cached=next((p for p in snap.get('resources',{}).get('pods',{}).get('items',[]) if p.get('metadata',{}).get('namespace')==body.namespace and p.get('metadata',{}).get('name')==body.pod),None)
                def progress(report):
                    update(evidence=json.dumps(connectors.redact_structure(report),ensure_ascii=False))
                live_report=collect_pod(cfg,body.namespace,body.pod,resolved_context.get('uid',''),cached,progress)
                from .diagnostics import verified_summary
                update(answer=verified_summary(live_report,body.mode),evidence=json.dumps(connectors.redact_structure(live_report),ensure_ascii=False))
                if not resolved_context.get('uid') and live_report.get('pod',{}).get('uid'):
                    resolved_context['uid']=live_report['pod']['uid']
                    update(context=json.dumps(connectors.redact_structure(resolved_context),ensure_ascii=False))
                trace.extend({'tool':r['id']+' '+r['check'],'status':r['status']} for r in live_report['records'])
                context=json.dumps(connectors.redact_structure(model_evidence(live_report)),ensure_ascii=False)
            else:
                context=evidence
            selected_skills=select_runbooks({'context':resolved_context,'pod':live_report.get('pod',{}),'findings':live_report.get('findings',[])},body.question)
            trace.append({'tool':'policy:'+POLICY_ID,'status':'applied','version':POLICY_VERSION})
            trace.extend({'tool':'skill:'+r['id'],'status':'selected','version':r['version']} for r in selected_skills)
            docs = connectors.redact_structure(search_docs(body.question, body.cluster_id))
            trace.append({'tool':'Documents FTS','status':'matches '+str(len(docs))})
            retrieval_query=body.question+' '+str(resolved_context.get('reason',''))+' '+str(resolved_context.get('detail',''))[:1200]
            try:
                version=re.match(r'v(\d+\.\d+)',snap.get('cluster_info',{}).get('server_version') or '')
                vector_docs,vector_note=state['knowledge'].search(retrieval_query+' '+str(live_report.get('pod',{}).get('message',''))[:1000],body.cluster_id,kubernetes_version=version[1] if version else None,exclude_saas=not body.chat and any(r['id']=='vkteams' for r in selected_skills))
            except connectors.ConnectorError as exc:
                vector_docs=[];vector_note=str(exc)
            docs=docs+connectors.redact_structure(vector_docs)
            trace.append({'tool':'Qdrant + '+state['knowledge'].model,'status':vector_note})
            # Volatile dialogs start without transcripts or resolutions from
            # earlier investigations; follow-up stays inside the current session.
            incidents = [] if body.session_id else db().rows('SELECT question,solution FROM incidents WHERE cluster_id=? ORDER BY created_at DESC LIMIT 3', (body.cluster_id,))
            incidents=connectors.redact_structure([{'question':r['question'][:1500],'solution':r['solution'][:3000]} for r in incidents])
            safe_question=connectors.redact_sensitive_text(body.question)
            messages = [
                {'role':'system','content':build_system_prompt(body.mode,body.chat,selected_skills)},
                {'role':'user','content':f'Контекст проблемы: {json.dumps(connectors.redact_structure(resolved_context),ensure_ascii=False)}\nВопрос оператора: {safe_question}\nКластер: {body.cluster_id}\nДоказательства и выполненные проверки: {context}\nСтатус поиска документации: {vector_note}. Лексический поиск и векторный поиск — разные проверки.\nДокументы: {json.dumps(docs,ensure_ascii=False)}\nПодтверждённые решения: {json.dumps(incidents,ensure_ascii=False)}'}]
            if body.chat:
                summary=facts.get('resources',{}).get('pods',{})
                if body.cluster_id:messages[-1]['content']='Сводка Pods из снимка: '+json.dumps({k:summary.get(k) for k in ('count','phases','healthy','unhealthy','error','truncated')},ensure_ascii=False)+'\n'+messages[-1]['content']
            if body.session_id:sessions.get(body.session_id)
            history=[];parent=body.previous_id;seen=set();size=0
            while parent and parent not in seen and len(history)<6:
                seen.add(parent);previous=previous_job(parent)
                if previous['cluster_id']!=body.cluster_id:break
                previous_answer=previous['answer']
                if previous['status']=='failed' and '\n\n## Незавершённый ответ модели\n' in previous_answer:
                    previous_answer=previous_answer.split('\n\n## Незавершённый ответ модели\n',1)[0]
                if previous['status']=='done' and '\n\n## Анализ модели по доказательствам\n' in previous_answer:
                    previous_answer=previous_answer.split('\n\n## Анализ модели по доказательствам\n',1)[1]
                pair=[{'role':'user','content':connectors.redact_sensitive_text(previous['question'])},{'role':'assistant','content':connectors.redact_sensitive_text(('Незавершённый ответ: ' if previous['status']!='done' else '')+previous_answer[:6000])}]
                size+=len(json.dumps(pair))
                if size>24000:break
                history=pair+history;parent=previous.get('previous_id')
            messages[1:1]=history
            if cfg.get('astra_ai',{}).get('enabled'):
                from .astra_ai import chat as astra_chat
                trace.append({'tool':'engine','status':'Astra AI / SSE'})
                update(trace=json.dumps(trace,ensure_ascii=False))
                def partial(text):
                    prefix=(verified_summary(live_report,body.mode)+'\n\n## Анализ модели по доказательствам\n') if live_report else ''
                    update(answer=prefix+text)
                answer=astra_chat(cfg['astra_ai'],messages,partial)
            elif cfg.get('holmes',{}).get('enabled'):
                answer,tool_trace=connectors.holmes_chat(cfg['holmes'],messages)
                trace.extend(tool_trace);trace.append({'tool':'engine','status':'HolmesGPT'})
            else:
                answer=connectors.llm_chat(llm,messages)
                trace.append({'tool':'engine','status':'local LLM'})
            if live_report:
                from .diagnostics import verified_summary,review_answer
                warnings=review_answer(live_report,answer)
                trace.append({'tool':'answer-review','status':'needs_review' if warnings else 'checked','warnings':warnings,'note':'Проверены ссылки и явная причина; семантическая точность не гарантируется.'})
                answer=verified_summary(live_report,body.mode)+'\n\n## Анализ модели по доказательствам\n'+answer
            answer=connectors.redact_sensitive_text(answer)
            update(trace=json.dumps(connectors.redact_structure(trace),ensure_ascii=False))
            update(status='done',answer=answer)
        except SessionGone:
            pass
        except Exception as exc:
            if body.session_id:
                try:sessions.get(body.session_id)
                except SessionGone:return
            error=str(exc) if isinstance(exc,connectors.ConnectorError) else 'Не удалось завершить анализ. Проверьте модель, лимиты и источники.'
            from .astra_ai import IncompleteAnswer
            if live_report:
                from .diagnostics import verified_summary
                partial=exc.partial if isinstance(exc,IncompleteAnswer) else ''
                update(answer=verified_summary(live_report,body.mode)+('\n\n## Незавершённый ответ модели\n'+partial if partial else ''))
            elif isinstance(exc,IncompleteAnswer):update(answer=exc.partial)
            partial_trace=locals().get('trace',[])+[{'tool':'analysis','status':'failed'}]
            update(status='failed',error=error,trace=json.dumps(connectors.redact_structure(partial_trace),ensure_ascii=False))
        finally:
            slots.release()

    @api.post('/investigations', status_code=202)
    def create_investigation(body: Investigate):
        if body.session_id or body.chat:raise HTTPException(422,'Используйте API временного диалога')
        item = cluster(body.cluster_id)
        if body.previous_id:
            previous = get_investigation(body.previous_id)
            if previous['cluster_id'] != body.cluster_id or previous['status'] != 'done':
                raise HTTPException(409, 'Продолжать можно только завершённый анализ этого кластера')
        resolved_context={}
        if body.previous_id:
            resolved_context=json.loads(previous.get('context') or '{}')
            if not body.metric:body.metric=resolved_context.get('metric')
            if not body.namespace:body.namespace=resolved_context.get('namespace') or None
            if not body.pod and resolved_context.get('kind')=='pods':body.pod=resolved_context.get('name')
        if body.problem_id:
            resolved_context=next((i for i in problem_items(body.cluster_id)[0] if i['id']==body.problem_id),None)
            if resolved_context is None:raise HTTPException(409,'Сигнал изменился или исчез. Обновите список проблем.')
            body.namespace=resolved_context.get('namespace') or None
            body.pod=resolved_context['name'] if resolved_context.get('kind')=='pods' else None
        if body.metric and not resolved_context:
            resolved_context={'metric':body.metric,'cluster_id':body.cluster_id,'title':'Утилизация '+body.metric}
        if body.namespace and body.pod and not resolved_context:
            resolved_context={'kind':'pods','namespace':body.namespace,'name':body.pod,'cluster_id':body.cluster_id}
        llm = db().settings().get('llm', {})
        engine=selected_engine(item['config'],llm)
        from .cluster_chat import inventory_intent
        if (not engine.get('url') or not engine.get('model')) and not (not body.pod and not body.namespace and inventory_intent(body.question)):
            raise HTTPException(409, 'Сначала настройте выбранный движок анализа')
        if not slots.acquire(blocking=False):
            raise HTTPException(429, 'Очередь анализа заполнена, дождитесь завершения заданий')
        jid = uuid.uuid4().hex
        try:
            db().execute('INSERT INTO investigations(id,cluster_id,question,status,created_at,previous_id) VALUES (?,?,?,?,?,?)', (jid,body.cluster_id,body.question,'queued',now(),body.previous_id or ''))
            db().execute('UPDATE investigations SET context=? WHERE id=?',(json.dumps(connectors.redact_structure(resolved_context),ensure_ascii=False),jid))
            pool.submit(investigate,jid,body,item['config'],llm,resolved_context)
        except Exception:
            slots.release()
            raise
        return {'id':jid,'status':'queued'}

    class NewSession(BaseModel):
        cluster_id: str = Field(default='',max_length=48)
        mode: str = Field(default='problem',pattern='^(problem|chat)$')
        engine: str = Field(default='cluster',pattern='^(cluster|local)$')

    @api.post('/sessions',status_code=201)
    def new_session(body: NewSession):
        if body.cluster_id:cluster(body.cluster_id)
        if body.mode=='problem' and not body.cluster_id:raise HTTPException(422,'Выберите кластер')
        try:sid=sessions.create(body.cluster_id,body.mode,body.engine)
        except ValueError as e:raise HTTPException(429,str(e))
        return {'id':sid,'ttl_seconds':1800,'persistent':False}

    def session(sid):
        try:return sessions.get(sid)
        except SessionGone:raise HTTPException(404,'Диалог закрыт или истёк. Начните новый.')

    @api.delete('/sessions/{sid}')
    def close_session(sid: str):
        sessions.delete(sid)
        return {'ok':True}

    @api.get('/sessions/{sid}/jobs/{jid}')
    def session_job(sid: str,jid: str):
        session(sid)
        try:return sessions.job(sid,jid)
        except SessionGone:raise HTTPException(404,'Ответ недоступен')

    class SessionMessage(BaseModel):
        question: str = Field(min_length=3,max_length=4000)
        problem_id: Optional[str] = Field(default=None,max_length=64)
        metric: Optional[str] = Field(default=None,pattern='^(cpu|ram|disk)$')
        mode: str = Field(default='extended',pattern='^(brief|extended|causes|impact|commands)$')

    @api.post('/sessions/{sid}/messages',status_code=202)
    def session_message(sid: str, request_body: SessionMessage):
        current=session(sid);cid=current['cluster_id']
        cfg=dict(cluster(cid)['config']) if cid else {}
        if current['engine']=='local':
            cfg.pop('astra_ai',None);cfg.pop('holmes',None)
        body=Investigate(cluster_id=cid,session_id=sid,chat=current['mode']=='chat',**request_body.model_dump())
        if body.chat:body.problem_id=None;body.metric=None
        resolved={}
        if body.problem_id:
            resolved=next((i for i in problem_items(cid)[0] if i['id']==body.problem_id),None)
            if resolved is None:raise HTTPException(409,'Сигнал изменился. Обновите Обзор.')
            body.namespace=resolved.get('namespace') or None
            body.pod=resolved['name'] if resolved.get('kind')=='pods' else None
        if body.metric:resolved={'metric':body.metric,'cluster_id':cid}
        if current['jobs']:
            last=list(current['jobs'].values())[-1]
            if last['status']=='done' or (last['status']=='failed' and last.get('answer')):body.previous_id=last['id']
            if not resolved:
                resolved=json.loads(last.get('context') or '{}')
                body.namespace=resolved.get('namespace') or None
                body.pod=resolved.get('name') if resolved.get('kind')=='pods' else None
        llm=db().settings().get('llm',{});engine=selected_engine(cfg,llm)
        from .cluster_chat import inventory_intent
        if (not engine.get('url') or not engine.get('model')) and not (cid and not body.pod and not body.namespace and inventory_intent(body.question)):raise HTTPException(409,'Настройте выбранный движок ИИ')
        if not slots.acquire(blocking=False):raise HTTPException(429,'Очередь заполнена')
        jid=uuid.uuid4().hex
        try:
            job=dict(id=jid,cluster_id=cid,question=body.question,status='queued',answer='',error='',created_at=now(),previous_id=body.previous_id or '',context=json.dumps(connectors.redact_structure(resolved),ensure_ascii=False),trace='[]',evidence='{}')
            sessions.add(sid,job)
            pool.submit(investigate,jid,body,cfg,llm,resolved)
        except (ValueError,SessionGone) as e:
            slots.release();raise HTTPException(409,str(e) or 'Диалог закрыт')
        except Exception:
            slots.release();raise
        return {'id':jid,'status':'queued'}

    @api.get('/investigations')
    def list_investigations(cluster_id: str='',problem_id: str='',metric: str=''):
        scope(cluster_id)
        if metric and metric not in ('cpu','ram','disk'):
            raise HTTPException(422,'Неизвестная метрика')
        if len(problem_id)>64:
            raise HTTPException(422,'Некорректный ID проблемы')
        return {'items':db().rows('SELECT id,cluster_id,question,status,answer,error,created_at,previous_id,context,trace FROM investigations WHERE (?=\'\' OR cluster_id=?) AND (?=\'\' OR json_extract(context,\'$.id\')=?) AND (?=\'\' OR json_extract(context,\'$.metric\')=?) ORDER BY created_at DESC LIMIT 100', (cluster_id,cluster_id,problem_id,problem_id,metric,metric))}

    @api.get('/investigations/{jid}')
    def get_investigation(jid: str):
        rows = db().rows('SELECT * FROM investigations WHERE id=?', (jid,))
        if not rows:
            raise HTTPException(404, 'Задание не найдено')
        return rows[0]

    class Resolution(BaseModel):
        solution: str = Field(min_length=10,max_length=10000)

    @api.post('/investigations/{jid}/resolve')
    def resolve(jid: str, body: Resolution):
        job = get_investigation(jid)
        if job['status'] != 'done':
            raise HTTPException(409, 'Сначала завершите анализ')
        db().execute('INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?)', (jid,job['cluster_id'],job['question'],job['answer'],body.solution,now()))
        return {'ok':True,'id':jid}

    @api.get('/incidents')
    def incidents(cluster_id: str=''):
        scope(cluster_id)
        return {'items':db().rows('SELECT * FROM incidents WHERE (?=\'\' OR cluster_id=?) ORDER BY created_at DESC LIMIT 100', (cluster_id,cluster_id))}

    class HolmesBootstrap(BaseModel):
        port: int = Field(default=5051,ge=1024,le=65535)
        image: str = Field(default='us-central1-docker.pkg.dev/genuine-flight-317411/devel/holmes:0.45.0',min_length=3,max_length=300,pattern=r'^[A-Za-z0-9][A-Za-z0-9._/:@-]+$')

    @api.post('/clusters/{cid}/holmes-bootstrap')
    def bootstrap_holmes(cid: str, body: HolmesBootstrap, request: Request):
        from .holmes_bundle import write_bundle
        cfg=cluster(cid)['config'];original=json.dumps(cfg,sort_keys=True);llm=db().settings().get('llm',{})
        if not llm.get('url') or not llm.get('model'):
            raise HTTPException(409,'Сначала сохраните проверенный vLLM URL и model ID')
        if llm.get('ca_pem'):
            raise HTTPException(409,'Для vLLM с собственным CA сначала настройте доверенный CA в Holmes вручную')
        portal_port=8090
        if (runtime/'.env').exists():
            portal_port=int(next((line.split('=',1)[1] for line in (runtime/'.env').read_text().splitlines() if line.startswith('PULSE_PORT=')),'8090'))
        if body.port in (portal_port,8002):
            raise HTTPException(409,'Выберите отдельный порт для Holmes')
        if (runtime/'holmes'/cid).exists():
            raise HTTPException(409,'Конфигурация уже существует. Повторная генерация запрещена, чтобы не ротировать рабочие токены.')
        token=secrets.token_urlsafe(48);key=secrets.token_urlsafe(48)
        root=None
        try:
            with db().connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                current=connection.execute('SELECT config FROM clusters WHERE id=?',(cid,)).fetchone()
                if not current or json.dumps(db().decrypt(current['config']),sort_keys=True)!=original:
                    raise HTTPException(409,'Подключение изменилось. Повторите генерацию Holmes.')
                if (runtime/'holmes'/cid).exists():raise HTTPException(409,'Конфигурация Holmes уже существует.')
                root=write_bundle(runtime,cid,portal_port,body.port,body.image,llm,token,key)
                cfg['holmes_agent_token']=token
                cfg['holmes']={'enabled':False,'url':f'http://127.0.0.1:{body.port}','model':'pulse-local','api_key':key}
                connection.execute('UPDATE clusters SET config=? WHERE id=?',(db().encrypt(cfg),cid))
        except Exception:
            if root is not None:
                import shutil
                shutil.rmtree(root)
            raise
        # Container path differs from host path; the host-side CLI prints the real command.
        return {'ok':True,'relative_path':'holmes/'+cid,'port':body.port,'enabled':False,'note':'Запустите отдельный Holmes, проверьте ответ, затем включите режим в настройках кластера.'}

    def agent_config(cid,request):
        cfg=cluster(cid)['config']
        authorization=request.headers.get('authorization','')
        supplied=authorization[7:] if authorization.startswith('Bearer ') else ''
        expected=cfg.get('holmes_agent_token','')
        if not expected or not secrets.compare_digest(supplied.encode(),expected.encode()):
            raise HTTPException(401,'Нужен токен чтения выбранного кластера')
        return cfg

    @api.post('/clusters/{cid}/holmes-accept')
    def accept_holmes(cid: str):
        import time
        cfg=cluster(cid)['config'];settings=cfg.get('holmes',{})
        nonce=secrets.token_urlsafe(18)
        challenges=state.setdefault('holmes_challenges',{})
        if cid in challenges and challenges[cid][1]>time.monotonic():
            raise HTTPException(409,'Проверка уже выполняется')
        challenges[cid]=(nonce,time.monotonic()+180)
        portal_port=8090
        if (runtime/'.env').exists():
            portal_port=int(next((line.split('=',1)[1] for line in (runtime/'.env').read_text().splitlines() if line.startswith('PULSE_PORT=')),'8090'))
        try:
            answer,trace=connectors.holmes_chat(settings,[{'role':'system','content':'Используй только разрешённые read-only инструменты. Для проверки нужно получить настоящее значение из HTTP API.'},
                {'role':'user','content':f'Вызови GET http://127.0.0.1:{portal_port}/agent/{cid}/challenge через pulse-readonly. Верни полученное поле nonce. Его значение заранее неизвестно.'}])
            ok=nonce in answer
            return {'ok':ok,'tool_call_verified':ok,'answer':answer if ok else 'Holmes не вернул результат инструмента. Проверьте tool calling модели и конфигурацию tools.','tools':trace}
        except connectors.ConnectorError as e:
            raise HTTPException(502,str(e))
        finally:
            challenges.pop(cid,None)

    @app.get('/agent/{cid}/challenge')
    def agent_challenge(cid: str,request: Request):
        import time
        agent_config(cid,request)
        value=state.get('holmes_challenges',{}).get(cid)
        if not value or value[1]<time.monotonic():raise HTTPException(404,'Активной проверки нет')
        return {'nonce':value[0]}

    @app.get('/agent/{cid}/snapshot')
    def agent_snapshot(cid: str,request: Request,kind: str='',namespace: str='',name: str=''):
        agent_config(cid,request)
        snap=snapshot(cid)
        if not kind:
            result=dashboard.summarize(snap,cid,cluster(cid)['name'],interval)
            return {'observed_at':snap.get('observed_at'),'health':result['health'],'workloads':result['workloads']}
        if kind not in connectors.RESOURCE_PATHS:
            raise HTTPException(422,'Тип ресурса не разрешён')
        entry=snap.get('resources',{}).get(kind,{})
        items=[i for i in entry.get('items',[]) if (not namespace or i.get('metadata',{}).get('namespace')==namespace) and (not name or i.get('metadata',{}).get('name')==name)]
        return {'observed_at':snap.get('observed_at'),'items':items[:50],'error':entry.get('error'),'truncated':bool(entry.get('truncated')) or len(items)>50}

    @app.get('/agent/{cid}/logs')
    def agent_logs(cid: str,request: Request,namespace: str,pod: str,container: Optional[str]=None,previous: bool=False):
        cfg=agent_config(cid,request)
        try:return {'text':connectors.resource_logs(cfg['kube'],namespace,pod,container,previous)}
        except connectors.ConnectorError as e:raise HTTPException(502,str(e))

    @app.get('/agent/{cid}/metrics')
    def agent_metrics(cid: str,request: Request,metric: str='cpu'):
        cfg=agent_config(cid,request)
        try:return connectors.query_metrics(connectors.metrics_config(cfg),metric)
        except connectors.ConnectorError as e:raise HTTPException(502,str(e))

    @app.get('/agent/{cid}/diagnostics')
    def agent_diagnostics(cid: str,request: Request,namespace: str,pod: str,uid: str=''):
        cfg=agent_config(cid,request)
        from .diagnostics import collect_pod,model_evidence
        try:return model_evidence(collect_pod(cfg,namespace,pod,uid))
        except connectors.ConnectorError as e:raise HTTPException(502,str(e))

    @app.get('/agent/{cid}/astra-logs')
    def agent_astra(cid: str,request: Request,namespace: str,pod: str,start: Optional[str]=None,end: Optional[str]=None):
        cfg=agent_config(cid,request)
        try:return connectors.astra_logs(cfg.get('astra',{}),namespace,pod,start,end)
        except connectors.ConnectorError as e:raise HTTPException(502,str(e))

    @app.get('/agent/{cid}/documents')
    def agent_documents(cid: str,request: Request,q: str):
        agent_config(cid,request)
        rows=search_docs(q[:4000],cid)
        version=re.match(r'v(\d+\.\d+)',snapshot(cid).get('cluster_info',{}).get('server_version') or '')
        try:
            vectors,note=state['knowledge'].search(q[:4000],cid,kubernetes_version=version[1] if version else None)
        except connectors.ConnectorError as exc:vectors=[];note=str(exc)
        return {'items':connectors.redact_structure(rows+vectors),'vector_note':note}

    @app.get('/agent/{cid}/incidents')
    def agent_incidents(cid: str,request: Request):
        agent_config(cid,request)
        return {'items':db().rows('SELECT question,solution,created_at FROM incidents WHERE cluster_id=? ORDER BY created_at DESC LIMIT 10',(cid,))}

    app.include_router(api)
    return app


app = create_app()
