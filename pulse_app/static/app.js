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
  return node('span', 'chip warning', {stale: 'Устаревший снимок', partial: 'Неполные данные', unknown: 'Нет данных', collecting: 'Сбор данных'}[normalized] || status || 'Нет данных');
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
async function loadOverview() {
  const target = $('#overview-content');
  const data = await api('/overview'); state.overview = data.clusters || []; $('#version').textContent = data.version || '0.6.1'; clear(target);
  const clusters = state.cluster ? state.overview.filter(item => item.id === state.cluster) : state.overview;
  if (!clusters.length) { target.append(empty('Подключите первый кластер', 'Pulse собирает факты из Kubernetes, сопоставляет их с метриками и помогает расследовать проблемы. Начните с подключения только для чтения.', button('+ Добавить кластер', () => openCluster(), ''))); return; }
  const stats = node('div', 'grid stats');
  const total = key => clusters.some(item => item.observed_at && item.counts?.[key] !== undefined) ? clusters.reduce((sum, item) => sum + (item.observed_at ? Number(item.counts?.[key]) || 0 : 0), 0) : '—';
  [['Кластеры', clusters.length, `${clusters.filter(item => item.enabled).length} со включённым сбором`], ['Узлы', total('nodes'), 'По последним снимкам'], ['Pods', total('pods'), 'Running не означает Ready'], ['Namespaces', total('namespaces'), 'Наблюдаемая область']].forEach(([title, value, hint]) => stats.append(append(node('div', 'stat'), node('span', '', title), node('strong', '', value), node('small', '', hint))));
  target.append(stats);
  target.append(append(node('div', 'section-heading'), node('h2', '', 'Подключённые кластеры'), button('+ Добавить кластер', () => openCluster(), 'secondary')));
  const cards = node('div', 'grid cluster-grid');
  clusters.forEach(cluster => {
    const card = node('article', 'cluster-card'); card.dataset.testid = 'cluster-card';
    const title = append(node('div'), node('h3', '', cluster.name), node('span', 'identifier', cluster.id));
    card.append(append(node('div', 'card-heading'), title, statusChip(cluster.status, cluster.enabled)));
    const counts = node('div', 'counts'); ['nodes', 'pods', 'namespaces'].forEach(key => counts.append(append(node('div'), node('strong', '', cluster.observed_at ? (cluster.counts?.[key] ?? '—') : '—'), node('span', '', typeNames[key])))); card.append(counts);
    if (cluster.error) notice(card, cluster.error, 'error');
    card.append(append(node('div', 'card-bottom'), node('span', '', date(cluster.observed_at)), button('Ресурсы →', () => { setCluster(cluster.id); location.hash = 'resources'; if (state.page === 'resources') loadPage(); })));
    cards.append(card);
  });
  target.append(cards);
  target.append(node('p', 'inline-help', 'Количество объектов относится к последнему доступному снимку. Проверяйте время наблюдения и полноту источников перед выводами.'));
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
