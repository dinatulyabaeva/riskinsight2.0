'use strict';
const $ = id => document.getElementById(id);
const state = {context:null, dashboard:null, busy:false, token:'', loadId:0, contextId:0, datasetId:'builtin', switching:false, importing:false, conversationId:null, completedTurns:0};
const ru = new Intl.NumberFormat('ru-RU');
const pct = v => v == null ? '—' : (v*100).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})+'%';
const money = v => {
  if (v == null) return '—';
  const unit = Math.abs(v)>=1e9 ? [1e9,'млрд ₽'] : Math.abs(v)>=1e6 ? [1e6,'млн ₽'] : [1,'₽'];
  return (v/unit[0]).toLocaleString('ru-RU',{maximumFractionDigits:2})+' '+unit[1];
};
const dateLabel = s => new Date(s+'T12:00:00Z').toLocaleDateString('ru-RU',{month:'short',year:'2-digit',timeZone:'UTC'}).replace(' г.','');
const colors = ['#6be3bd','#82a5c7','#a2a3dd','#e1ba7b','#d58489'];
function element(tag, className, text) { const e=document.createElement(tag); if(className)e.className=className; if(text!==undefined)e.textContent=text; return e; }
function showError(text) { $('global-error').textContent=text; $('global-error').hidden=!text; }
async function api(path, options={}) {
  const headers={...options.headers};
  if(typeof options.body==='string')headers['Content-Type']='application/json';
  if(state.token) headers.Authorization='Bearer '+state.token;
  const response=await fetch(path,{...options,headers});
  if(!response.ok){
    if(response.status===401 && !$('access-dialog').open) $('access-dialog').showModal();
    let data;try{data=await response.json();}catch{data={};}
    const detail=typeof data.detail==='string'?data.detail:(data.detail?.message||data.errors?.[0]?.message||`Ошибка HTTP ${response.status}`);
    const error=new Error(detail);error.report=data.status?data:null;error.status=response.status;error.requestId=data.request_id||response.headers.get('x-request-id');error.trace=data.trace;throw error;
  }
  const contentType=response.headers.get('content-type')||'';
  if(contentType.includes('application/json'))return response.json();
  if(contentType.includes('spreadsheetml')||contentType.includes('octet-stream'))return response.blob();
  return response.text();
}
function scope(){return {dataset_id:state.datasetId,as_of:$('date-filter').value||null,filters:{product:$('product-filter').value||null,region:$('region-filter').value||null,risk_band:$('rating-filter').value||null}};}
function scopeText(s){
  if(!s)return '';
  const filters=s.filters||{};
  const period=s.start_date&&s.end_date?`${s.start_date} — ${s.end_date}`:s.as_of||'последний доступный';
  return `Набор ${s.dataset_id||state.datasetId} · ${period} · ${filters.product||'все продукты'} · ${filters.region||'все регионы'} · ${filters.risk_band||'все рейтинги'}`;
}
function updateMemoryNote(){
  const live=state.context?.mode==='llm';
  $('composer-note').textContent=live?'Можно уточнить предыдущий вопрос. Смена области начинает новый анализ.':'DEMO: используйте готовые сценарии';
  $('memory-status').textContent=state.conversationId?`Диалог активен · ответов: ${state.completedTurns}`:'Новый диалог';
}
function resetConversation(reason='Начните новый анализ или выберите сценарий.'){
  state.conversationId=null;state.completedTurns=0;
  const welcome=element('div','welcome');welcome.append(element('div','welcome-mark','✧'),element('h3','','Исследуйте выбранный портфель'),element('p','',reason));
  $('conversation').replaceChildren(welcome);updateMemoryNote();
}
function requestIdentifier(){
  const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
  return Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
}
function setBusy(busy){state.busy=busy;const locked=busy||state.switching||state.importing;
  document.querySelectorAll('[data-scenario]').forEach(b=>b.disabled=locked||!state.context||!state.dashboard);
  ['dataset-filter','date-filter','product-filter','region-filter','rating-filter','reset-filters','xlsx-file','upload-button','new-analysis','load-example','download-example'].forEach(id=>{if($(id))$(id).disabled=locked;});
  document.querySelectorAll('.retry-analysis').forEach(b=>b.disabled=locked);
  $('send-button').disabled=locked||!state.dashboard||state.context?.mode!=='llm'||!state.context?.llm_configured;
}

function fillSelect(id,values,placeholder){const select=$(id);select.replaceChildren();if(placeholder)select.add(new Option(placeholder,''));values.forEach(v=>select.add(new Option(v,v)));}
async function initialize(){
  const contextId=++state.contextId;state.switching=true;state.loadId++;state.dashboard=null;clearDashboard();
  $('export-dashboard').disabled=true;setBusy(state.busy);
  try{
    showError('');
    const health=await api('/api/health');
    $('storage-note').hidden=health.storage!=='ephemeral';
    $('storage-note').textContent='На этом хостинге загруженные файлы и диалоги могут исчезнуть при перезапуске. Сохраните исходный XLSX и отчёт. Тестовый портфель можно снова открыть кнопкой выше.';
    if(health.auth_required&&!state.token){$('access-dialog').showModal();return;}
    await refreshDatasets();const ctx=await api('/api/context?dataset_id='+encodeURIComponent(state.datasetId));if(contextId!==state.contextId)return;state.context=ctx;
    $('dataset-tag').textContent=ctx.synthetic?'СИНТЕТИЧЕСКИЕ ДАННЫЕ':'ЗАГРУЖЕННЫЙ ПОРТФЕЛЬ';
    fillSelect('date-filter',[...ctx.dates].reverse());fillSelect('product-filter',ctx.filters.product,'Все продукты');fillSelect('region-filter',ctx.filters.region,'Все регионы');fillSelect('rating-filter',ctx.filters.risk_band,'Все рейтинги');
    const live=ctx.mode==='llm';$('mode-badge').textContent=live?'LLM':'DEMO';$('mode-badge').classList.toggle('live',live);
    $('mode-note').textContent=live?`Модель: ${ctx.model||'не задана'}. LLM выбирает анализ. Числа и графики строятся по результатам MCP.`:'Без LLM: готовые сценарии и шаблонные отчеты. MCP и расчеты настоящие. Для свободных вопросов настройте APP_MODE=llm.';
    $('question').disabled=!live||!ctx.llm_configured;updateMemoryNote();
    $('connection-dot').classList.add('online');$('connection-label').textContent='MCP подключен · stdio';await loadDashboard();await refreshImports();
  }catch(error){showError(error.message);$('connection-dot').classList.remove('online');$('connection-label').textContent='Нет подключения';state.context=null;}
  finally{if(contextId===state.contextId){state.switching=false;setBusy(state.busy);}}
}
function clearDashboard(){
  ['kpi-balance','kpi-npl','kpi-count','kpi-el','performing'].forEach(id=>$(id).textContent='—');
  $('kpi-rate').textContent='Средневзвешенная ставка —';$('kpi-delta').textContent='Загрузка…';
  $('metric-availability').hidden=true;$('npl-calculation').textContent='Загрузка выбранной области…';
  $('trend-chart').replaceChildren(element('div','loading-placeholder','Загрузка данных через MCP…'));
  $('bucket-legend').replaceChildren();$('segments-body').replaceChildren();$('donut').style.background='#243945';
}
async function loadDashboard(){
  const loadId=++state.loadId;state.dashboard=null;clearDashboard();setBusy(state.busy);const s=scope();const params=new URLSearchParams();
  Object.entries({dataset_id:s.dataset_id,as_of:s.as_of,...s.filters}).forEach(([k,v])=>{if(v)params.set(k,v);});
  try{
    showError('');$('export-dashboard').disabled=true;
    const d=await api('/api/dashboard?'+params.toString());if(loadId!==state.loadId)return;
    state.dashboard=d;renderDashboard(d);$('export-dashboard').disabled=false;
  }catch(error){if(loadId===state.loadId)showError(error.message);}
  finally{if(loadId===state.loadId)setBusy(state.busy);}
}
function renderDashboard(d){
  const m=d.summary.metrics;
  const calc=d.summary.npl90_calculation;$('npl-calculation').textContent=`DPD 90+ = ${ru.format(calc.numerator_rub)} / ${ru.format(calc.denominator_rub)} ₽ · ${calc.formula} · ${calc.as_of} · ${calc.dataset_id}`;
  const missing=Object.entries(m.unavailable_metrics||{}).map(([k,v])=>`${k}: ${v}`);$('metric-availability').textContent=missing.join('; ');$('metric-availability').hidden=!missing.length;
$('kpi-balance').textContent=money(m.balance_rub);$('kpi-npl').textContent=pct(m.npl90_ratio);$('kpi-count').textContent=ru.format(m.loan_count);$('kpi-el').textContent=money(m.el_rub);$('kpi-rate').textContent='Средневзвешенная ставка '+pct(m.weighted_rate);
  const delta=d.trend.npl90_delta_pp;
  $('kpi-delta').textContent=delta==null?'Недостаточно данных':`${delta>=0?'+':''}${delta.toLocaleString('ru-RU',{maximumFractionDigits:2})} п.п. за ${d.trend.points.length} срезов`;
  $('kpi-delta').classList.toggle('up',delta>0);$('period-label').textContent=d.trend.points.length+' срезов';
  $('trend-caption').textContent=dateLabel(d.trend.start_date)+' — '+dateLabel(d.trend.end_date);drawTrend(d.trend.points);
  drawBuckets(d.delinquency.buckets);drawSegments(d.segments.segments);
}
const ns='http://www.w3.org/2000/svg';
function svgEl(tag,attrs,text){const el=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));if(text!==undefined)el.textContent=text;return el;}
function drawTrend(points){
  const container=$('trend-chart');container.replaceChildren();
  const available=points.filter(p=>p.npl90_ratio!=null);if(!available.length){container.append(element('div','loading-placeholder','Нет данных для выбранной области'));return;}
  const width=530,height=190,pad={left:48,right:18,top:20,bottom:32};
  const svg=svgEl('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':'DPD 90+ по месячным срезам'});
  const max=Math.max(...available.map(p=>p.npl90_ratio*100));const top=Math.max(1,Math.ceil(max*1.2*2)/2);
  const x=i=>pad.left+i*(width-pad.left-pad.right)/Math.max(1,points.length-1);
  const y=v=>height-pad.bottom-v/top*(height-pad.top-pad.bottom);
  for(let i=0;i<=3;i++){const v=top*i/3;svg.append(svgEl('line',{x1:pad.left,y1:y(v),x2:width-pad.right,y2:y(v),stroke:'#2b3843','stroke-width':1,'stroke-dasharray':'3 5'}));svg.append(svgEl('text',{x:pad.left-9,y:y(v)+3,'text-anchor':'end',fill:'#8096a6','font-size':9},v.toFixed(1).replace('.',',')+'%'));}
  const defs=svgEl('defs',{}),gradient=svgEl('linearGradient',{id:'trendFill',x1:0,y1:0,x2:0,y2:1});gradient.append(svgEl('stop',{offset:'0%','stop-color':'#6be3bd','stop-opacity':'.18'}),svgEl('stop',{offset:'100%','stop-color':'#6be3bd','stop-opacity':'0'}));defs.append(gradient);svg.append(defs);
  // Split on missing values: a null observation must not be drawn as zero or interpolated.
  let segment=[];const chunks=[];points.forEach((p,i)=>{if(p.npl90_ratio==null){if(segment.length)chunks.push(segment);segment=[];}else segment.push([x(i),y(p.npl90_ratio*100)]);});if(segment.length)chunks.push(segment);
  chunks.forEach(chunk=>{const line=chunk.map((p,i)=>(i?'L':'M')+p.join(',')).join(' ');svg.append(svgEl('path',{d:line+` L${chunk.at(-1)[0]},${y(0)} L${chunk[0][0]},${y(0)} Z`,fill:'url(#trendFill)'}));svg.append(svgEl('path',{d:line,fill:'none',stroke:'#6be3bd','stroke-width':2.5,'stroke-linejoin':'round'}));});
  points.forEach((p,i)=>{svg.append(svgEl('text',{x:x(i),y:height-10,'text-anchor':'middle',fill:'#8096a6','font-size':9},dateLabel(p.as_of)));if(p.npl90_ratio!=null){const c=svgEl('circle',{cx:x(i),cy:y(p.npl90_ratio*100),r:i===points.length-1?4:3,fill:'#6be3bd',stroke:'#14232b','stroke-width':2});c.append(svgEl('title',{},`${p.as_of}: ${pct(p.npl90_ratio)}`));svg.append(c);}});container.append(svg);
}
function drawBuckets(rows){
  let position=0;const stops=[];$('bucket-legend').replaceChildren();
  rows.forEach((r,i)=>{const share=r.balance_share||0;stops.push(`${colors[i]} ${position}% ${position+share*100}%`);position+=share*100;
    const item=element('div','bucket-row');const dot=element('i');dot.style.background=colors[i];const bar=element('span','bar');const fill=element('span');fill.style.width=share*100+'%';fill.style.background=colors[i];bar.append(fill);item.append(dot,element('span','b-label','DPD '+r.bucket),bar,element('strong','',pct(r.balance_share)));$('bucket-legend').append(item);
  });$('donut').style.background=position?'conic-gradient('+stops.join(',')+')':'#243945';$('performing').textContent=pct(rows[0]?.balance_share);
}
function drawSegments(rows){$('segments-body').replaceChildren();rows.forEach(r=>{const tr=element('tr');tr.append(element('td','',r.segment),element('td','',money(r.balance_rub)));const td=element('td');td.append(element('span','ratio-chip',pct(r.npl90_ratio)));tr.append(td,element('td','',pct(r.portfolio_share)));$('segments-body').append(tr);});if(!rows.length){const tr=element('tr');const td=element('td','','Нет данных');td.colSpan=4;tr.append(td);$('segments-body').append(tr);}}
function download(name,content,type){const blob=new Blob([content],{type});const url=URL.createObjectURL(blob);const a=element('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function appendReferencedText(container,text,traceElements){const parts=text.split(/(\[T\d+\])/g);parts.forEach(p=>{const match=p.match(/^\[(T\d+)\]$/);if(match&&traceElements[match[1]]){const b=element('button','source-ref',p);b.type='button';b.addEventListener('click',()=>{const {outer,inner}=traceElements[match[1]];outer.open=true;inner.open=true;inner.scrollIntoView({behavior:'smooth',block:'nearest'});});container.append(b);}else container.append(document.createTextNode(p));});}
function renderResult(result,container){
  const trace=Array.isArray(result.trace)?result.trace:[];
  const provenance=result.provenance||{};
  const label=provenance.llm_called?'✧ RiskInsight · LLM → MCP':result.mode==='demo'?'▦ RiskInsight · DEMO':'▦ RiskInsight · расчёт MCP';
  const head=element('div','answer-head');head.append(element('span','',label),element('small','',`${(result.duration_ms/1000).toFixed(1)} c · ${trace.length} вызова MCP`));container.append(head);
  if(provenance.llm_called){
    const source=provenance.narrative_source==='mcp_renderer'?'Модель выбрала инструменты. Числовой ответ и графики сформированы из MCP.':'Модель выбрала инструменты и сформировала ответ. Числа графика получены из MCP.';
    container.append(element('p','provenance-note',source));
  }
  const resolved=result.resolved_scope||result.scope;
  if(resolved)container.append(element('p','resolved-scope','Область ответа: '+scopeText(resolved)));
  const outer=element('details','trace');outer.append(element('summary','',`Источники и вызовы MCP · ${trace.length}`));const body=element('div','trace-body');const traceElements={};
  trace.forEach(t=>{const detail=element('details','trace-call');detail.append(element('summary','',`[${t.ref}] ${t.ok?'✓':'!'} ${t.tool} · ${t.duration_ms} мс`));detail.append(element('pre','',JSON.stringify({arguments:t.arguments,result:t.result,error:t.error},null,2)));body.append(detail);traceElements[t.ref]={outer,inner:detail};});
  const protocol=element('details','trace-call');protocol.append(element('summary','','MCP handshake / JSON-RPC'));protocol.append(element('pre','',JSON.stringify(result.protocol,null,2)));body.append(protocol);outer.append(body);
  const answer=element('div','answer');appendReferencedText(answer,String(result.answer||''),traceElements);container.append(answer);
  const openSource=ref=>{const source=traceElements[ref];if(source){source.outer.open=true;source.inner.open=true;source.inner.scrollIntoView({behavior:'smooth',block:'nearest'});}};
  (result.visualizations||[]).forEach(chart=>{const rendered=window.RiskEvidenceCharts.render(chart,trace,openSource);if(rendered)container.append(rendered);});
  (result.warnings||[]).forEach(w=>container.append(element('div','warning',w)));container.append(outer);
  const actions=element('div','result-actions');
  const md=element('button','','↓ Отчет .md');md.type='button';
  md.addEventListener('click',()=>download('riskinsight-report.md',`# RiskInsight\n\nРежим: ${result.mode}\nRequest: ${result.request_id}\n\n${scopeText(resolved)}\n\n${result.answer}\n\n## Происхождение ответа\n\n\`\`\`json\n${JSON.stringify(provenance,null,2)}\n\`\`\`\n\n## Данные графиков\n\n\`\`\`json\n${JSON.stringify(result.visualizations||[],null,2)}\n\`\`\`\n\n## Источники MCP\n\n\`\`\`json\n${JSON.stringify(trace,null,2)}\n\`\`\`\n`,'text/markdown;charset=utf-8'));
  const js=element('button','','↓ Журнал .json');js.type='button';js.addEventListener('click',()=>download('riskinsight-evidence.json',JSON.stringify(result,null,2),'application/json'));actions.append(md,js);container.append(actions);
  if(result.request_id)container.append(element('p','request-id','Запрос: '+result.request_id));
}
async function executeTurn(turn){
  if(state.busy||state.switching||state.importing||turn.completed)return;
  turn.output.replaceChildren(element('div','analysis-loading',state.context.mode==='llm'?'Модель выбирает анализ; MCP получает данные…':'Выполняются MCP-инструменты сценария…'));
  setBusy(true);
  const started=performance.now();
  const timer=setInterval(()=>{const loading=turn.output.querySelector('.analysis-loading');if(loading)loading.textContent=`${state.context.mode==='llm'?'Ожидаем модель и MCP':'Ожидаем MCP'} · ${Math.round((performance.now()-started)/1000)} с. Повторный запрос не отправляется.`;},1000);
  try{
    const result=await api('/api/chat',{method:'POST',body:JSON.stringify(turn.payload)});
    turn.output.replaceChildren();turn.output.className='result-block';
    if(result.conversation_reset){state.completedTurns=0;turn.output.append(element('p','warning','Контекст предыдущего диалога завершился. Ответ рассчитан для нового анализа.'));}
    state.conversationId=result.conversation_id||null;state.completedTurns++;turn.completed=true;updateMemoryNote();
    renderResult(result,turn.output);turn.output.scrollIntoView({behavior:'smooth',block:'nearest'});
    if($('question').value.trim()===turn.payload.question)$('question').value='';
  }catch(error){
    turn.output.replaceChildren();turn.output.className='failed-turn';
    const alert=element('div','analysis-error');alert.setAttribute('role','alert');alert.append(element('p','',error.message));
    if(error.requestId)alert.append(element('p','request-id','Код запроса: '+error.requestId));
    alert.append(element('p','error-help',error.status===429?'Лимит запросов исчерпан. Подождите перед повтором.':error.status===401?'Укажите токен доступа, затем повторите запрос.':'Ответ не засчитан как успешный. Предыдущий контекст и вопрос сохранены.'));
    const retry=element('button','secondary retry-analysis','Повторить этот вопрос');retry.type='button';retry.addEventListener('click',()=>executeTurn(turn));alert.append(retry);turn.output.append(alert);
    if(Array.isArray(error.trace)&&error.trace.length){const details=element('details','trace');details.append(element('summary','','MCP-журнал неудачного запроса'),element('pre','error-trace',JSON.stringify(error.trace,null,2)));turn.output.append(details);}
  }finally{clearInterval(timer);setBusy(false);}
}
async function analyze(scenario=null){
  if(state.busy||state.switching||state.importing||!state.context||!state.dashboard)return;
  const question=scenario?state.context.scenarios[scenario]:$('question').value.trim();if(!question||question.length<3)return;
  const conv=$('conversation');conv.querySelector('.welcome')?.remove();conv.append(element('div','user-message',question));
  const selectedScope=scope();conv.append(element('div','section-label question-scope',scopeText(selectedScope)));
  const output=element('div','pending-turn');conv.append(output);conv.scrollTop=conv.scrollHeight;
  const turn={payload:{question,scenario,...selectedScope,conversation_id:state.conversationId,client_request_id:requestIdentifier()},output,completed:false};
  await executeTurn(turn);
}
async function refreshDatasets(){
  const data=await api('/api/datasets');const select=$('dataset-filter');select.replaceChildren();
  data.datasets.forEach(d=>select.add(new Option(d.name+(d.row_count?` · ${d.row_count} строк`:''),d.dataset_id)));
  if(!data.datasets.some(d=>d.dataset_id===state.datasetId)){
    state.datasetId='builtin';resetConversation('Прежний загруженный набор недоступен после перезапуска. Выбран встроенный портфель; откройте тестовый файл или загрузите свой снова.');
  }
  select.value=state.datasetId;
}
function renderImport(report,container){
  if(report.reused){container.replaceChildren(element('p','',`Тестовый портфель уже сохранён и выбран: ${report.dataset_id}. Повторная копия не создаётся.`));return;}
  container.replaceChildren();container.append(element('p','',`${report.status==='imported'?'Импорт сохранён':'Импорт отклонён'}. Строк: ${report.row_count}; корректных: ${report.valid_rows}; с ошибками: ${report.invalid_rows}; ошибок: ${report.error_count}.`));
  if(report.dataset_id)container.append(element('p','',`Набор ${report.dataset_id} сохранён. Текущий набор указан в переключателе портфеля.`));
  (report.warnings||[]).forEach(w=>container.append(element('p','warning',w)));
  if(report.errors?.length){const table=element('table');const heading=element('tr');['Строка','Колонка','Ошибка'].forEach(t=>heading.append(element('th','',t)));table.append(heading);
    report.errors.forEach(e=>{const row=element('tr');[e.row??'—',e.column??'Файл',e.message].forEach(t=>row.append(element('td','',t)));table.append(row);});const wrap=element('div','table-wrap');wrap.append(table);container.append(wrap);}
  if(report.errors_truncated)container.append(element('p','','Показаны первые 100 ошибок. Исправьте файл и повторите загрузку.'));
}
async function refreshImports(){const data=await api('/api/imports');$('import-history').replaceChildren();data.imports.forEach(report=>{const entry=element('details');entry.append(element('summary','',`${report.created_at} · ${report.status} · строк ${report.row_count}`));const body=element('div');renderImport(report,body);entry.append(body);$('import-history').append(entry);});}
$('dataset-filter').addEventListener('change',()=>{state.datasetId=$('dataset-filter').value;resetConversation('Выбран другой набор. Старые данные и контекст не используются.');initialize();});
$('upload-form').addEventListener('submit',async event=>{
  event.preventDefault();if(state.busy||state.switching||state.importing)return;
  const file=$('xlsx-file').files[0];if(!file){showError('Выберите файл .xlsx');return;}
  if(!file.name.toLowerCase().endsWith('.xlsx')){showError('Допускаются только файлы .xlsx');return;}
  if(file.size>10*1024*1024){showError('Размер XLSX превышает 10 МБ');return;}
  state.importing=true;setBusy(state.busy);showError('');$('import-result').textContent='Проверка файла…';
  try{const report=await api('/api/datasets/import?filename='+encodeURIComponent(file.name),{method:'POST',body:file});renderImport(report,$('import-result'));if(report.dataset_id){state.datasetId=report.dataset_id;resetConversation('Файл импортирован и выбран. Теперь вопросы относятся к его данным.');await initialize();}else await refreshDatasets();}
  catch(error){if(error.report)renderImport(error.report,$('import-result'));else $('import-result').textContent=error.message;}
  finally{try{await refreshImports();}catch(error){showError(error.message);}state.importing=false;setBusy(state.busy);}
});
['date-filter','product-filter','region-filter','rating-filter'].forEach(id=>$(id).addEventListener('change',()=>{resetConversation('Область анализа изменилась. Начат новый диалог.');loadDashboard();}));
$('reset-filters').addEventListener('click',()=>{$('date-filter').value=state.context.latest_date;['product-filter','region-filter','rating-filter'].forEach(id=>$(id).value='');resetConversation('Фильтры сброшены. Анализируется весь выбранный портфель.');loadDashboard();});
$('new-analysis').addEventListener('click',()=>{resetConversation('Память диалога очищена. Набор данных и фильтры сохранены.');$('question').value='';$('question').focus();});
$('download-example').addEventListener('click',async()=>{
  try{const blob=await api('/api/examples/portfolio-scenarios.xlsx');download('portfolio-scenarios.xlsx',blob,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');}catch(error){showError(error.message);}
});
$('load-example').addEventListener('click',async()=>{
  if(state.busy||state.switching||state.importing)return;
  state.importing=true;setBusy(state.busy);showError('');$('import-result').textContent='Проверка тестового портфеля…';
  try{const report=await api('/api/datasets/example',{method:'POST'});renderImport(report,$('import-result'));if(report.dataset_id){state.datasetId=report.dataset_id;resetConversation('Выбран тестовый портфель со сценариями. Начните с динамики риска и задайте уточняющий вопрос.');await initialize();}}
  catch(error){$('import-result').textContent=error.message;}
  finally{state.importing=false;setBusy(state.busy);}
});
document.querySelectorAll('[data-scenario]').forEach(b=>b.addEventListener('click',()=>analyze(b.dataset.scenario)));
$('chat-form').addEventListener('submit',e=>{e.preventDefault();analyze();});
$('question').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!$('send-button').disabled)analyze();}});
$('export-dashboard').addEventListener('click',()=>{if(state.dashboard)download('riskinsight-dashboard.json',JSON.stringify(state.dashboard,null,2),'application/json');});
$('method-button').addEventListener('click',async()=>{$('method-dialog').showModal();try{$('method-text').textContent=await api('/api/methodology');}catch(e){$('method-text').textContent=e.message;}});
$('access-button').addEventListener('click',()=>$('access-dialog').showModal());
$('access-form').addEventListener('submit',e=>{e.preventDefault();state.token=$('access-token').value.trim();$('access-token').value='';$('access-dialog').close();initialize();});
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
initialize();
