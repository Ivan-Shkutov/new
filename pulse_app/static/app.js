'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {page: 'overview', cluster: '', clusters: [], snapshot: null, overview: [], editingCluster: null, logs: null, resolving: null, previousId: null, loadSequence: 0, pollBusy: false};
try { state.cluster = localStorage.getItem('pulse.cluster') || ''; } catch (_) { /* Storage can be unavailable in private browser contexts. */ }
const titles = {overview: 'Обзор кластеров', resources: 'Ресурсы Kubernetes', investigations: 'Расследования', knowledge: 'База знаний', settings: 'Настройки'};
const typeNames = {pods: 'Pods', nodes: 'Nodes', namespaces: 'Namespaces'};

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}
function append(parent, ...children) { children.filter(Boolean).forEach(child => parent.append(child)); return parent; }
function clear(target) { const element = typeof target === 'string' ? $(target) : target; element.replaceChildren(); return element; }
function button(text, callback, style = 'secondary small') {
  const item = node('button', `button ${style}`, text); item.type = 'button'; item.addEventListener('click', callback); return item;
}
function date(value) { if (!value) return 'Ещё нет снимка'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleString('ru-RU'); }
function errorText(error) { return error instanceof Error ? error.message : String(error); }
function globalError(error) { const target = $('#global-error'); target.textContent = error ? errorText(error) : ''; target.classList.toggle('hidden', !error); }
function message(form, text, success = false) { const target = $('.form-message', form); if (target) { target.textContent = text; target.className = `form-message ${text ? (success ? 'success' : 'error') : ''}`; } }
function toast(text) { const target = $('#toast'); target.textContent = text; target.classList.remove('hidden'); clearTimeout(toast.timer); toast.timer = setTimeout(() => target.classList.add('hidden'), 4500); }
function empty(title, description, action) { return append(node('div', 'empty'), node('div', 'empty-symbol', '◫'), node('h2', '', title), node('p', '', description), action); }
function loading(target) { clear(target).append(node('div', 'loading', 'Загрузка данных…')); }
function notice(target, text, kind = 'info') { if (text) target.append(node('div', `notice ${kind}`, text)); }
function statusChip(status, enabled = true) {
  const normalized = String(status || '').toLowerCase();
  if (!enabled || normalized === 'paused') return node('span', 'chip', 'Приостановлен');
  if (['ok', 'healthy', 'ready', 'fresh', 'success', 'completed', 'done', 'resolved'].includes(normalized)) return node('span', 'chip good', {completed: 'Завершено', done: 'Завершено', resolved: 'Подтверждено'}[normalized] || 'Данные получены');
  if (['error', 'failed', 'unreachable'].includes(normalized)) return node('span', 'chip bad', 'Ошибка');
  if (['running', 'pending', 'queued'].includes(normalized)) return node('span', 'chip warning', {running: 'Выполняется', pending: 'Ожидание', queued: 'В очереди'}[normalized]);
  return node('span', 'chip warning', {stale: 'Устаревший снимок', partial: 'Неполные данные', degraded: 'Неполные данные', unknown: 'Нет данных', collecting: 'Сбор данных'}[normalized] || status || 'Нет данных');
}
async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  const method = options.method || 'GET';
  if (method !== 'GET' && method !== 'HEAD') headers['X-Pulse-Request'] = '1';
  let body = options.body;
  if (body !== undefined && !(body instanceof FormData)) { body = JSON.stringify(body); headers['Content-Type'] = 'application/json'; }
  const response = await fetch(`/api${path}`, {...options, method, headers, body, credentials: 'same-origin', cache: 'no-store'});
  let payload;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    let detail = payload.detail || `HTTP ${response.status}`;
    if (Array.isArray(detail)) detail = detail.map(item => `${(item.loc || []).join('.')}: ${item.msg || 'Ошибка значения'}`).join('\n');
    if (typeof detail === 'object') detail = JSON.stringify(detail);
    throw new Error(detail);
  }
  $('#connection-state').textContent = 'Сервер доступен';
  return payload;
}
async function busy(buttonElement, action) {
  if (buttonElement) buttonElement.disabled = true;
  try { return await action(); } finally { if (buttonElement) buttonElement.disabled = false; }
}
function requireCluster() {
  if (!state.cluster) throw new Error('Выберите конкретный кластер в левой панели.');
  return state.cluster;
}
function clusterPath(suffix = '') { return `/clusters/${encodeURIComponent(requireCluster())}${suffix}`; }
function currentClusterName() { return state.clusters.find(item => item.id === state.cluster)?.name || state.cluster; }
function setCluster(id) {
  state.cluster = id; state.snapshot = null; clearFollowup();
  try { localStorage.setItem('pulse.cluster', id); } catch (_) { /* Optional preference only. */ }
  $('#cluster-select').value = id;
}
async function loadClusters() {
  const result = await api('/clusters'); state.clusters = result.items || [];
  const select = clear('#cluster-select'); append(select, new Option('Все кластеры', ''));
  state.clusters.forEach(item => select.append(new Option(`${item.name}${item.enabled ? '' : ' · пауза'}`, item.id)));
  if (state.cluster && !state.clusters.some(item => item.id === state.cluster)) setCluster('');
  select.value = state.cluster;
}
async function navigate() {
  const page = location.hash.slice(1) || 'overview'; state.page = Object.hasOwn(titles, page) ? page : 'overview';
  $$('.page').forEach(item => item.classList.toggle('hidden', item.id !== `page-${state.page}`));
  $$('#navigation a').forEach(item => { const active = item.dataset.page === state.page; item.classList.toggle('active', active); if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current'); });
  $('#page-title').textContent = titles[state.page]; $('#breadcrumb').textContent = titles[state.page].toUpperCase();
  document.body.classList.toggle('compact-overview',state.page==='overview');
  await loadPage();
}
async function loadPage() {
  globalError(null); const sequence = ++state.loadSequence;
  try {
    if (state.page === 'overview') await loadOverview();
    else if (state.page === 'resources') await loadResources();
    else if (state.page === 'investigations') await loadInvestigations();
    else if (state.page === 'knowledge') await loadKnowledge();
    else if (state.page === 'settings') await loadSettings();
  } catch (error) { if (sequence === state.loadSequence) globalError(error); }
}
const workloadNames = {daemonsets:'DaemonSets',deployments:'Deployments',jobs:'Jobs',cronjobs:'CronJobs',replicasets:'ReplicaSets',statefulsets:'StatefulSets',pods:'Pods'};
let metricWindow = '1h';
function resourceView(cid, kind='pods') { setCluster(cid); $('#resource-type').value=kind; location.hash='resources'; if(state.page==='resources') loadPage(); }
function donut(percent, caption, tone='') {
  const box=node('div',`donut ${tone}`), svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 120 120'); svg.setAttribute('aria-hidden','true');
  const known=Number.isFinite(percent);
  for(const [cls,offset] of [['donut-track',0],['donut-value',known?339.292*(1-Math.min(100,Math.max(0,percent))/100):339.292]]) {
    const circle=document.createElementNS(svg.namespaceURI,'circle');
    for(const [k,v] of Object.entries({cx:60,cy:60,r:54,fill:'none','stroke-width':9,class:cls})) circle.setAttribute(k,v);
    if(cls==='donut-value'){circle.setAttribute('stroke-dasharray','339.292');circle.setAttribute('stroke-dashoffset',String(offset));}svg.append(circle);
  }
  box.setAttribute('role','img');box.setAttribute('aria-label',`${caption}: ${known?percent.toFixed(1)+'%':'нет данных'}`);
  box.append(svg,node('strong','donut-number',known?`${Math.round(percent)}%`:'—'));return box;
}
function workloadCard(kind, value, clusters) {
  const card=node('article','workload-card'), known=value?.total!==null && value?.total!==undefined;
  card.dataset.testid=`workload-${kind}`;
  card.append(append(node('div','workload-title'),node('h3','',workloadNames[kind]),node('span','metadata',known?`${value.positive} / ${value.total} ${value.label}`:'Нет данных')),
    donut(value?.percent,workloadNames[kind],value?.health==='critical'?'danger':value?.health==='warning'?'amber':value?.health==='unknown'?'muted-ring':''));
  card.classList.add('workload-'+(value?.health||'neutral'));
  card.append(healthBadge(value?.health||'neutral'));
  const foot=node('div','workload-foot');
  if(known && !value.total) foot.append(node('small','muted','Объектов нет'));
  if(value?.partial) foot.append(node('small','warning-text','Выборка неполная'));
  if(value?.stale) foot.append(node('small','warning-text','Проверьте время снимка'));
  if(value?.states) foot.append(node('small','muted',Object.entries(value.states).map(([k,v])=>`${stateLabels[k]||k}: ${v}`).join(' · ')));
  if(clusters.length===1) foot.append(button('Ресурсы →',()=>resourceView(clusters[0].id,kind)));
  card.append(foot);return card;
}
const healthLabels={healthy:'В норме',critical:'Проблема',warning:'Внимание',unknown:'Нет подтверждения',stale:'Устарело',paused:'Пауза',neutral:'Нет объектов'};
const stateLabels={ready:'Готовы',completed:'Завершены',completion_unverified:'Завершение не подтверждено',enabled:'Включены',active:'Выполняются',pending:'Ожидают',not_ready:'Не готовы',failed:'Ошибка',suspended:'Приостановлены',updating:'Обновляются',scaled_zero:'Реплик: 0',terminating:'Удаляются',unknown:'Неизвестно'};
function healthBadge(value){return node('span',`health-badge ${value}`,healthLabels[value]||'Нет данных');}
function ratio(ready,total){return Number.isFinite(total)&&Number.isFinite(ready)?`${ready}/${total}`:'—';}
function healthTile(title,value,hint,health,id,onClick){const tile=node('article',`stat signal-tile ${health||'unknown'}`);tile.dataset.testid=id;tile.append(append(node('div','signal-title'),node('span','',title),id==='scope'?node('span','health-badge','Kubernetes'):healthBadge(health)),node('strong','',value),node('small','',hint));if(onClick){const action=button('Подробнее →',onClick);tile.append(action);}return tile;}
function renderHealthBanner(target,data,clusters){const health=data.health||{status:'unknown',label:'Нет подтверждения'},banner=node('section',`health-banner ${health.status}`);banner.dataset.testid='cluster-health';
const heading=health.status==='healthy'?'Наблюдаемые объекты Kubernetes готовы':health.status==='critical'?'Есть проблемы — начните с красных карточек':health.status==='stale'?'Состояние изменилось? Сначала обновите данные':health.status==='paused'?'Сбор данных приостановлен':health.status==='warning'?'Есть состояния, требующие проверки':'Не хватает данных для оценки кластера';
banner.append(append(node('div'),node('div','eyebrow','СОСТОЯНИЕ KUBERNETES'),node('h2','',heading),node('p','',`${health.critical||0} критических сигналов · ${health.warning||0} предупреждений · автообновление 30 с`)),healthBadge(health.status));target.append(banner);
const urgent=(data.issues||[]).filter(i=>i.severity==='critical'&&!i.stale).slice(0,3);
if(urgent.length){const preview=node('div','urgent-preview');urgent.forEach(i=>preview.append(append(node('div'),node('strong','',i.title||i.state),node('span','',`${i.name} · ${i.namespace||i.cluster_name}`))));preview.append(button('Все сигналы ↓',()=>$('#problem-list')?.scrollIntoView({behavior:'smooth'})));target.append(preview);}
if(clusters.some(c=>c.health?.incomplete))notice(target,'Снимок неполный: часть типов ресурсов недоступна или достигнут лимит выборки. Неизвестное состояние не считается исправным.','warning');
}
function renderNodePanel(target,clusters){const panel=node('details','panel node-panel');panel.open=nodeDetailsOpen;panel.addEventListener('toggle',()=>{nodeDetailsOpen=panel.open;});panel.append(node('summary','','Состояние узлов — раскрыть детализацию'));panel.append(append(node('div','section-heading'),node('h2','','Узлы: готовность и давление ресурсов'),node('small','muted','Готовность, conditions и размещение Pod')));
const table=node('table'),head=node('tr');['Узел','Ready','Проблемы / обслуживание','Pods','Версия',''].forEach(t=>head.append(node('th','',t)));table.append(append(node('thead'),head));const body=node('tbody');let count=0;
for(const c of clusters)for(const n of c.health?.node_rows||[]){count++;const tr=node('tr');tr.dataset.testid='node-row';const tone=c.health.status==='stale'||c.health.status==='paused'?'unknown':n.status;tr.className=`node-${tone}`;const flags=node('td');if(n.problems.length)flags.append(node('span','',n.problems.join(' · ')));else flags.append(node('span','muted','Нет отмеченных conditions'));if(n.unschedulable)flags.append(node('span','chip warning','Cordon — новые Pod не назначаются'));tr.append(append(node('td'),node('strong','',n.name),node('small','metadata',`${c.name} · ${n.ip}`)),append(node('td'),healthBadge(tone)),flags,node('td','',n.pods??'—'),node('td','',n.version),append(node('td'),button('Узел →',()=>{resourceView(c.id,'nodes');$('#resource-search').value=n.name;})));body.append(tr);}
table.append(body);if(count)panel.append(append(node('div','table-wrap'),table));else notice(panel,'Информация об узлах ещё не получена. Проверьте сбор и права доступа.','warning');target.append(panel);}
function renderWarningEvents(target,clusters){const rows=clusters.flatMap(c=>(c.health?.events||[]).map(e=>({...e,cluster:c.name})));const details=node('details','panel warning-events');details.open=warningEventsOpen;details.addEventListener('toggle',()=>{warningEventsOpen=details.open;});details.append(node('summary','',`Warning-события за последние 15 минут · ${rows.length}`),node('p','muted','События — дополнительный контекст. Они не доказывают, что проблема продолжается сейчас.'));if(!rows.length)details.append(node('p','muted','В доступной выборке свежих Warning-событий нет.'));
for(const e of rows.slice(0,20)){details.append(append(node('div','event-row'),node('strong','',`${e.reason} · ${e.name}`),node('small','metadata',`${e.cluster} · ${e.namespace} · ${date(e.time)} · повторений: ${e.count}`),node('p','',e.message)));}target.append(details);}

let overviewRequest=0, issueFilter='all', warningEventsOpen=false, nodeDetailsOpen=false;
let problemQuery='', problemSeverity='all', problemOffset=0, problemRequest=0;
function activateCard(card,callback,label){card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label',label);card.addEventListener('click',callback);card.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();callback();}});}
function detailShell(title){const dialog=$('#dashboard-detail');$('#detail-title').textContent=title;clear('#detail-body');if(!dialog.open)dialog.showModal();return $('#detail-body');}
function selectedMetricCluster(){return state.cluster||(state.overview.length===1?state.overview[0].id:null);}
async function openResourceDetail(kind,name='',clusterId='',namespace=''){
 const cid=clusterId||selectedMetricCluster();if(!cid){toast('Выберите кластер');return;}
 const target=detailShell(`${kind==='nodes'?'Узлы':workloadNames[kind]||kind} · ${currentClusterName()||cid}`);loading(target);
 try{const snap=await api(`/clusters/${encodeURIComponent(cid)}/snapshot`);if(!target.isConnected)return;clear(target);const entry=snap.resources?.[kind];notice(target,`Снимок: ${date(snap.observed_at)}`);if(entry?.error)notice(target,entry.error,'warning');if(entry?.truncated)notice(target,'Выборка неполная','warning');const items=(entry?.items||[]).filter(i=>(!name||i.metadata?.name===name)&&(!namespace||i.metadata?.namespace===namespace));
 const table=node('table'),header=node('tr');['Объект','Namespace','Состояние','Детали'].forEach(t=>header.append(node('th','',t)));table.append(append(node('thead'),header));const body=node('tbody');for(const item of items){const row=node('tr');row.append(node('td','',item.metadata?.name),node('td','',item.metadata?.namespace||'—'),node('td','',resourceStatus(item,kind)),append(node('td'),button('JSON',()=>{const pre=node('pre','',JSON.stringify(item,null,2));const details=node('details');details.append(node('summary','','Статус и спецификация'),pre);row.lastChild.append(details);})));body.append(row);}table.append(body);target.append(append(node('div','table-wrap'),table));if(!items.length)notice(target,'Объектов в доступной выборке нет.');
 }catch(e){notice(clear(target),errorText(e),'error');}}
function metricValue(value,unit){const n=Number(value);if(value==null||!Number.isFinite(n))return 'Нет данных';if(String(unit).startsWith('bytes')){const units=['B','KiB','MiB','GiB','TiB'];let v=n,index=0;while(Math.abs(v)>=1024&&index<units.length-1){v/=1024;index++;}return `${v.toLocaleString('ru-RU',{maximumFractionDigits:2})} ${units[index]}`;}return n.toLocaleString('ru-RU',{maximumFractionDigits:3});}
async function openMetricDetail(metric){const cid=selectedMetricCluster();if(!cid){toast('Выберите кластер');return;}
 const target=detailShell(`${{cpu:'CPU',ram:'RAM',disk:'Диски'}[metric]} · ${currentClusterName()||cid}`);loading(target);
 const controls=node('div','toolbar');const seriesSearch=node('input');seriesSearch.type='search';seriesSearch.placeholder='Узел, Pod, mountpoint…';seriesSearch.setAttribute('aria-label','Поиск серий');seriesSearch.addEventListener('input',()=>{for(const row of target.querySelectorAll('.series-section tbody tr'))row.hidden=!row.textContent.toLowerCase().includes(seriesSearch.value.toLowerCase());});const range=node('select');range.setAttribute('aria-label','Диапазон подробного графика');for(const [v,t] of [['1h','1 час'],['6h','6 часов'],['24h','24 часа']])range.append(new Option(t,v));range.value=metricWindow;
 const load=async()=>{loading(target);try{const data=await api(`/clusters/${encodeURIComponent(cid)}/metrics-detail?metric=${metric}&window=${range.value}`);clear(target);target.append(controls);const holder=node('div','detail-metric-chart');renderDashboardMetrics(holder,{...data.summary,metrics:{[metric]:data.summary.metrics?.[metric]}},false);holder.querySelectorAll('.utilization-card').forEach(card=>{if(!card.dataset.metric||card.dataset.metric===metric)return;card.remove();});target.append(holder);target.append(node('h3','','Детализация по узлам и workloads'));
 for(const [name,payload] of Object.entries(data.series||{})){const section=node('section','series-section');section.append(node('h3','',`${name} · ${data.units?.[name]||''}`));if(payload.error)notice(section,payload.error,'warning');const rows=payload.data?.result||[];if(!rows.length)notice(section,'Серий нет: проверьте exporter и labels.');const table=node('table');table.append(append(node('thead'),append(node('tr'),node('th','','Метки источника'),node('th','','Значение'),node('th','','Время'))));const body=node('tbody');for(const row of rows.slice(0,500))body.append(append(node('tr'),node('td','',Object.entries(row.metric||{}).filter(([k])=>k!=='__name__').map(([k,v])=>`${k}=${v}`).join(' · ')),node('td','',metricValue(row.value?.[1],data.units?.[name])),node('td','',row.value?date(row.value[0]*1000):'—')));table.append(body);section.append(append(node('div','table-wrap'),table));if(rows.length>500)notice(section,'Показаны первые 500 серий. Уточните scope источника.','warning');target.append(section);}
 }catch(e){notice(clear(target),errorText(e),'error');}};
 controls.append(range,seriesSearch,button('✧ Спросить ИИ',()=>openAnalysis({cluster_id:cid,metric,title:`Утилизация ${metric.toUpperCase()}`})));range.addEventListener('change',load);await load();}
async function loadProblemTable(board){const request=++problemRequest;const list=$('.problem-table-body',board),count=$('.problem-count',board),pager=$('.problem-pager',board);if(!list)return;
 const params=new URLSearchParams({cluster_id:state.cluster,severity:problemSeverity,q:problemQuery,offset:problemOffset,limit:25});
 try{const result=await api('/problems?'+params);if(!board.isConnected||request!==problemRequest)return;clear(list);count.textContent=`${result.total}`;
 for(const item of result.items){const tr=node('tr',`problem-${item.severity}${item.stale?' historical':''}`);const ai=button('✧',()=>openAnalysis(item),'secondary ai-spark');ai.setAttribute('aria-label',`Анализ ИИ: ${item.name}`);ai.title='Разобрать с ИИ';tr.append(append(node('td'),ai),append(node('td'),node('span',`severity-dot ${item.severity}`),node('span','',item.severity==='critical'?'Критично':'Внимание')),append(node('td'),node('strong','',item.title),node('small','metadata',item.detail)),append(node('td'),button(item.name,()=>openResourceDetail(item.kind,item.name,item.cluster_id,item.namespace),'secondary small')),node('td','',`${item.cluster_name} / ${item.namespace||item.kind}`),node('td','',item.stale?'Старый снимок':date(item.observed_at)));list.append(tr);}
 if(!result.items.length)list.append(append(node('tr'),Object.assign(node('td','muted','Сигналов по выбранному фильтру нет'),{colSpan:6})));
 clear(pager);const prev=button('←',()=>{problemOffset=Math.max(0,problemOffset-25);loadProblemTable(board);});prev.disabled=problemOffset===0;const next=button('→',()=>{problemOffset+=25;loadProblemTable(board);});next.disabled=problemOffset+25>=result.total;pager.append(node('span','muted',`${result.total?problemOffset+1:0}–${Math.min(problemOffset+25,result.total)} из ${result.total}`),prev,next);const coverage=$('.problem-coverage',board);coverage.textContent=result.coverage.some(c=>c.incomplete||c.stale||!c.enabled)?'Есть неполные / устаревшие источники':'По доступным Kubernetes-наблюдениям';
 }catch(e){clear(list).append(append(node('tr'),Object.assign(node('td','critical-text',errorText(e)),{colSpan:6})));}}
async function loadOverview(){const request=++overviewRequest,selected=state.cluster,target=$('#overview-content');const data=await api('/dashboard'+(selected?`?cluster_id=${encodeURIComponent(selected)}`:''));if(request!==overviewRequest||selected!==state.cluster||state.page!=='overview')return;clear(target);state.overview=data.clusters||[];$('#version').textContent=data.version;
 if(!state.overview.length){target.append(empty('Подключите кластер','Добавьте reader kubeconfig.',button('+ Добавить',()=>openCluster())));return;}
 const board=node('div','compact-board');target.append(board);const h=data.health||{},nh=h.nodes||{},clusters=data.clusters;
 const bar=node('div','compact-status');bar.append(healthBadge(h.status||'unknown'),node('span','',clusters.length===1?clusters[0].name:`Кластеров: ${clusters.length}`),node('small','muted',`Снимок: ${date(clusters[0].observed_at)}`),button('Кластеры / источники',()=>{const detail=detailShell('Кластеры и источники');clusters.forEach(c=>detail.append(append(node('div','panel'),node('h3','',c.name),healthBadge(c.health?.status),node('p','',`Метрики ${c.sources.metrics?'настроены':'не подключены'} · Astra ${c.sources.astra?'настроена':'не подключена'}`),node('p','muted',c.error||date(c.observed_at)))));}));board.append(bar);
 const top=node('div','compact-top');const pr=clusters.reduce((n,c)=>n+(c.health?.pods?.healthy||0),0),pa=clusters.reduce((n,c)=>n+(c.health?.pods?.total||0),0),pk=clusters.every(c=>c.health?.pods?.total!=null);const pt=clusters.some(c=>['unknown','stale','paused'].includes(c.health?.status)||c.health?.pods?.health==='unknown')?'unknown':clusters.some(c=>c.health?.pods?.health==='critical')?'critical':pr<pa?'warning':pa?'healthy':'neutral';
 top.append(healthTile('Узлы Ready',ratio(nh.ready,nh.total),'',nh.health||'unknown','nodes-ready',()=>openResourceDetail('nodes')),healthTile('Pods здоровы',pk?ratio(pr,pa):'—','Ready + проверенные Completed / все',pt,'pods-ready',()=>openResourceDetail('pods')),healthTile('Критические сигналы',h.critical??'—',`Предупреждений: ${h.warning||0}`,['stale','unknown','paused'].includes(h.status)?'unknown':h.critical?'critical':h.warning?'warning':'healthy','critical-signals',()=>{problemSeverity=h.critical?'critical':'warning';$('.problem-severity',board).value=problemSeverity;problemOffset=0;loadProblemTable(board);}));board.append(top);
 const util=node('section','compact-util');const heading=append(node('div','compact-heading'),node('h2','','Утилизация ресурсов'));const range=node('select');range.setAttribute('aria-label','Интервал графиков');for(const [v,t] of [['1h','1 час'],['6h','6 часов'],['24h','24 часа']])range.append(new Option(t,v));range.value=metricWindow;range.addEventListener('change',()=>{metricWindow=range.value;loadOverview();});heading.append(range);util.append(heading);const mt=node('div');mt.id='dashboard-metrics';util.append(mt);board.append(util);
 const cid=selectedMetricCluster();if(!cid)renderDashboardMetrics(mt,{configured:false,metrics:{},note:'Выберите кластер'});else api(`/clusters/${encodeURIComponent(cid)}/dashboard-metrics?window=${metricWindow}`).then(result=>{if(mt.isConnected)renderDashboardMetrics(mt,result);}).catch(e=>{if(mt.isConnected)renderDashboardMetrics(mt,{configured:false,metrics:{},error:errorText(e)});});
 const section=node('section','compact-workloads');section.append(append(node('div','compact-heading'),node('h2','','Состояние workloads'),node('small','muted','Готовность · завершение · расписания')));const cards=node('div','workload-grid');for(const kind of Object.keys(workloadNames)){const card=workloadCard(kind,data.workloads[kind],clusters);card.title=Object.entries(data.workloads[kind]?.states||{}).map(([k,v])=>`${stateLabels[k]||k}: ${v}`).join(' · ');activateCard(card,()=>openResourceDetail(kind),`${workloadNames[kind]}: открыть подробности`);cards.append(card);}section.append(cards);board.append(section);
 const problems=node('section','compact-problems panel');const toolbar=node('div','problem-toolbar');toolbar.append(node('h2','','Проблемы'),node('span','chip problem-count','…'));const search=node('input');search.type='search';search.placeholder='Объект, namespace, причина';search.setAttribute('aria-label','Поиск проблем');search.value=problemQuery;let timer;search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{problemQuery=search.value;problemOffset=0;loadProblemTable(board);},250);});const severity=node('select','problem-severity');severity.setAttribute('aria-label','Фильтр проблем');for(const [v,t] of [['all','Все уровни'],['critical','Критические'],['warning','Предупреждения']])severity.append(new Option(t,v));severity.value=problemSeverity;severity.addEventListener('change',()=>{problemSeverity=severity.value;problemOffset=0;loadProblemTable(board);});toolbar.append(search,severity);problems.append(toolbar);
 const table=node('table','problem-table'),tr=node('tr');['ИИ','Уровень','Сигнал','Объект','Кластер / область','Наблюдение'].forEach(t=>tr.append(node('th','',t)));table.append(append(node('thead'),tr),node('tbody','problem-table-body'));problems.append(append(node('div','problem-scroll'),table));const foot=node('div','problem-footer');foot.append(node('small','muted problem-coverage',''),node('div','problem-pager'));problems.append(foot);board.append(problems);loadProblemTable(board);
}

function renderDashboardMetrics(target,result,interactive=true) {
  clear(target);if(result.error)notice(target,result.error,'warning');
  if(!result.configured)notice(target,result.note||'Подключите Prometheus / VictoriaMetrics. Значения утилизации пока неизвестны.');
  const grid=node('div','utilization-grid');
  for(const [key,title,hint] of [['cpu','CPU','Non-idle CPU узлов'],['ram','RAM','Total − available'],['disk','Диски /','Корневая файловая система узлов; не PVC']]) {
    const value=result.metrics?.[key],card=node('article','panel utilization-card');card.dataset.testid=`utilization-${key}`;
    const stale=value?.observed_at && Date.now()/1000-value.observed_at>120;
    card.append(append(node('div','utilization-head'),append(node('div'),node('h3','',title),node('small','muted',hint)),donut(value?.percent,title,stale?'amber':value?.percent>=90?'danger':value?.percent>=75?'amber':'')));
    card.classList.add(!Number.isFinite(value?.percent)||stale?'metric-unknown':value.percent>=90?'metric-critical':value.percent>=75?'metric-warning':'metric-healthy');
    if(Number.isFinite(value?.percent)&&!stale&&value.percent>=75)card.append(node('p',value.percent>=90?'critical-text':'warning-text',value.percent>=90?'Высокая утилизация — проверьте запас ресурсов':'Утилизация выше 75% — следите за запасом'));
    if(!Number.isFinite(value?.percent)){const reason=value?.error||result.error||result.note||'Нет данных';const text=node('p','muted metric-status',reason);text.title=reason;card.append(text);}
    if(value?.error)notice(card,value.error,'warning');
    if(stale)notice(card,'Значение метрики устарело','warning');
    if(value?.observed_at)card.append(node('small','metadata',`Источник: ${date(value.observed_at*1000)}`));
    const points=value?.history||[];
    if(points.length>1){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 400 110');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');svg.setAttribute('aria-label',`${title}: история, шкала 0–100%`);svg.classList.add('history-chart');
      for(const y of [5,55,105]){const line=document.createElementNS(svg.namespaceURI,'line');for(const [k,v] of Object.entries({x1:0,x2:400,y1:y,y2:y,stroke:'#2b4054'}))line.setAttribute(k,v);svg.append(line);}
      let segment=[];const flush=()=>{if(segment.length>1){const poly=document.createElementNS(svg.namespaceURI,'polyline');poly.setAttribute('points',segment.join(' '));poly.setAttribute('fill','none');poly.setAttribute('stroke',key==='cpu'?'#27d6b5':key==='ram'?'#71a6ff':'#b69af7');poly.setAttribute('stroke-width','2.5');svg.append(poly);}segment=[];};let prior=null;
      for(const [ts,v] of points){if(prior!==null && ts-prior>result.step*1.5)flush();segment.push(`${400*(ts-result.start)/(result.end-result.start)},${105-v}`);prior=ts;}flush();card.append(svg,node('small','metadata',`${result.window||metricWindow} · шкала 0–100% · пропуски не соединяются`));
    }else card.append(node('div','history-empty',value?.history_error||'История пока недоступна'));
    card.dataset.metric=key;if(interactive)activateCard(card,()=>openMetricDetail(key),`${title}: подробный dashboard`);grid.append(card);
  }
  target.append(grid);if(result.note)target.append(node('p','inline-help',result.note));
}

async function loadResources() {
  $('#collect-now').disabled = !state.cluster;
  clear('#resource-notice'); clear('#metric-content');
  if (!state.cluster) { $('#snapshot-time').textContent = 'Выберите кластер для просмотра снимка.'; clear('#resource-table').append(empty('Выберите кластер', 'Ресурсы и логи всегда относятся к одному выбранному кластеру.')); return; }
  loading('#resource-table'); state.snapshot = null;
  const selected = state.cluster;
  const data = await api(clusterPath('/snapshot'));
  if (state.cluster !== selected) return;
  state.snapshot = data;
  $('#snapshot-time').textContent = `${currentClusterName()} · ${date(data.observed_at)}`;
  renderResources();
  try { const metrics = await api(clusterPath('/metrics')); if (selected === state.cluster) renderMetrics(metrics); } catch (error) { if (selected === state.cluster) notice($('#metric-content'), `Метрики: ${errorText(error)}`, 'warning'); }
}
function podStatus(item) {
  const status = item.status || {}, containers = status.containerStatuses || [];
  const problem = containers.map(c => c.state?.waiting?.reason || c.state?.terminated?.reason).find(Boolean);
  const ready = containers.length ? `${containers.filter(c => c.ready).length}/${containers.length} Ready` : 'Ready неизвестно';
  return `${problem || status.phase || '—'} · ${ready}`;
}
function resourceStatus(item, type) {
  if (type === 'pods') return podStatus(item);
  if (type === 'nodes') { const condition = item.status?.conditions?.find(c => c.type === 'Ready'); return condition ? (condition.status === 'True' ? 'Ready' : 'NotReady') : 'Неизвестно'; }
  if (type === 'events') return `${item.type || 'Event'} · ${item.reason || ''}`;
  if(type==='jobs'){const conditions=item.status?.conditions||[];if(conditions.some(c=>c.type==='Complete'&&c.status==='True'))return 'Завершено';if(conditions.some(c=>c.type==='Failed'&&c.status==='True'))return 'Ошибка';if(item.spec?.suspend)return 'Приостановлено';return item.status?.active?'Выполняется':'Ожидание';}
  if(type==='cronjobs')return item.spec?.suspend?'Приостановлено':'Расписание включено';
  if(type==='daemonsets')return `${item.status?.numberReady||0}/${item.status?.desiredNumberScheduled??'—'} Ready`;
  if(item.spec?.replicas===0)return 'Реплик: 0';
  if (item.status?.phase) return item.status.phase;
  if (item.spec?.replicas !== undefined || item.status?.replicas !== undefined) return `${item.status?.readyReplicas || 0}/${item.spec?.replicas ?? item.status?.replicas ?? 0} Ready`;
  return '—';
}
function renderResources() {
  const target = clear('#resource-table'), notices = clear('#resource-notice');
  if (!state.snapshot) return;
  const type = $('#resource-type').value, query = $('#resource-search').value.trim().toLowerCase();
  const resources = state.snapshot.resources || {}, resource = resources[type];
  if (!state.snapshot.observed_at) notice(notices, 'Первичный сбор ещё не завершён. Нажмите «Собрать сейчас» или дождитесь фонового обновления.', 'info');
  if (state.snapshot.observed_at && Date.now() - new Date(state.snapshot.observed_at).valueOf() > 120000) notice(notices, 'Снимок старше двух минут. Проверьте время сбора и доступность источника.', 'warning');
  if (!resource) { target.append(empty('Нет данных этого типа', 'Снимок ещё не получен или ресурс не собран. Это не подтверждает отсутствие объектов.')); return; }
  if (resource.error) notice(notices, resource.error, 'error');
  if (resource.truncated) notice(notices, 'Достигнут предел выборки. Список неполный.', 'warning');
  const all = Array.isArray(resource) ? resource : resource.items || [];
  const items = all.filter(item => `${item.metadata?.name || ''} ${item.metadata?.namespace || ''}`.toLowerCase().includes(query));
  target.append(node('p', 'table-meta', `Показано ${items.length} из ${all.length} полученных объектов`));
  if (!items.length) { target.append(empty(query ? 'Ничего не найдено' : 'Объектов в выборке нет', query ? 'Измените строку поиска.' : resource.error ? 'Источник вернул ошибку. Пустая выборка не является подтверждением отсутствия объектов.' : 'Проверьте права доступа, полноту сбора и выбранный тип ресурса.')); return; }
  const table = node('table'), thead = node('thead'), header = node('tr'); ['Имя', 'Namespace', 'Состояние', 'Действия'].forEach(name => header.append(node('th', '', name))); thead.append(header); table.append(thead);
  const tbody = node('tbody');
  items.forEach(item => {
    const row = node('tr'); row.append(node('td', 'resource-name', item.metadata?.name || '—'), node('td', '', item.metadata?.namespace || '—'), node('td', '', resourceStatus(item, type)));
    const actions = node('div', 'table-actions');
    actions.append(button('JSON', () => { $('#object-title').textContent = item.metadata?.name || type; $('#object-json').textContent = JSON.stringify(item, null, 2); $('#object-dialog').showModal(); }));
    if (type === 'pods') actions.append(button('Логи', () => openLogs(item)), button('Почему?', () => { clearFollowup(); const form = $('#investigation-form'); form.elements.namespace.value = item.metadata?.namespace || ''; form.elements.pod.value = item.metadata?.name || ''; form.elements.question.value = `Объясни состояние Pod ${item.metadata?.name || ''}. Какие факты указывают на проблему и что проверить?`; location.hash = 'investigations'; }));
    row.append(append(node('td'), actions)); tbody.append(row);
  }); table.append(tbody); target.append(append(node('div', 'table-wrap'), table));
}
function renderMetrics(data) {
  const target = clear('#metric-content'); if (!data || !Object.keys(data).length) return;
  target.append(node('h2', '', 'Метрики источника'));
  if (data.error) { notice(target, data.error, 'warning'); return; }
  const grid = node('div', 'metric-grid');
  Object.entries(data.metrics || data).forEach(([name, payload]) => {
    if (['configured', 'observed_at', 'errors'].includes(name)) return;
    const panel = node('div', 'panel'); panel.append(node('h3', '', {cpu: 'CPU', memory: 'RAM'}[name] || name));
    if (payload?.error) notice(panel, payload.error, 'warning');
    else {
      const results = payload?.data?.result || payload?.result;
      if (Array.isArray(results)) {
        if (!results.length) panel.append(node('p', 'muted', 'Нет данных'));
        results.slice(0, 50).forEach(result => panel.append(node('div', 'metric-value', `${Object.entries(result.metric || {}).map(([k, v]) => `${k}=${v}`).join(', ') || 'Значение'}: ${result.value?.[1] ?? 'нет данных'}`)));
        if (results.length > 50) panel.append(node('p', 'muted', `Показаны первые 50 из ${results.length} серий.`));
      } else panel.append(node('div', 'metric-value', typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)));
    }
    grid.append(panel);
  }); target.append(grid);
}
function openLogs(pod) {
  state.logs = {namespace: pod.metadata?.namespace || 'default', pod: pod.metadata?.name || ''};
  $('#logs-title').textContent = `Логи · ${state.logs.pod}`;
  const select = clear($('#logs-form').elements.container);
  [...(pod.spec?.containers || []), ...(pod.spec?.initContainers || [])].forEach(container => select.append(new Option(container.name, container.name)));
  if (!select.options.length) select.append(new Option('По умолчанию', ''));
  $('#logs-form').elements.previous.checked = false; $('#logs-notice').textContent = ''; $('#logs-output').textContent = 'Нажмите «Получить логи». Логи могут содержать конфиденциальные данные.'; $('#logs-dialog').showModal();
}
async function loadInvestigations() {
  const target = clear('#investigation-list');
  if (!state.cluster) { target.append(empty('Выберите кластер', 'Контекст расследования ограничивается выбранным кластером.')); return; }
  const selected = state.cluster, data = await api(`/investigations?cluster_id=${encodeURIComponent(selected)}`);
  if (state.cluster !== selected) return;
  if (!(data.items || []).length) { target.append(empty('Расследований пока нет', 'Задайте вопрос о состоянии инфраструктуры или откройте анализ из карточки Pod.')); return; }
  (data.items || []).forEach(item => {
    const card = node('article', 'item-card'); card.dataset.testid = 'investigation'; card.dataset.status = item.status;
    card.append(append(node('div', 'section-heading'), node('h3', '', item.question), statusChip(item.status)));
    card.append(node('div', 'metadata', `${date(item.created_at)} · ${item.namespace || 'Кластер'}${item.pod ? ` / ${item.pod}` : ''}`));
    if (item.answer) card.append(node('div', 'answer', item.answer));
    if (item.error) notice(card, item.error, 'error');
    if (['queued', 'pending', 'running'].includes(item.status)) card.append(node('p', 'muted', 'Задание сохранено. Можно закрыть страницу — результат останется в истории.'));
    if (item.answer) { card.append(button('Продолжить обсуждение', () => { state.previousId = item.id; const context = $('#followup-context'); $('span', context).textContent = `Продолжение: ${item.question}`; context.classList.remove('hidden'); const form = $('#investigation-form'); form.elements.namespace.value = item.namespace || ''; form.elements.pod.value = item.pod || ''; form.elements.question.value = ''; form.elements.question.focus(); form.scrollIntoView({behavior: 'smooth', block: 'start'}); })); const action = button('Записать проверенное решение', () => { state.resolving = item.id; $('#resolve-form').reset(); message($('#resolve-form'), ''); $('#resolve-dialog').showModal(); }); action.style.marginTop = '16px'; card.append(action); }
    target.append(card);
  });
}
async function loadKnowledge(query = '') {
  const target = clear('#document-list'); const scope = encodeURIComponent(state.cluster);
  const data = await api(query ? `/documents/search?q=${encodeURIComponent(query)}&cluster_id=${scope}` : `/documents?cluster_id=${scope}`);
  if (!(data.items || []).length) target.append(empty(query ? 'Совпадений нет' : 'Добавьте первый документ', query ? 'Попробуйте точное имя сервиса или фрагмент ошибки. Лексический поиск не гарантирует перевод между языками.' : 'Загрузите описание приложения и инструкции. Общие документы доступны при выборе любого кластера.'));
  (data.items || []).forEach(item => {
    const card = node('article', 'item-card');
    card.append(append(node('div', 'section-heading'), node('h3', '', item.name || item.filename || 'Документ'), node('span', 'chip', item.cluster_id || 'Общий')));
    if (item.created_at) card.append(node('div', 'metadata', date(item.created_at)));
    if (item.excerpt || item.snippet) card.append(node('p', 'answer', item.excerpt || item.snippet));
    card.append(button('Удалить документ', async () => { if (!confirm(`Удалить документ «${item.name || item.filename}» из базы знаний?`)) return; try { await api(`/documents/${encodeURIComponent(item.id)}`, {method: 'DELETE'}); toast('Документ удалён'); await loadKnowledge(); } catch (error) { globalError(error); } }, 'danger small')); target.append(card);
  });
  const incidents = clear('#incident-list');
  if (!state.cluster) { incidents.append(node('p', 'muted', 'Выберите кластер, чтобы увидеть его подтверждённые решения.')); return; }
  const result = await api(`/incidents?cluster_id=${scope}`);
  if (!(result.items || []).length) incidents.append(node('p', 'muted', 'Подтверждённых решений пока нет. Их можно сохранить из завершённого расследования.'));
  (result.items || []).forEach(item => incidents.append(append(node('article', 'item-card'), node('h3', '', item.question || item.title || 'Решение инцидента'), node('div', 'metadata', date(item.created_at)), node('div', 'answer', item.solution || item.answer || ''))));
}
async function loadSettings() {
  const settings = await api('/settings'); const form = $('#llm-form');
  form.elements.url.value = settings.llm?.url || ''; form.elements.model.value = settings.llm?.model || ''; form.elements.api_key.value = '';
  $('#llm-key-state').textContent = settings.llm?.has_key ? 'API key уже сохранён. Значение не возвращается в браузер.' : 'API key не задан. Для локального сервера без авторизации он не требуется.';
  await loadClusters(); const target = clear('#cluster-settings-list');
  if (!state.clusters.length) { target.append(node('p', 'muted', 'Подключений пока нет.')); return; }
  state.clusters.forEach(cluster => {
    const content = append(node('div'), node('h3', '', cluster.name), node('small', '', `${cluster.id} · ${cluster.enabled ? 'сбор включён' : 'пауза'} · метрики ${cluster.has_metrics ? '✓' : '—'} · Astra ${cluster.has_astra ? '✓' : '—'} · ИИ ${cluster.astra_ai?.enabled?'Astra AI':cluster.holmes?.enabled?'HolmesGPT':'локальная LLM'}`));
    const actions = node('div', 'actions');
    actions.append(button('Настроить', () => openCluster(cluster)), button('Удалить', async () => { if (!confirm(`Удалить подключение «${cluster.name}» и связанные данные? Сохраните необходимые сведения перед удалением.`)) return; try { await api(`/clusters/${encodeURIComponent(cluster.id)}`, {method: 'DELETE'}); await loadSettings(); toast('Кластер удалён'); } catch (error) { globalError(error); } }, 'danger small'));
    target.append(append(node('div', 'setting-item'), content, actions));
  });
}
function openCluster(cluster = null) {
  state.editingCluster = cluster?.id || null;
  const form = $('#cluster-form'); form.reset(); message(form, '');
  form.elements.id.value = cluster?.id || ''; form.elements.id.disabled = !!cluster;
  form.elements.name.value = cluster?.name || ''; form.elements.enabled.checked = cluster ? !!cluster.enabled : true;
  form.elements.kubeconfig.required = !cluster;
  form.elements.holmes_enabled.checked=!!cluster?.holmes?.enabled;form.elements.holmes_url.value=cluster?.holmes?.url||'';form.elements.holmes_model.value=cluster?.holmes?.model||'pulse-local';$('#holmes-message').textContent='';$('#holmes-bootstrap').disabled=!cluster;$('#holmes-test').disabled=!cluster;$('#holmes-accept').disabled=!cluster;
  form.elements.astra_ai_enabled.checked=!!cluster?.astra_ai?.enabled;form.elements.astra_ai_url.value=cluster?.astra_ai?.url||'';form.elements.astra_ai_model.value=cluster?.astra_ai?.model||'qwen';$('#astra-ai-state').textContent=cluster?.astra_ai?.has_key?'Токен сохранён. Значение не возвращается.':'Токен не сохранён.';$('#astra-ai-message').textContent='';$('#astra-ai-test').disabled=!cluster;
  const metrics=cluster?.metrics||{};form.elements.metrics_transport.value=metrics.transport||'direct';for(const key of ['url','namespace','service','port','mode'])if(metrics[key]!=null)form.elements['metrics_'+key].value=metrics[key];form.elements.metrics_labels.value=metrics.labels?JSON.stringify(metrics.labels):'';$('#metrics-message').textContent='';clear('#metrics-candidates').append(new Option('Выберите после поиска',''));$('#metrics-discover').disabled=!cluster;$('#metrics-test').disabled=!cluster;
  $('#cluster-dialog-title').textContent = cluster ? `Настроить · ${cluster.name}` : 'Добавить кластер';
  $('#cluster-dialog').showModal();
}
function integrationSettings(form, prefix) {
  const url = form.elements[`${prefix}_url`].value.trim(); const transport=prefix==='metrics'?form.elements.metrics_transport.value:'direct';if (!url&&transport!=='kubernetes') return undefined;
  const settings = {url};if(prefix==='metrics'){settings.transport=transport;if(transport==='kubernetes'){settings.namespace=form.elements.metrics_namespace.value.trim();settings.service=form.elements.metrics_service.value.trim();settings.port=Number(form.elements.metrics_port.value);}}
  (prefix === 'metrics' ? ['token', 'username', 'password'] : ['token']).forEach(key => { const value = form.elements[`${prefix}_${key}`].value; if (value) settings[key] = value; });
  if (form.elements[`${prefix}_ca`].value.trim()) settings.ca_pem = form.elements[`${prefix}_ca`].value.trim();
  if (prefix === 'metrics') {
    const raw = form.elements.metrics_labels.value.trim(); const labels = raw ? JSON.parse(raw) : {};
    if (!labels || Array.isArray(labels) || typeof labels !== 'object' || Object.values(labels).some(value => typeof value !== 'string')) throw new Error('Метки метрик должны быть JSON-объектом со строковыми значениями.');
    settings.labels = labels; settings.mode = form.elements.metrics_mode.value;
    if (settings.mode === 'shared' && !Object.keys(labels).length) throw new Error('Для общего источника метрик укажите метки кластера.');
  } else {
    ['cluster_field', 'cluster_value', 'namespace_field', 'pod_field'].forEach(key => { const value = form.elements[`astra_${key}`].value.trim(); if (value) settings[key] = value; });
    if (!settings.cluster_field || !settings.cluster_value) throw new Error('Для Astra укажите точное поле и значение кластера.');
  }
  return settings;
}

function clearFollowup() { state.previousId = null; $('#followup-context').classList.add('hidden'); }
$('#clear-followup').addEventListener('click', clearFollowup);
$('#cluster-select').addEventListener('change', event => { setCluster(event.target.value); loadPage(); });
$('#refresh-page').addEventListener('click', event => busy(event.currentTarget, async () => { try { await loadClusters(); await loadPage(); } catch (error) { globalError(error); } }));
$('#resource-type').addEventListener('change', renderResources);
$('#resource-search').addEventListener('input', renderResources);
$('#collect-now').addEventListener('click', event => busy(event.currentTarget, async () => { try { await api(clusterPath('/refresh'), {method: 'POST'}); await loadResources(); toast('Сбор завершён. Проверьте ошибки отдельных источников.'); } catch (error) { globalError(error); } }));
$('#add-cluster').addEventListener('click', () => openCluster());
$$('[data-close]').forEach(item => item.addEventListener('click', () => item.closest('dialog').close()));
$('#kubeconfig-file').addEventListener('change', async event => { const file = event.target.files?.[0]; if (!file) return; if (file.size > 1024 * 1024) { message($('#cluster-form'), 'Kubeconfig превышает 1 MiB.'); return; } $('#cluster-form').elements.kubeconfig.value = await file.text(); });
$('#cluster-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('[type=submit]', form), async () => {
    message(form, '');
    try {
      const body = {name: form.elements.name.value.trim(), enabled: form.elements.enabled.checked};
      if (!state.editingCluster) body.id = form.elements.id.value.trim();
      if (form.elements.kubeconfig.value.trim()) body.kubeconfig = form.elements.kubeconfig.value.trim();
      const metrics = integrationSettings(form, 'metrics'), astra = integrationSettings(form, 'astra');
      if (metrics) body.metrics = metrics; if (astra) body.astra = astra;
      body.holmes={enabled:form.elements.holmes_enabled.checked,url:form.elements.holmes_url.value.trim(),model:form.elements.holmes_model.value.trim()};if(form.elements.holmes_api_key.value)body.holmes.api_key=form.elements.holmes_api_key.value;
      body.astra_ai={enabled:form.elements.astra_ai_enabled.checked,url:form.elements.astra_ai_url.value.trim(),model:form.elements.astra_ai_model.value.trim()};if(form.elements.astra_ai_api_key.value)body.astra_ai.api_key=form.elements.astra_ai_api_key.value;
      if(body.holmes.enabled&&(!body.holmes.url||!body.holmes.model))throw new Error('Для Holmes нужны URL и model alias');
      await api(state.editingCluster ? `/clusters/${encodeURIComponent(state.editingCluster)}` : '/clusters', {method: state.editingCluster ? 'PUT' : 'POST', body});
      $('#cluster-dialog').close(); await loadClusters(); await loadPage(); toast('Подключение сохранено. Получение данных зависит от доступности источников.');
    } catch (error) { message(form, errorText(error)); }
  });
});
$('#llm-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('[type=submit]', form), async () => { try { const llm = {url: form.elements.url.value.trim(), model: form.elements.model.value.trim()}; if (form.elements.api_key.value) llm.api_key = form.elements.api_key.value; await api('/settings', {method: 'PUT', body: {llm}}); form.elements.api_key.value = ''; message(form, 'Настройки сохранены.', true); } catch (error) { message(form, errorText(error)); } });
});
$('#test-llm').addEventListener('click', event => busy(event.currentTarget, async () => { const form = $('#llm-form'); message(form, 'Проверяем сохранённые настройки…', true); try { const result = await api('/settings/test', {method: 'POST'}); message(form, result.ok ? `Модель ответила: ${result.answer || 'OK'}` : result.error || 'Проверка не пройдена.', !!result.ok); } catch (error) { message(form, errorText(error)); } }));
$('#logs-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('[type=submit]', form), async () => { $('#logs-notice').textContent = 'Получение логов…'; $('#logs-output').textContent = ''; try { const params = new URLSearchParams({...state.logs, container: form.elements.container.value, source: form.elements.source.value, previous: String(form.elements.previous.checked)}); const result = await api(clusterPath(`/logs?${params}`)); $('#logs-output').textContent = result.text || (Array.isArray(result.items) ? result.items.map(item => typeof item === 'string' ? item : JSON.stringify(item)).join('\n') : '') || 'В выбранном интервале строки не получены.'; $('#logs-notice').textContent = result.notice || (result.incomplete || result.truncated ? 'Выборка ограничена; отсутствие строк не доказывает отсутствие событий.' : 'Полученная выборка. Проверьте время и выбранный источник.'); } catch (error) { $('#logs-notice').textContent = errorText(error); } });
});
$('#investigation-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('[type=submit]', form), async () => { try { const body = {cluster_id: requireCluster(), question: form.elements.question.value.trim()}; if (state.previousId) body.previous_id = state.previousId; ['namespace', 'pod'].forEach(key => { if (form.elements[key].value.trim()) body[key] = form.elements[key].value.trim(); }); await api('/investigations', {method: 'POST', body}); message(form, 'Расследование поставлено в очередь.', true); await loadInvestigations(); } catch (error) { message(form, errorText(error)); } });
});
$('#resolve-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('[type=submit]', form), async () => { try { await api(`/investigations/${encodeURIComponent(state.resolving)}/resolve`, {method: 'POST', body: {solution: form.elements.solution.value.trim()}}); $('#resolve-dialog').close(); await loadInvestigations(); toast('Проверенное решение сохранено в базе знаний.'); } catch (error) { message(form, errorText(error)); } });
});
$('#document-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('[type=submit]', form), async () => { try { const cluster = form.elements.scope.value === 'global' ? '' : requireCluster(); const body = new FormData(); const file = form.elements.file.files[0]; if (!file) throw new Error('Выберите документ.'); body.append('file', file); body.append('cluster_id', cluster); await api('/documents', {method: 'POST', body}); form.elements.file.value = ''; message(form, 'Документ загружен. Проверьте его через поиск.', true); await loadKnowledge(); } catch (error) { message(form, errorText(error)); } });
});
$('#document-search-form').addEventListener('submit', event => { event.preventDefault(); busy($('[type=submit]', event.currentTarget), async () => { try { await loadKnowledge($('#document-search-form').elements.q.value.trim()); } catch (error) { globalError(error); } }); });
window.addEventListener('hashchange', navigate);
setInterval(async () => {
  if (state.pollBusy || document.hidden || state.page !== 'investigations' || !state.cluster || !$('[data-status="queued"], [data-status="running"], [data-status="pending"]', $('#investigation-list'))) return;
  state.pollBusy = true;
  try { await loadInvestigations(); } catch (error) { globalError(error); } finally { state.pollBusy = false; }
}, 4000);
(async () => { loading('#overview-content'); try { await loadClusters(); await navigate(); } catch (error) { $('#connection-state').textContent = 'Ошибка подключения'; clear('#overview-content').append(empty('Не удалось получить данные', 'Проверьте доступ к серверу и авторизацию. Повторите запрос кнопкой «Обновить».')); globalError(error); } })();

setInterval(async()=>{if(state.page!=='overview'||document.hidden||state.overviewBusy||document.querySelector('dialog[open]')||['SELECT','INPUT','TEXTAREA'].includes(document.activeElement?.tagName))return;state.overviewBusy=true;try{await loadOverview();}catch(e){globalError(e);}finally{state.overviewBusy=false;}},30000);

let analysisState=null, analysisSequence=0, analysisTimer=null;
function readJson(value,fallback){try{return JSON.parse(value);}catch(_){return fallback;}}
function safeAnswer(text){
 const root=node('div','chat-answer');let code=null,list=null,table=null;
 for(const line of String(text||'').split('\n')){
  if(line.trim().startsWith('```')){code=code?null:node('pre');if(code)root.append(code);list=null;table=null;continue;}
  if(code){code.textContent+=line+'\n';continue;}
  const clean=line.replace(/^#{1,6}\s*/,'').replace(/\*\*(.*?)\*\*/g,'$1');
  if(/^\s*\|.*\|\s*$/.test(clean)){
   const cells=clean.trim().slice(1,-1).split('|').map(v=>v.trim());
   if(cells.every(v=>/^:?-+:?$/.test(v)))continue;
   const row=node('tr');for(const value of cells)row.append(node(table?'td':'th','',value));
   if(!table){table=node('table','analysis-table');table.append(append(node('thead'),row),node('tbody'));root.append(append(node('div','table-wrap'),table));}else table.querySelector('tbody').append(row);
   list=null;continue;
  }
  table=null;
  if(/^\s*[-*]\s+/.test(line)){if(!list){list=node('ul');root.append(list);}list.append(node('li','',clean.replace(/^\s*[-*]\s+/,'')));}
  else{list=null;root.append(node(/^#{1,6}\s|^\d+[.)]\s/.test(line)?'h3':'p','',clean));}
 }
 return root;
}
function showAnalysisJobs(jobs){const root=clear('#ai-conversation');for(const job of jobs){const turn=node('article','chat-turn');turn.append(node('div','chat-question',job.question));if(job.answer){if(job.status!=='done')notice(turn,['queued','running'].includes(job.status)?'Ответ поступает; анализ ещё не завершён.':'Незавершённый ответ — анализ не подтверждён.','warning');turn.append(safeAnswer(job.answer));}if(job.error)notice(turn,job.error,'error');if(['queued','running'].includes(job.status))turn.append(node('p','muted',job.status==='queued'?'В очереди…':'Собираем факты и готовим ответ…'));const trace=readJson(job.trace,[]);if(Array.isArray(trace)&&trace.length){const details=node('details','chat-trace');details.append(node('summary','','Источники, skills и инструменты'));for(const item of trace)details.append(node('div','',`${item.tool} · ${item.status}`));turn.append(details);}const evidence=readJson(job.evidence,{});if(evidence.records)showEvidence(turn,evidence);else turn.append(button('Журнал проверок',async()=>{try{const full=await api('/investigations/'+encodeURIComponent(job.id));const report=readJson(full.evidence,{});if(report.records)showEvidence(turn,report);else toast('Для прежнего задания журнал не сохранён');}catch(e){toast(errorText(e));}},'secondary small'));root.append(turn);}root.scrollTop=root.scrollHeight;}
async function openAnalysis(context){clearTimeout(analysisTimer);const sequence=++analysisSequence;analysisState={context,previous:null,jobs:[]};$('#ai-title').textContent='✧ Анализ проблемы';$('#ai-context').textContent=`${context.cluster_name||context.cluster_id} · ${context.namespace||context.kind||context.metric||'кластер'} · ${context.name||context.title||''}\n${context.title||''}${context.stale?' · исходный снимок устарел':''}`;$('#ai-form').reset();$('#ai-status').textContent='';$('#ai-form button[type=submit]').disabled=false;clear('#ai-conversation').append(node('p','muted','Задайте вопрос. ИИ получит контекст выбранной проблемы и доступные факты.'));if(!$('#ai-dialog').open)$('#ai-dialog').showModal();
 try{const historyParams=new URLSearchParams({cluster_id:context.cluster_id});if(context.id)historyParams.set('problem_id',context.id);if(context.metric)historyParams.set('metric',context.metric);const result=await api('/investigations?'+historyParams);if(sequence!==analysisSequence)return;const jobs=(result.items||[]).filter(job=>{const ctx=readJson(job.context,{});return context.id?ctx.id===context.id:context.metric?ctx.metric===context.metric:false;}).reverse();analysisState.jobs=jobs;if(jobs.length){showAnalysisJobs(jobs);const last=jobs[jobs.length-1];if(last.status==='done')analysisState.previous=last.id;else if(['queued','running'].includes(last.status))pollAnalysis(last.id,sequence);}}
 catch(e){$('#ai-status').textContent=errorText(e);}}
async function pollAnalysis(id,sequence){if(sequence!==analysisSequence||!$('#ai-dialog').open)return;$('#ai-form button[type=submit]').disabled=true;try{const job=await api(`/investigations/${encodeURIComponent(id)}`);if(sequence!==analysisSequence)return;const index=analysisState.jobs.findIndex(j=>j.id===id);if(index<0)analysisState.jobs.push(job);else analysisState.jobs[index]=job;showAnalysisJobs(analysisState.jobs);$('#ai-status').textContent={done:'Ответ готов',failed:'Анализ не завершён',interrupted:'Задание прервано',queued:'В очереди',running:'Анализ…'}[job.status]||job.status;if(['queued','running'].includes(job.status)){analysisTimer=setTimeout(()=>pollAnalysis(id,sequence),2000);}else{$('#ai-form button[type=submit]').disabled=false;if(job.status==='done')analysisState.previous=id;}}
 catch(e){$('#ai-status').textContent=errorText(e);$('#ai-form button[type=submit]').disabled=false;}}
$('#ai-dialog').addEventListener('close',()=>{clearTimeout(analysisTimer);analysisSequence++;});
$('#ai-form').addEventListener('submit',async event=>{event.preventDefault();if(!analysisState)return;const sequence=analysisSequence,context=analysisState.context,body={cluster_id:context.cluster_id,question:event.currentTarget.elements.question.value.trim(),mode:event.currentTarget.elements.analysis_mode.value};if(analysisState.previous)body.previous_id=analysisState.previous;else if(context.id)body.problem_id=context.id;if(context.metric)body.metric=context.metric;$('#ai-form button[type=submit]').disabled=true;try{const job=await api('/investigations',{method:'POST',body});if(sequence!==analysisSequence)return;$('#ai-form').reset();await pollAnalysis(job.id,sequence);}catch(e){$('#ai-status').textContent=errorText(e);$('#ai-form button[type=submit]').disabled=false;}});

$('#holmes-bootstrap').addEventListener('click',event=>busy(event.currentTarget,async()=>{if(!state.editingCluster)return;const out=$('#holmes-message');try{const port=Number($('#cluster-form').elements.holmes_port.value);const result=await api(`/clusters/${encodeURIComponent(state.editingCluster)}/holmes-bootstrap`,{method:'POST',body:{port}});out.textContent=`Создан runtime/${result.relative_path}. Запустите compose этого каталога на сервере. Затем заново откройте настройки кластера и проверьте ответ Holmes.`;await loadClusters();}catch(e){out.textContent=errorText(e);}}));
$('#holmes-test').addEventListener('click',event=>busy(event.currentTarget,async()=>{if(!state.editingCluster)return;const out=$('#holmes-message');out.textContent='Проверяем сохранённое подключение…';try{const result=await api(`/clusters/${encodeURIComponent(state.editingCluster)}/holmes-test`,{method:'POST'});out.textContent='Holmes ответил: '+result.answer;}catch(e){out.textContent=errorText(e);}}));

$('#holmes-accept').addEventListener('click',event=>busy(event.currentTarget,async()=>{if(!state.editingCluster)return;const out=$('#holmes-message');out.textContent='Проверяем настоящий вызов инструмента…';try{const result=await api(`/clusters/${encodeURIComponent(state.editingCluster)}/holmes-accept`,{method:'POST'});out.textContent=result.ok?'Инструмент вызван, непредсказуемый результат получен. Можно включить Holmes для кластера.':result.answer;}catch(e){out.textContent=errorText(e);}}));

function showEvidence(target,report){if(target.querySelector('.evidence-journal'))return;const journal=node('details','evidence-journal');journal.append(node('summary','',`Реальные проверки: ${report.records.length} · недоступны ${report.unavailable??report.records.filter(r=>r.status!=='ok').length}`),node('p','muted',report.note||'API-запросы, эквиваленты kubectl и полученные данные.'));for(const record of report.records){const row=node('details','evidence-row');row.append(node('summary','',`${record.id} · ${record.check} · ${record.status}`),node('small','muted',date(record.finished_at)),node('pre','',`${record.method} ${record.path}\n${record.query?JSON.stringify(record.query):''}\n${record.equivalent_command||''}`),node('pre','',record.error||JSON.stringify(record.data,null,2)));journal.append(row);}target.append(journal);}
let metricCandidates=[];
$('#metrics-discover').addEventListener('click',event=>busy(event.currentTarget,async()=>{try{const data=await api(`/clusters/${encodeURIComponent(state.editingCluster)}/metrics-discovery`);metricCandidates=data.items;const select=clear('#metrics-candidates');select.append(new Option('Выберите источник',''));metricCandidates.forEach((s,i)=>select.append(new Option(`${s.namespace}/${s.service}:${s.port}`,String(i))));$('#metrics-message').textContent=data.error||data.note+(data.items.length?'':' Сервисы пока не найдены в снимке. Выполните сбор.');}catch(e){$('#metrics-message').textContent=errorText(e);}}));
$('#metrics-candidates').addEventListener('change',event=>{if(event.target.value==='')return;const source=metricCandidates[Number(event.target.value)],form=$('#cluster-form');form.elements.metrics_transport.value='kubernetes';for(const key of ['namespace','service','port'])form.elements['metrics_'+key].value=source[key];$('#metrics-message').textContent='Источник выбран. Укажите scope (выделенный или labels), сохраните кластер и проверьте подключение.';});
$('#metrics-test').addEventListener('click',event=>busy(event.currentTarget,async()=>{const out=$('#metrics-message');out.textContent='Проверяем сохранённый источник…';try{const data=await api(`/clusters/${encodeURIComponent(state.editingCluster)}/metrics-test`,{method:'POST'});out.textContent=data.note+` · серий ${data.series}`;}catch(e){out.textContent=errorText(e);}}));
$$('[data-analysis]').forEach(control=>control.addEventListener('click',()=>{if($('#ai-form button[type=submit]').disabled)return;const mode=control.dataset.analysis;if(mode==='about'){toast('Pulse собирает доказательства через read-only API; анализ выполняет выбранный движок: локальная LLM, Holmes или ИИ Astra. Выполненные проверки видны в журнале.');return;}const form=$('#ai-form');form.elements.analysis_mode.value=mode;form.elements.question.value={brief:'Кратко: что произошло, важность, подтверждённая причина или гипотеза?',extended:'Проведи подробное расследование этой проблемы по собранным доказательствам.',causes:'Установи причину этой проблемы и отдели доказанные факты от гипотез.',impact:'Каковы подтверждённые и возможные последствия этой проблемы?',commands:'Дай нужные команды для диагностики и условного исправления, с кратким пояснением каждой.'}[mode];form.requestSubmit();}));

$('#cluster-form').elements.astra_ai_enabled.addEventListener('change',event=>{if(event.target.checked)$('#cluster-form').elements.holmes_enabled.checked=false;});
$('#cluster-form').elements.holmes_enabled.addEventListener('change',event=>{if(event.target.checked)$('#cluster-form').elements.astra_ai_enabled.checked=false;});
$('#astra-ai-test').addEventListener('click',event=>busy(event.currentTarget,async()=>{const out=$('#astra-ai-message');out.textContent='Проверяем сохранённый URL, авторизацию и завершение потока…';try{const result=await api(`/clusters/${encodeURIComponent(state.editingCluster)}/astra-ai-test`,{method:'POST'});out.textContent='Поток завершён. Astra AI: '+result.answer;}catch(e){out.textContent=errorText(e);}}));
