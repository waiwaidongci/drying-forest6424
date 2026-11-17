import {research} from '../stores/research.js';
import {session} from '../stores/session.js';
import {devicesApi,batchesApi} from '../api/research.js';
import {batchLedger,windowTable,metrics} from '../components/common/business.js';
import {table} from '../components/common/table.js';
import {esc,formObject,localTime,fmt} from '../utils/format.js';
import {action,bind,toast} from '../hooks/actions.js';

const KEY='device-seal-key-';

function sealKey(id){const k=KEY+id;let v=localStorage.getItem(k);if(!v){v='seal-'+id+'-'+Math.random().toString(36).slice(2,10);localStorage.setItem(k,v)}return v}

export async function devicePage(){
  if(!research.devices.length){research.devices=await devicesApi.list()}
  if(!research.deviceId&&research.devices.length)research.deviceId=research.devices[0].id
  research.deviceState=research.deviceId?await devicesApi.state(research.deviceId):null
  const st=research.deviceState;
  const deviceOptions=research.devices.map(d=>'<option value="'+d.id+'" '+(d.id==research.deviceId?'selected':'')+'>'+esc(d.name)+' · '+esc(d.key)+'</option>').join('');
  if(!st)return '<section class="page-heading"><p class="eyebrow">02 / SAMPLE ASSEMBLY</p><h1>先让设备日志报齐，再生成研究样本。</h1><p>逐点平均会把采样疏密当成权重。这里按试件工序起止窗，用窗前前值与窗内读数做时间加权；隔离与报齐状态先于计算可见。</p></section><div class="empty"><strong>还没有设备日志流。</strong><p>先在来源下登记设备，再按试件工序起止窗接收读数。</p></div>'+(session.writable?newDeviceForm():'');
  const d=st.device;const counts=st.counts;
  const openConflicts=st.conflicts.filter(c=>c.status==='open');
  const openDisputes=st.disputes.filter(x=>x.status==='open');
  return '<section class="page-heading"><p class="eyebrow">02 / SAMPLE ASSEMBLY</p><h1>先让设备日志报齐，再生成研究样本。</h1><p>逐点平均会把采样疏密当成权重。这里按试件工序起止窗，用窗前前值（LOCF）与窗内读数做时间加权；窗前无值、断采超限、待报齐与同刻异值都会先呈现在这里。</p></section>'
    +'<label class="compact">设备日志流<select id="device-select">'+deviceOptions+'</select></label>'
    +'<div class="run-title"><h2>'+esc(d.name)+'</h2><span class="status effective">'+esc(d.key)+'</span><small>来源 '+esc(d.source_name)+' · 断采上限 '+d.gap_limit_seconds+' 秒</small></div>'
    +'<p class="fine">设备报齐线：'+(d.reported_through?esc(localTime(d.reported_through))+'（北京时间）':'尚未确认')+' · 封窗只使用报齐线以前的读数；更早迟到值保留争议。</p>'
    +metrics([{label:'试件工序窗',value:counts.windows},{label:'待报齐',value:counts.pending},{label:'隔离',value:counts.quarantined_preview},{label:'可生效预览',value:counts.effective_preview},{label:'封窗后待重算',value:counts.dirty},{label:'开放争议',value:counts.disputes}])
    +'<div class="result-grid"><section><h2>工序窗与生效依据 <small>按工序开始排序</small></h2>'+windowTable(st.windows)+'</section><aside class="method-sheet"><p class="eyebrow">ASSEMBLY RULES</p><ol><li>窗前前值：取工序开始前最近读数，沿用到首个窗内读数。</li><li>窗内读数按发生时刻覆盖到下一读数或工序结束。</li><li>工序结束后的读数不补窗内缺口。</li><li>窗前无值或窗内最大间隔超限 → 隔离，不进入研究。</li><li>设备确认报齐至工序结束才可封窗；同刻异值阻止封窗。</li><li>封窗后更早迟到值只留争议，不改已封样本。</li></ol><p class="fine">公式 '+esc(d.key)+' · LOCF_TIME_WEIGHTED_V1 · 采样疏密不同的设备使用同一条时间加权口径。</p></aside></div>'
    +'<h2>封窗批次 <small>研究只冻结完整批次；同批整批一次生效</small></h2>'+batchLedger(st.batches)
    +(openConflicts.length?'<h2>同刻异值待裁定 <small>阻止封窗</small></h2>'+table(openConflicts.map(c=>({observed_at:localTime(c.observed_at),candidates:c.candidate_values.join(' / '),status:'待裁定'})),[{key:'observed_at',label:'读数时刻'},{key:'candidates',label:'候选值 ℃'},{key:'status',label:'状态'}])+conflictForms(openConflicts):'')
    +(st.conflicts.length?'<details><summary>全部同刻异值记录（'+st.conflicts.length+'）</summary>'+table(st.conflicts.map(c=>({observed_at:localTime(c.observed_at),values:(c.candidate_values||[]).join(' / '),status:c.status==='open'?'待裁定':'已裁定 '+fmt(c.chosen_value),by:c.resolved_by||''})),[{key:'observed_at',label:'读数时刻'},{key:'values',label:'候选值 ℃'},{key:'status',label:'状态'},{key:'by',label:'裁定人'}])+'</details>':'')
    +'<h2>迟到争议 <small>封窗后迟到值不改已封样本</small></h2><p class="fine">报齐线以前的补报会标记为迟到；只有落在已封窗口或晚于已封窗前值的迟到值才进入争议。</p>'+(st.disputes.length?table(st.disputes.map(x=>({observed_at:localTime(x.observed_at),value:x.value,batch:'#'+(x.batch_id??'—'),status:x.status==='open'?'待复核':'已复核',note:x.note})),[{key:'observed_at',label:'读数时刻'},{key:'value',label:'值 ℃'},{key:'batch',label:'已封批次'},{key:'status',label:'状态'},{key:'note',label:'说明'}]):'<div class="empty">暂无迟到争议。</div>')
    +(openDisputes.length&&session.writable?disputeForms(openDisputes,st):'')
    +(session.writable?operationsPanel(d,st):''
    );
}

function conflictForms(conflicts){return '<div class="entry-sheet">'+conflicts.map(c=>'<form class="inline-form" data-conflict="'+c.id+'"><label>'+esc(localTime(c.observed_at))+' 裁定值<select name="value">'+c.candidate_values.map(v=>'<option value="'+v+'">'+v+' ℃</option>').join('')+'</select></label><button type="submit">裁定并解除封窗阻止</button></form>').join('')+'</div>'}
function disputeForms(disputes,st){const refs=Object.fromEntries((st.windows||[]).map(w=>[w.id,w.reference]));return '<div class="entry-sheet"><h2>复核争议</h2>'+disputes.map(x=>'<form class="inline-form" data-dispute="'+x.id+'"><label>'+esc(localTime(x.observed_at))+' · '+esc(refs[x.window_id]||('窗口 #'+x.window_id))+'<input name="note" value="已复核，保留原封样本"></label><button type="submit">标记已复核</button></form>').join('')+'</div>'}
function newDeviceForm(){return '<div class="entry-sheet"><h2>登记设备日志流</h2><form id="new-device"><label>所属来源<select name="source_id">'+sourceOptions(research.sources,research.sourceId)+'</select></label><label>设备标识<input name="key" required placeholder="DEV-HX-02"></label><label>设备名称<input name="name" required placeholder="二号热风干燥箱 · 温度日志"></label><label>断采上限（秒）<input name="gap_limit_seconds" type="number" min="1" value="300" required></label><button type="submit">登记设备</button></form></div>'}
function sourceOptions(sources,id){return sources.map(s=>'<option value="'+s.id+'" '+(s.id==id?'selected':'')+'>'+esc(s.name)+'</option>').join('')}

function operationsPanel(d,st){
  return '<div class="split operations"><section class="entry-sheet"><h2>推进设备报齐线</h2><p class="fine">只有设备确认已报齐至某时刻后，才允许把该时刻之前的工序窗封窗。报齐线只能前进。</p><form id="watermark-form"><label>已报齐至（北京时间）<input name="reported_through" required placeholder="2026-11-04T17:20:00+08:00" value="'+(d.reported_through?esc(localTimeInput(d.reported_through)):'')+'"></label><button type="submit">推进报齐线</button></form></section>'
    +'<section class="entry-sheet"><h2>登记试件工序窗</h2><form id="window-form"><div class="parameter-fields"><label>试件编号<input name="reference" required placeholder="HX-202611-01-D4-002"></label><label>初始湿度 %<input name="moisture" type="number" step="any" required value="35"></label><label>厚度 mm<input name="thickness" type="number" step="any" required value="4.2"></label><label>风量 m³/min<input name="airflow" type="number" step="any" required value="3.1"></label><label>实测时长 min<input name="minutes" type="number" step="any" required value="83"></label><label>工序开始<input name="started_at" required placeholder="2026-11-05T09:00:00+08:00"></label><label>工序结束<input name="ended_at" required placeholder="2026-11-05T09:20:00+08:00"></label></div><button type="submit">登记工序窗</button></form></section></div>'
    +'<div class="split operations"><section class="entry-sheet"><h2>接收设备读数（可乱序补报）</h2><p class="fine">每行“时刻,温度”，时刻需带时区。重复同值幂等；同刻异值进入待裁定且阻止封窗。</p><form id="reading-form"><label>读数列表<textarea name="readings" rows="6" required placeholder="2026-11-05T09:02:00+08:00,71.2&#10;2026-11-05T09:06:00+08:00,72.8"></textarea></label><button type="submit">接收读数并重算受影响窗口</button></form></section>'
    +'<section class="entry-sheet"><h2>封窗并生成整批样本</h2><p class="fine">整批写入后一次生效；失败可恢复，重试复用同一幂等键，不会重复落样本。</p><form id="seal-form"><label>幂等键<input name="idempotency_key" required value="'+esc(sealKey(d.id))+'"></label><button type="submit">封窗并生成样本 →</button></form><p class="fine">当前可生效预览 '+st.counts.effective_preview+' · 隔离 '+st.counts.quarantined_preview+' · 待报齐 '+st.counts.pending+' · 开放同刻异值 '+st.counts.conflicts+'</p></section></div>';
}
function localTimeInput(value){const d=new Date(value);const parts=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).formatToParts(d);const get=t=>parts.find(p=>p.type===t).value;return get('year')+'-'+get('month')+'-'+get('day')+'T'+get('hour')+':'+get('minute')+':'+get('second')+'+08:00'}

export function bindDevice(refresh){
  bind('#device-select','change',async e=>{research.deviceId=Number(e.target.value);await refresh()});
  action('#new-device',async form=>{const data=formObject(form);const d=await devicesApi.create({source_id:Number(data.source_id),key:data.key,name:data.name,gap_limit_seconds:Number(data.gap_limit_seconds)});research.deviceId=d.id;research.devices=await devicesApi.list();await refresh();toast('设备日志流已登记')});
  action('#watermark-form',async form=>{await devicesApi.watermark(research.deviceId,formObject(form).reported_through);await refresh();toast('报齐线已推进；受影响窗口将重算')});
  action('#window-form',async form=>{const data=formObject(form);await devicesApi.addWindows(research.deviceId,[{reference:data.reference,started_at:data.started_at,ended_at:data.ended_at,moisture:Number(data.moisture),thickness:Number(data.thickness),airflow:Number(data.airflow),minutes:Number(data.minutes)}]);await refresh();toast('工序窗已登记')});
  action('#reading-form',async form=>{const readings=formObject(form).readings.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{const [observed_at,value]=line.split(',');return {observed_at:observed_at.trim(),value:Number(value)}});const r=await devicesApi.ingest(research.deviceId,readings);await refresh();toast('新增 '+r.added+' 点，重复 '+r.duplicates+' 点，冲突 '+r.conflicts.length+' 处，争议 '+r.disputes.length+' 条')});
  action('#seal-form',async form=>{const r=await devicesApi.seal(research.deviceId,formObject(form).idempotency_key);localStorage.removeItem(KEY+research.deviceId);await refresh();toast('批次 #'+r.id+' 已封窗：'+r.summary.effective+' 条生效，'+r.summary.quarantined+' 条隔离')});
  for(const f of document.querySelectorAll('[data-conflict]'))f.addEventListener('submit',async e=>{e.preventDefault();await devicesApi.resolveConflict(research.deviceId,Number(f.dataset.conflict),Number(formObject(f).value));await refresh();toast('冲突已裁定')});
  for(const f of document.querySelectorAll('[data-dispute]'))f.addEventListener('submit',async e=>{e.preventDefault();await devicesApi.resolveDispute(research.deviceId,Number(f.dataset.dispute),formObject(f).note);await refresh();toast('争议已复核')});
  for(const b of document.querySelectorAll('[data-resume-batch]'))b.onclick=async()=>{try{const r=await batchesApi.resume(b.dataset.resumeBatch);await refresh();toast('批次 #'+r.id+' 已恢复并封窗')}catch(e){toast(e.message,true)}};
}
