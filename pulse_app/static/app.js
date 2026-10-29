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
const stateLabels={ready:'Готовы',completed:'Завершены',enabled:'Включены',active:'Выполняются',pending:'Ожидают',not_ready:'Не готовы',failed:'Ошибка',suspended:'Приостановлены',updating:'Обновляются',scaled_zero:'Реплик: 0',terminating:'Удаляются',unknown:'Неизвестно'};
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
async function loadOverview() {
  const request=++overviewRequest, target=$('#overview-content'), selected=state.cluster;
  const data=await api('/dashboard'+(selected?`?cluster_id=${encodeURIComponent(selected)}`:''));
  if(request!==overviewRequest || selected!==state.cluster || state.page!=='overview')return;
  state.overview=data.clusters||[];$('#version').textContent=data.version;clear(target);
  const clusters=state.overview;
  if(!clusters.length){target.append(empty('Подключите первый кластер','Добавьте подключение Kubernetes с правами чтения.',button('+ Добавить кластер',()=>openCluster(),'')));return;}
  const total=key=>{const rows=clusters.filter(c=>Number.isFinite(c.counts?.[key]));return rows.length?rows.reduce((n,c)=>n+c.counts[key],0):'—';};
  renderHealthBanner(target,data,clusters);
  const h=data.health||{}, nh=h.nodes||{}, stats=node('div','grid stats');
  const pr=clusters.reduce((a,c)=>a+(c.health?.pods?.ready||0),0), pa=clusters.reduce((a,c)=>a+(c.health?.pods?.active||0),0);
  const pk=clusters.every(c=>c.health?.pods?.active!==null&&c.health?.pods?.active!==undefined);
  const pt=clusters.some(c=>c.health?.status==='stale'||c.health?.status==='unknown'||c.health?.status==='paused')?'unknown':clusters.some(c=>c.issues?.some(i=>i.kind==='pods'&&i.severity==='critical'))?'critical':pr<pa?'warning':pa?'healthy':'neutral';
  stats.append(healthTile('Узлы Ready',ratio(nh.ready,nh.total),'Готовые / все наблюдаемые узлы',nh.health||'unknown','nodes-ready',()=>{const panel=$('.node-panel');if(panel){panel.open=true;panel.scrollIntoView({behavior:'smooth'});}}));
  stats.append(healthTile('Активные Pods Ready',pk?ratio(pr,pa):'—','Завершённые и удаляемые исключены',pt,'pods-ready',clusters.length===1?()=>resourceView(clusters[0].id,'pods'):null));
  stats.append(healthTile('Критические сигналы',h.critical??'—','Узлы, запуск контейнеров и deadlines',h.status==='stale'||h.status==='unknown'||h.status==='paused'?'unknown':h.critical?'critical':'healthy','critical-signals',()=>$('#problem-list')?.scrollIntoView({behavior:'smooth'})));
  stats.append(healthTile('Область наблюдения',`${clusters.length} / ${total('namespaces')}`,'Кластеры / namespaces','neutral','scope'));
  target.append(stats);
  const map=node('div','panel cluster-map');map.append(append(node('div','section-heading'),node('h2','','Карта кластеров'),button('+ Добавить кластер',()=>openCluster())));
  const table=node('table'),thead=node('thead'),tr=node('tr');['Кластер','Состояние / сбор','Nodes / Pods','Наблюдение','Источники',''].forEach(t=>tr.append(node('th','',t)));thead.append(tr);table.append(thead);
  const body=node('tbody');clusters.forEach(c=>{const r=node('tr');r.append(node('td','',c.name),append(node('td'),healthBadge(c.health?.status||'unknown'),node('small','metadata',`Сбор: ${{ok:'обновляется',degraded:'неполный',stale:'устарел',paused:'пауза',waiting:'первичный'}[c.status]||c.status}`)),node('td','',`Узлы ${ratio(c.health?.nodes?.ready,c.health?.nodes?.total)} · Pods ${c.counts.pods??'—'}`),node('td','',date(c.observed_at)),node('td','',`Kubernetes · метрики ${c.sources.metrics?'настроены':'—'} · Astra ${c.sources.astra?'настроена':'—'}`),append(node('td'),button('Открыть',()=>{setCluster(c.id);loadPage();})));body.append(r);});table.append(body);map.append(append(node('div','table-wrap'),table));target.append(map);
  const metricHeading=node('div','section-heading');metricHeading.append(node('h2','','Утилизация ресурсов'));
  const range=node('select');range.setAttribute('aria-label','Интервал графиков');range.className='range-select';for(const [v,t] of [['1h','1 час'],['6h','6 часов'],['24h','24 часа']])range.append(new Option(t,v));range.value=metricWindow;
  range.addEventListener('change',()=>{metricWindow=range.value;loadPage();});metricHeading.append(range);target.append(metricHeading);
  const metricTarget=node('div');metricTarget.id='dashboard-metrics';target.append(metricTarget);
  const metricCid=selected||(clusters.length===1?clusters[0].id:null);
  if(!metricCid)notice(metricTarget,'Выберите кластер слева для утилизации и графиков. Проценты разных кластеров не усредняются.');
  else {notice(metricTarget,'Получение метрик…');api(`/clusters/${encodeURIComponent(metricCid)}/dashboard-metrics?window=${metricWindow}`).then(result=>{if(metricTarget.isConnected&&selected===state.cluster)renderDashboardMetrics(metricTarget,result);}).catch(e=>{if(metricTarget.isConnected)notice(clear(metricTarget),errorText(e),'error');});}
  renderNodePanel(target,clusters);
  target.append(append(node('div','section-heading'),node('h2','','Состояние workloads')));
  target.append(node('p','inline-help','Процент объектов Ready / завершённых Jobs / включённых CronJobs. Пустой набор не означает 100%.'));
  const cards=node('div','workload-grid');for(const kind of Object.keys(workloadNames))cards.append(workloadCard(kind,data.workloads[kind],clusters));target.append(cards);
  const bottom=node('div','overview-bottom'),issues=node('div','panel');issues.id='problem-list';issues.append(node('h2','',`Требует проверки · ${data.issues_total}`));
  const filter=node('select');filter.setAttribute('aria-label','Фильтр проблем');for(const [v,t] of [['all','Все сигналы'],['critical','Критические'],['warning','Предупреждения']])filter.append(new Option(t,v));filter.value=issueFilter;issues.append(filter);
  const list=node('div');issues.append(list);
  const showIssues=()=>{clear(list);const found=data.issues.filter(i=>filter.value==='all'||i.severity===filter.value);if(!found.length)notice(list,'В доступной выборке нет сигналов выбранного уровня. Проверяйте свежесть и полноту данных.');for(const issue of found){const row=node('div',`issue-row severity-${issue.stale?'historical':issue.severity||'warning'}`);row.append(append(node('div'),node('span',`severity-label ${issue.stale?'historical':issue.severity||'warning'}`,issue.stale?'ПО ПРОШЛОМУ СНИМКУ':issue.severity==='critical'?'КРИТИЧЕСКИЙ СИГНАЛ':'ПРЕДУПРЕЖДЕНИЕ'),node('strong','',issue.title||issue.state),node('small','muted',`${issue.name} · ${issue.cluster_name} · ${issue.namespace||issue.kind}`),node('p','',issue.detail||'')));
  row.append(button(issue.kind==='pods'?'Почему?':'Ресурс',()=>{if(issue.kind!=='pods'){resourceView(issue.cluster_id,issue.kind);return;}setCluster(issue.cluster_id);const form=$('#investigation-form');form.elements.namespace.value=issue.namespace;form.elements.pod.value=issue.name;form.elements.question.value=`Объясни состояние Pod ${issue.name}. Симптом: ${issue.title||issue.state}. Укажи факты, гипотезы и безопасные проверки.`;location.hash='investigations';}));list.append(row);}};filter.addEventListener('change',()=>{issueFilter=filter.value;showIssues();});showIssues();
  const sources=node('div','panel');sources.append(node('h2','','Источники и диагностика'),node('p','muted','Статус сбора API не означает, что все приложения исправны.'));
  for(const c of clusters){sources.append(node('h3','',c.name));if(c.error)notice(sources,c.error,'warning');sources.append(node('p','',`Метрики: ${c.sources.metrics?'настроены':'не подключены'} · Astra: ${c.sources.astra?'настроена':'не подключена'}`));}
  sources.append(node('p','',`Локальная LLM: ${data.llm_configured?'настроена — проверьте анализ конкретного Pod':'укажите URL и Model ID в настройках'}`),button('Настройки источников',()=>location.hash='settings'));
  const restarts=clusters.reduce((n,c)=>n+(c.health?.pods?.restarts||0),0);sources.append(node('p','muted',`Рестарты текущих Pod: ${restarts} (накопленные счётчики, не скорость за интервал)`));
  bottom.append(issues,sources);target.append(bottom);renderWarningEvents(target,clusters);
}
function renderDashboardMetrics(target,result) {
  clear(target);if(result.error)notice(target,result.error,'warning');
  if(!result.configured)notice(target,result.note||'Подключите Prometheus / VictoriaMetrics. Значения утилизации пока неизвестны.');
  const grid=node('div','utilization-grid');
  for(const [key,title,hint] of [['cpu','CPU','Non-idle CPU узлов'],['ram','RAM','Total − available'],['disk','Диски /','Корневая файловая система узлов; не PVC']]) {
    const value=result.metrics?.[key],card=node('article','panel utilization-card');card.dataset.testid=`utilization-${key}`;
    const stale=value?.observed_at && Date.now()/1000-value.observed_at>120;
    card.append(append(node('div','utilization-head'),append(node('div'),node('h3','',title),node('small','muted',hint)),donut(value?.percent,title,stale?'amber':value?.percent>=90?'danger':value?.percent>=75?'amber':'')));
    card.classList.add(!Number.isFinite(value?.percent)||stale?'metric-unknown':value.percent>=90?'metric-critical':value.percent>=75?'metric-warning':'metric-healthy');
    if(Number.isFinite(value?.percent)&&!stale&&value.percent>=75)card.append(node('p',value.percent>=90?'critical-text':'warning-text',value.percent>=90?'Высокая утилизация — проверьте запас ресурсов':'Утилизация выше 75% — следите за запасом'));
    if(!Number.isFinite(value?.percent))card.append(node('p','muted','Нет данных'));
    if(value?.error)notice(card,value.error,'warning');
    if(stale)notice(card,'Значение метрики устарело','warning');
    if(value?.observed_at)card.append(node('small','metadata',`Источник: ${date(value.observed_at*1000)}`));
    const points=value?.history||[];
    if(points.length>1){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 400 110');svg.setAttribute('role','img');svg.setAttribute('aria-label',`${title}: история, шкала 0–100%`);svg.classList.add('history-chart');
      for(const y of [5,55,105]){const line=document.createElementNS(svg.namespaceURI,'line');for(const [k,v] of Object.entries({x1:0,x2:400,y1:y,y2:y,stroke:'#2b4054'}))line.setAttribute(k,v);svg.append(line);}
      let segment=[];const flush=()=>{if(segment.length>1){const poly=document.createElementNS(svg.namespaceURI,'polyline');poly.setAttribute('points',segment.join(' '));poly.setAttribute('fill','none');poly.setAttribute('stroke',key==='cpu'?'#27d6b5':key==='ram'?'#71a6ff':'#b69af7');poly.setAttribute('stroke-width','2.5');svg.append(poly);}segment=[];};let prior=null;
      for(const [ts,v] of points){if(prior!==null && ts-prior>result.step*1.5)flush();segment.push(`${400*(ts-result.start)/(result.end-result.start)},${105-v}`);prior=ts;}flush();card.append(svg,node('small','metadata',`${metricWindow} · шкала 0–100% · пропуски не соединяются`));
    }else card.append(node('div','history-empty',value?.history_error||'История пока недоступна'));
    grid.append(card);
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
    const content = append(node('div'), node('h3', '', cluster.name), node('small', '', `${cluster.id} · ${cluster.enabled ? 'сбор включён' : 'пауза'} · метрики ${cluster.has_metrics ? '✓' : '—'} · Astra ${cluster.has_astra ? '✓' : '—'}`));
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
  $('#cluster-dialog-title').textContent = cluster ? `Настроить · ${cluster.name}` : 'Добавить кластер';
  $('#cluster-dialog').showModal();
}
function integrationSettings(form, prefix) {
  const url = form.elements[`${prefix}_url`].value.trim(); if (!url) return undefined;
  const settings = {url};
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

setInterval(async()=>{if(state.page!=='overview'||document.hidden||state.overviewBusy||document.querySelector('dialog[open]')||document.activeElement?.tagName==='SELECT')return;state.overviewBusy=true;try{await loadOverview();}catch(e){globalError(e);}finally{state.overviewBusy=false;}},30000);
