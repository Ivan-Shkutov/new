'use strict';

// A scenario has one target and one result. Previous model answers are never input.
const scenarioDefinitions = [
  {id:'brief',title:'Краткий разбор',description:'Что произошло, насколько важно и что проверить первым',hint:'Суть, важность и первая проверка'},
  {id:'deep',title:'Подробный разбор',description:'Связанные объекты, события, логи и ограничения данных',hint:'Связи, события и доступные логи'},
  {id:'causes',title:'Причины сбоя',description:'Подтверждённая причина и гипотезы с основаниями',hint:'Факты отдельно от гипотез'},
  {id:'impact',title:'Последствия сбоя',description:'Что затронуто и какие риски требуют проверки',hint:'Влияние на приложение и данные'},
  {id:'plan',title:'План устранения',description:'Последовательность проверок и рекомендации оператору',hint:'Шаги, команды и ожидаемый результат'},
  {id:'recovery',title:'Проверка восстановления',description:'Свежая проверка реплик и готовых адресов сервисов',hint:'Реплики и готовые адреса сервисов'}
];
const scenarioState = {target:null,active:'brief',session:null,sequence:0,timer:null,busy:false,targets:[],load:0,metadataLoaded:false,sourceText:'Проверки ещё не выполнялись',sourceTone:''};

function discardScenarioSession() {
  clearTimeout(scenarioState.timer); scenarioState.timer = null;
  const sid = scenarioState.session; scenarioState.session = null;
  if (sid) fetch(`/api/sessions/${encodeURIComponent(sid)}`, {method:'DELETE',headers:{'X-Pulse-Request':'1'},credentials:'same-origin',keepalive:true}).catch(()=>{});
}
function clearScenarioContext() {
  ++scenarioState.sequence; ++scenarioState.load;
  discardScenarioSession(); scenarioState.target = null; scenarioState.active = 'brief'; scenarioState.busy = false;
  if ($('#scenario-result-body')) { renderScenarioTarget(); renderScenarioIdle(); }
}
function scenarioIdentity(target) {
  if (!target) return '';
  return JSON.stringify([target.cluster_id,target.id||'',target.kind||'',target.namespace||'',target.name||target.pod||'',target.uid||'',target.metric||'']);
}
function scenarioMetadata(id = scenarioState.active) { return scenarioDefinitions.find(item => item.id === id) || scenarioDefinitions[0]; }
function setScenarioSource(text,tone = '') {
  scenarioState.sourceText = text; scenarioState.sourceTone = tone;
  const source = $('#scenario-source-state');
  if (source) { source.textContent = text; source.className = `chip ${tone}`; }
}
function renderScenarioButtons() {
  const list = clear('#scenario-buttons');
  scenarioDefinitions.forEach((item,index) => {
    const control = node('button','scenario-button'); control.type = 'button'; control.dataset.scenario = item.id;
    control.append(node('span','scenario-number',String(index+1).padStart(2,'0')),append(node('span'),node('strong','',item.title),node('small','',item.hint)));
    control.addEventListener('click',()=>runScenario(item.id)); list.append(control);
  });
  updateScenarioSelection();
}
function updateScenarioSelection() {
  $$('#scenario-buttons [data-scenario]').forEach(control => {
    const active = control.dataset.scenario === scenarioState.active;
    control.classList.toggle('active',active); control.setAttribute('aria-pressed',String(active));
    control.disabled = !scenarioState.target;
  });
  const metadata = scenarioMetadata();
  $('#scenario-result-title').textContent = metadata.title;
  $('#scenario-result-description').textContent = metadata.description;
  $('#scenario-rerun').disabled = !scenarioState.target || scenarioState.busy;
}
function scenarioProviderName() {
  if ($('#scenario-provider').value === 'local') return 'Локальная LLM';
  const selected = state.clusters.find(item => item.id === (scenarioState.target?.cluster_id || state.cluster));
  if (selected?.astra_ai?.enabled) return `Astra AI · ${selected.astra_ai.model || 'модель кластера'}`;
  if (selected?.holmes?.enabled) return 'HolmesGPT';
  return 'Локальная LLM · настройки кластера';
}
function renderScenarioTarget() {
  const target = scenarioState.target, box = $('#scenario-target');
  clear(box);
  const identity = node('div','scenario-target-identity');
  if (target) {
    const name = node('div','scenario-target-name');
    name.append(node('span','scenario-kind',target.metric ? 'Метрики' : target.kind === 'pods' || target.pod ? 'Pod' : target.kind || 'Объект'),node('strong','',target.name||target.pod||target.title||target.metric||'Кластер'));
    identity.append(name,node('div','scenario-target-meta',[target.cluster_name||target.cluster_id,target.namespace,target.title].filter(Boolean).join(' · ')));
    if (target.uid) identity.append(node('div','scenario-target-uid',`UID: ${target.uid}`));
  } else identity.append(node('div','scenario-target-name','Выберите объект'),node('div','scenario-target-meta','Откройте проблему из Обзора или Pod из Ресурсов.'));
  const source = node('span',`chip ${scenarioState.sourceTone}`,scenarioState.sourceText); source.id = 'scenario-source-state';
  box.append(identity,source);
  $('#scenario-provider option[value=cluster]').textContent = $('#scenario-provider').value === 'local' ? 'Настройки кластера' : scenarioProviderName();
  updateScenarioSelection();
}
function renderScenarioIdle(message) {
  scenarioState.busy = false; $('#scenario-result').setAttribute('aria-busy','false');
  $('#scenario-status').textContent = scenarioState.target ? 'Готов к запуску' : 'Ожидает выбора объекта';
  $('#scenario-status').className = 'scenario-status';
  $('#scenario-observed-at').textContent = 'История вопросов не используется';
  setScenarioSource('Проверки ещё не выполнялись');
  delete $('#scenario-result-body').dataset.scenarioResult;
  clear('#scenario-result-body').append(append(node('div','scenario-empty'),node('div','scenario-empty-symbol','✧'),node('h3','',scenarioState.target ? 'Выберите сценарий слева' : 'Начните с объекта диагностики'),node('p','',message||(scenarioState.target ? 'Проверка прочитает состояние выбранного объекта и связанных ресурсов.' : 'В Обзоре нажмите ✧ рядом с проблемой. В Ресурсах откройте «Диагностика» у нужного Pod.'))));
  updateScenarioSelection();
}
async function loadDiagnostics() {
  const sequence = ++scenarioState.load;
  renderScenarioTarget();
  if (!scenarioState.busy && !$('#scenario-result-body').children.length) renderScenarioIdle();
  if (!scenarioState.metadataLoaded) {
    try {
      const response = await api('/scenarios');
      if (sequence !== scenarioState.load || state.page !== 'diagnostics') return;
      for (const definition of scenarioDefinitions) {
        const saved = (response.items || []).find(item=>item.id===definition.id);
        if (saved?.title) definition.title = saved.title;
        if (saved?.description) definition.description = saved.description;
      }
      scenarioState.metadataLoaded = true; renderScenarioButtons();
    } catch (_) { /* Names remain available; a run reports an incompatible server. */ }
  }
  const cid = state.cluster, select = clear('#scenario-target-select');
  select.append(new Option(cid ? 'Выберите проблему из списка' : 'Сначала выберите кластер',''));
  select.disabled = !cid;
  if (!cid) { scenarioState.targets = []; return; }
  const selected = scenarioState.target;
  if (selected) select.append(new Option(selected.name||selected.pod||selected.title||selected.metric,'current'));
  select.value = selected ? 'current' : '';
  try {
    const response = await api('/problems?'+new URLSearchParams({cluster_id:cid,limit:100,offset:0,severity:'all',q:''}));
    if (sequence !== scenarioState.load || state.page !== 'diagnostics' || state.cluster !== cid) return;
    scenarioState.targets = Array.isArray(response.items) ? response.items : [];
    scenarioState.targets.forEach((item,index)=>{
      if (selected?.id && item.id === selected.id) return;
      select.append(new Option(`${item.namespace ? item.namespace+' / ' : ''}${item.name||item.title} · ${item.reason||item.title}`,String(index)));
    });
    if (!selected && !scenarioState.targets.length) select.options[0].textContent = 'Проблем не найдено · выберите Pod в Ресурсах';
  } catch (error) {
    if (sequence !== scenarioState.load) return;
    select.options[0].textContent = 'Список проблем недоступен';
    select.title = errorText(error);
  }
}
function openScenario(context) {
  const target = {...context,cluster_id:context.cluster_id||state.cluster};
  if (!target.cluster_id) { toast('Выберите кластер'); return; }
  if (state.cluster !== target.cluster_id) setCluster(target.cluster_id);
  if (scenarioIdentity(target) !== scenarioIdentity(scenarioState.target)) clearScenarioContext();
  scenarioState.target = target;
  if ($('#ai-dialog').open) $('#ai-dialog').close();
  if ($('#dashboard-detail').open) $('#dashboard-detail').close();
  if ($('#object-dialog').open) $('#object-dialog').close();
  renderScenarioTarget();
  if (location.hash !== '#diagnostics') location.hash = 'diagnostics';
  else loadDiagnostics();
  // A fresh scenario replaces the previous result, including for the same target.
  runScenario('brief');
}
function scenarioRequestBody() {
  const target = scenarioState.target, body = {scenario:scenarioState.active};
  if (target.id) body.problem_id = target.id;
  if (target.namespace) body.namespace = target.namespace;
  if (target.metric) body.metric = target.metric;
  if (target.pod || target.kind === 'pods') body.pod = target.pod || target.name;
  if (target.uid) body.uid = target.uid;
  return body;
}
function renderScenarioProgress() {
  const target = clear('#scenario-result-body'), progress = node('div','scenario-progress','Читаем выбранный объект → проверяем связанные данные → формируем результат');
  target.dataset.scenarioResult = scenarioState.active;
  progress.id = 'scenario-progress'; progress.setAttribute('role','status'); target.append(progress);
  const skeleton = node('div','scenario-skeleton'); skeleton.setAttribute('aria-hidden','true');
  for (let i=0;i<3;i++) skeleton.append(node('i'));
  target.append(skeleton,node('p','scenario-quiet','Проверки только читают данные. Изменения в кластере не выполняются.'));
  $('#scenario-result').setAttribute('aria-busy','true');
  $('#scenario-status').textContent = 'Проверяем данные…'; $('#scenario-status').className = 'scenario-status running';
  setScenarioSource('Получаем свежие данные');
  $('#scenario-observed-at').textContent = scenarioProviderName();
}
async function runScenario(id) {
  if (!scenarioState.target || !scenarioDefinitions.some(item=>item.id===id)) return;
  const wasBusy = scenarioState.busy;
  const sequence = ++scenarioState.sequence;
  clearTimeout(scenarioState.timer);
  // Superseding a running job uses a new session; an old response cannot win a race.
  if (wasBusy) discardScenarioSession();
  scenarioState.active = id; scenarioState.busy = true; updateScenarioSelection(); renderScenarioProgress();
  try {
    if (!scenarioState.session) {
      const session = await api('/sessions',{method:'POST',body:{cluster_id:scenarioState.target.cluster_id,mode:'problem',engine:$('#scenario-provider').value}});
      if (sequence !== scenarioState.sequence) {
        fetch(`/api/sessions/${encodeURIComponent(session.id)}`,{method:'DELETE',headers:{'X-Pulse-Request':'1'},credentials:'same-origin',keepalive:true}).catch(()=>{}); return;
      }
      scenarioState.session = session.id;
    }
    const sid = scenarioState.session;
    const job = await api(`/sessions/${encodeURIComponent(sid)}/scenarios`,{method:'POST',body:scenarioRequestBody()});
    if (sequence !== scenarioState.sequence) return;
    pollScenario(sid,job.id,sequence);
  } catch (error) { if (sequence === scenarioState.sequence) renderScenarioError(error); }
}
async function pollScenario(sid,id,sequence) {
  if (sequence !== scenarioState.sequence) return;
  try {
    const job = await api(`/sessions/${encodeURIComponent(sid)}/jobs/${encodeURIComponent(id)}`);
    if (sequence !== scenarioState.sequence) return;
    if (['queued','pending','running'].includes(job.status)) {
      renderScenarioJobProgress(job);
      scenarioState.timer = setTimeout(()=>pollScenario(sid,id,sequence),900); return;
    }
    scenarioState.busy = false; $('#scenario-result').setAttribute('aria-busy','false'); updateScenarioSelection();
    const result = scenarioObject(job.scenario_result);
    if (!result || typeof result.summary !== 'string' || !['complete','partial','unconfirmed'].includes(result.status)) {
      renderScenarioError(new Error('Сервер не вернул проверенный результат сценария. Проверьте версию сборки Pulse.')); return;
    }
    renderScenarioResult(result,job);
  } catch (error) { if (sequence === scenarioState.sequence) renderScenarioError(error); }
}
function renderScenarioJobProgress(job) {
  const progress = scenarioObject(job.progress) || {}, report = scenarioObject(job.evidence) || {};
  const records = scenarioList(report.records);
  const checks = Number.isInteger(progress.checks) && progress.checks >= 0 ? progress.checks : records.length;
  const unavailable = Number.isInteger(progress.unavailable) && progress.unavailable >= 0 ? progress.unavailable : records.filter(item=>!['ok','partial'].includes(item.status)).length;
  const analyzing = progress.phase === 'analyzing';
  $('#scenario-status').textContent = analyzing ? 'Готовим разбор…' : job.status === 'running' ? 'Проверяем данные…' : 'В очереди';
  const label = analyzing ? `Данные собраны · проверок: ${checks}. ${scenarioProviderName()} формирует результат.` : checks ? `Выполнено проверок: ${checks}${unavailable ? ` · недоступно: ${unavailable}` : ''}${progress.last_check ? `\nПоследняя проверка: ${progress.last_check}` : ''}` : 'Ожидаем начала проверки выбранного объекта…';
  const target = $('#scenario-progress'); if (target && target.textContent !== label) target.textContent = label;
  if (checks) setScenarioSource(`Проверок: ${checks} · недоступно: ${unavailable}`,unavailable ? 'warning' : '');
}
function scenarioObject(value) {
  if (!value) return null;
  if (typeof value === 'object' && !Array.isArray(value)) return value;
  try { const parsed = JSON.parse(value); return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null; } catch (_) { return null; }
}
function scenarioList(value) { return Array.isArray(value) ? value : []; }
function scenarioRefs(ids) {
  const refs = node('span','scenario-refs');
  scenarioList(ids).filter(id=>/^E\d+$/.test(String(id))).forEach(id=>{
    const ref = button(id,()=>{
      const source = $('#scenario-sources'), evidence = document.getElementById(`scenario-evidence-${id}`);
      if (source) source.open = true;
      if (evidence) { evidence.open = true; evidence.scrollIntoView({behavior:'smooth',block:'nearest'}); }
    },'secondary small');
    ref.classList.add('scenario-ref'); ref.setAttribute('aria-label',`Показать доказательство ${id}`); refs.append(ref);
  }); return refs;
}
function renderScenarioResult(result,job) {
  const target = clear('#scenario-result-body'); target.dataset.scenarioResult = scenarioState.active;
  const modelIncomplete = result.model?.status && !['complete','not_used'].includes(result.model.status);
  const labels = {complete:'Разбор готов',partial:'Часть данных недоступна',unconfirmed:'Вывод не подтверждён'};
  $('#scenario-status').textContent = modelIncomplete ? 'ИИ не завершил разбор · факты Pulse сохранены' : labels[result.status];
  $('#scenario-status').className = `scenario-status ${result.status === 'complete' && !modelIncomplete ? 'ready' : 'partial'}`;
  target.append(node('p','scenario-summary',result.summary));
  const rows = scenarioList(result.rows);
  if (rows.length) {
    const table = node('table','scenario-answer-table'), heading = node('tr');
    heading.append(node('th','','Параметр'),node('th','','Значение')); table.append(append(node('thead'),heading));
    const body = node('tbody'); rows.forEach(item=>body.append(append(node('tr'),node('td','',item.label),append(node('td'),node('span','',item.value),scenarioRefs(item.evidence_ids)))));
    table.append(body); target.append(append(node('div','scenario-table-frame'),table));
  }
  scenarioList(result.sections).forEach(section=>{
    const box = node('section','scenario-section');
    if (section.title) box.append(node('h3','',section.title));
    if (section.text) box.append(node('p','',section.text));
    if (scenarioList(section.items).length) { const list = node('ul'); section.items.forEach(item=>list.append(node('li','',typeof item === 'string' ? item : item.text || item.title || ''))); box.append(list); }
    target.append(box);
  });
  scenarioList(result.steps).forEach((step,index)=>{
    const title = String(step.title||'Проверка').replace(/^\s*\d+\s*[.)]\s*/,'');
    const card = node('section','scenario-step'); card.append(node('h3','',`${index+1}. ${title}`));
    if (step.detail) card.append(node('p','',step.detail));
    card.append(scenarioRefs(step.evidence_ids));
    scenarioList(step.commands).forEach(item=>{
      if (!item.command || typeof item.command !== 'string') return;
      const command = node('div','scenario-command'); command.append(node('code','',item.command));
      const copy = button('Копировать',async()=>{
        try { await navigator.clipboard.writeText(item.command); copy.textContent = 'Скопировано'; }
        catch (_) { copy.textContent = 'Выделите команду'; }
      },'secondary small'); command.append(copy); card.append(command);
      if (item.purpose) card.append(node('p','scenario-command-note',item.purpose));
      if (item.expected) card.append(node('p','scenario-command-note',`Ожидается: ${item.expected}`));
      if (item.scope) card.append(node('p','scenario-command-note',`Область: ${item.scope}`));
    }); target.append(card);
  });
  if (scenarioState.active === 'plan') target.append(node('p','scenario-quiet','Рекомендации выполняет оператор после проверки условий. Pulse команды не запускает.'));
  if (scenarioState.active === 'recovery') renderScenarioRecovery(target,result.recovery,scenarioObject(job.evidence));
  const limitations = [...new Set(scenarioList(result.limitations).filter(item=>typeof item === 'string' && item))];
  if (limitations.length) {
    const box = node('div','scenario-limitation');
    limitations.forEach(text=>box.append(node('p','',text))); target.append(box);
  }
  renderScenarioSources(target,result,job);
  $('#scenario-observed-at').textContent = result.observed_at ? `Данные: ${date(result.observed_at)}` : 'Время данных не получено';
  if (scenarioState.active === 'brief' || scenarioState.active === 'causes') target.append(append(node('div','scenario-next-actions'),button('Открыть план устранения →',()=>runScenario('plan'),'secondary small')));
  else if (scenarioState.active === 'impact' || scenarioState.active === 'plan') target.append(append(node('div','scenario-next-actions'),button('Проверить восстановление →',()=>runScenario('recovery'),'secondary small')));
}
function renderScenarioRecovery(target,recovery,report) {
  const value = recovery || {state:'unconfirmed',checks:[]};
  const checks = scenarioList(value.checks);
  const records = scenarioList(report?.records);
  const hasEvidence = item => {
    const ids = scenarioList(item.evidence_ids);
    return ids.length > 0 && ids.every(id=>records.some(record=>record.id === id && record.status === 'ok' && record.complete !== false));
  };
  const verified = value.state === 'verified' && checks.length > 0 && checks.every(item=>item.state === 'verified' && hasEvidence(item));
  const summary = node('div',`scenario-recovery-state ${verified ? 'verified' : 'unconfirmed'}`,verified ? 'Готовность по выполненным проверкам подтверждена' : value.state === 'not_recovered' ? 'Проверки показывают сохраняющуюся проблему' : 'Восстановление пока не подтверждено');
  summary.dataset.recoveryState = verified ? 'verified' : value.state === 'not_recovered' ? 'not_recovered' : 'unconfirmed'; target.append(summary);
  checks.forEach(item=>{
    const confirmed = item.state === 'verified' && hasEvidence(item);
    const row = node('div',`scenario-recovery-check ${confirmed ? 'verified' : 'unconfirmed'}`);
    row.append(append(node('div'),node('strong','',item.title),item.detail ? node('p','',item.detail) : null,scenarioRefs(item.evidence_ids)),node('span','',confirmed ? 'Подтверждено' : item.state === 'not_recovered' ? 'Проблема сохраняется' : 'Не подтверждено')); target.append(row);
  });
  if (!checks.length) target.append(node('p','scenario-quiet','Нет достаточных данных о репликах, endpoints и повторении исходного сбоя.'));
}
function renderScenarioSources(target,result,job) {
  const report = scenarioObject(job.evidence) || {}, records = scenarioList(report.records);
  const missing = records.filter(item=>!['ok','partial'].includes(item.status)).length, partial = records.filter(item=>item.status === 'partial').length;
  setScenarioSource(records.length ? `Проверок: ${records.length} · недоступно: ${missing}${partial ? ` · неполно: ${partial}` : ''}` : 'Журнал проверок не получен',!records.length || missing || partial ? 'warning' : '');
  const sources = node('details','scenario-sources'); sources.id = 'scenario-sources';
  sources.append(node('summary','',`Источники и проверки${records.length ? ` · ${records.length}` : ''}${missing ? ` · недоступно ${missing}` : ''}`));
  sources.append(node('p','scenario-quiet',`Движок: ${scenarioProviderName()} · ${result.origin === 'model' ? 'интерпретация ИИ по доказательствам Pulse' : 'проверенные факты Pulse'}`));
  const modelStatuses = {complete:'Ответ ИИ завершён · формат проверен',unavailable:'ИИ недоступен',invalid:'Ответ ИИ не прошёл проверку формата',not_used:'Вывод построен по проверкам Pulse'};
  sources.append(node('p','scenario-quiet',modelStatuses[result.model?.status]||'Статус ИИ не получен'));
  const modelError = job.error || result.model?.error;
  if (modelError) sources.append(node('p','scenario-source-error',modelError));
  if (report.note) sources.append(node('p','scenario-quiet',report.note));
  let trace = job.trace;
  if (typeof trace === 'string') { try { trace = JSON.parse(trace); } catch (_) { trace = []; } }
  if (scenarioList(trace).length) {
    const tools = node('details','scenario-evidence-row');
    tools.append(node('summary','','Правила и инструменты'));
    scenarioList(trace).forEach(item=>{
      if (item && typeof item === 'object') tools.append(node('p','scenario-quiet',
        `${item.tool||'Проверка'} · ${item.status||'статус не получен'}${item.version ? ` · v${item.version}` : ''}`));
    }); sources.append(tools);
  }
  if (!records.length) sources.append(node('p','scenario-source-error','Журнал недоступен. Полнота проверок не подтверждена.'));
  records.forEach(record=>{
    const row = node('details','scenario-evidence-row'); row.id = `scenario-evidence-${record.id}`;
    const status = {ok:'получено',partial:'неполные данные',unavailable:'недоступно',error:'ошибка'}[record.status]||record.status||'нет данных';
    row.append(node('summary','',`${record.id} · ${record.check||'Проверка'} · ${status}`));
    if (record.finished_at) row.append(node('p','scenario-quiet',date(record.finished_at)));
    if (record.error) row.append(node('p','scenario-source-error',record.error));
    if (record.equivalent_command) row.append(node('pre','',record.equivalent_command));
    if (record.method || record.path) row.append(node('pre','',`${record.method||'GET'} ${record.path||''}${record.query ? '\n'+JSON.stringify(record.query) : ''}`));
    if (record.data !== undefined) row.append(node('pre','',typeof record.data === 'string' ? record.data : JSON.stringify(record.data,null,2)));
    sources.append(row);
  }); target.append(sources);
}
function renderScenarioError(error) {
  scenarioState.busy = false; $('#scenario-result').setAttribute('aria-busy','false'); updateScenarioSelection();
  $('#scenario-status').textContent = 'Проверка не завершена'; $('#scenario-status').className = 'scenario-status partial';
  setScenarioSource('Свежие данные не получены','warning');
  clear('#scenario-result-body').append(node('div','scenario-limitation',errorText(error)),node('p','scenario-quiet','Проверьте подключение и повторите сценарий. Прежний результат не используется.'));
}

$('#scenario-provider').addEventListener('change',()=>{
  ++scenarioState.sequence; discardScenarioSession(); scenarioState.busy = false;
  renderScenarioTarget(); renderScenarioIdle('Движок изменён. Запустите сценарий для новой проверки.');
});
$('#scenario-target-select').addEventListener('change',event=>{
  if (event.target.value === 'current') return;
  if (event.target.value === '') { clearScenarioContext(); return; }
  const target = scenarioState.targets[Number(event.target.value)]; if (target) openScenario(target);
});
$('#scenario-rerun').addEventListener('click',()=>runScenario(scenarioState.active));
window.addEventListener('pagehide',clearScenarioContext);
renderScenarioButtons(); renderScenarioIdle();
