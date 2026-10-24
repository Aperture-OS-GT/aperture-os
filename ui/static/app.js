const $ = id => document.getElementById(id);
let logs = [], paused = false, rows = [], busy = false;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function chat(text, user=false) {
  const item = document.createElement('div'); item.className = 'chat-message'+(user?' user':'');
  item.innerHTML = `<span class="avatar">${user?'↗':'◈'}</span><div><small>${user?'YOU':'APERTURE'} <b>JUST NOW</b></small><p>${esc(text)}</p></div>`;
  $('chat').append(item); $('chat').scrollTop = $('chat').scrollHeight;
}
function renderPins(m, evidence=false) {
  if (!m?.physical) return;
  const i=m.injected, p=m.physical, t=m.telemetry;
  const pins = [[17,4,'Trigger',i.trig],[18,5,'PWM',i.pwm],[27,18,'Fault',i.fault],[22,19,'Actuator',p['22']],[23,21,'Safety',p['23']]];
  $('pins').innerHTML=pins.map(([pi,esp,label,val],idx)=>{
    const pwm=idx===1, cls=pwm?'pwm':val?'on':'';
    const state=pwm?`${Math.round(val*100)}%`:val?'HIGH':'LOW';
    return `<div class="pin-row ${cls} ${idx>2?'reverse':''}"><div class="pin-end ${cls}"><span>${pi} <small>${label}</small></span><b>${state}</b></div><div class="wire"></div><div class="pin-end ${cls}"><span>GPIO ${esp}</span><b>${state}</b></div></div>`;
  }).join('');
  $('claims').innerHTML=[['Actuator','actuator','22'],['Safety relay','safety','23']].map(([label,key,pin])=>`<div class="claim ${t[key]!==p[pin]?'mismatch':''}"><span>${label} ${t[key]!==p[pin]?'≠':''}</span><strong>${t[key]?'HIGH · 1':'LOW · 0'}</strong><strong>${p[pin]?'HIGH · 1':'LOW · 0'}</strong></div>`).join('');
  $('dutState').textContent=t.state;
  $('evidenceNote').textContent=evidence?'⚑ Captured failure evidence · rig recovered to idle after plan.':'Live observation · independent physical readback.';
}
function renderSteps() {
  $('stepCount').textContent=`${rows.length} STEPS`;
  $('steps').innerHTML=rows.map((r,n)=>`<tr><td><span style="color:#647089;margin-right:10px">${String(n+1).padStart(2,'0')}</span>${esc(r.step||'Tool observation')}</td><td>${esc(r.reasoning||'Independent physical verification')}</td><td>${esc(JSON.stringify(r.injected||{}))}</td><td>${esc(r.latency_ms)} ms</td><td><span class="badge ${r.issues?.length?'fail':''}">${r.issues?.length?'✕ FAIL':'✓ PASS'}</span></td></tr>`).join('');
}
function report(r) {
  if(!r) return;
  rows=r.steps; renderSteps();
  const mismatch=rows.find(row=>row.issues?.some(i=>i.includes('DISCREPANCY')));
  $('verdict').className='verdict '+(r.pass?'pass':'fail');
  $('verdictTitle').textContent=mismatch?'FIRMWARE LIED':r.pass?'Physical verification passed':'Verification failed';
  $('verdictDetail').textContent=mismatch?mismatch.issues[0]:r.pass?'Every observed Serial claim agrees with independent pin readback.':'Review the test sequence and wire log for details.';
  $('verdictMeta').textContent=`${rows.filter(r=>r.issues?.length).length} FAILED / ${rows.length} OBSERVATIONS`;
  if(mismatch) renderPins(mismatch,true);
}
function renderLogs(){
  if(paused) return;
  const filter=$('filter').value;
  $('logs').innerHTML=logs.filter(e=>filter==='ALL'||e.level===filter).slice(-150).map(e=>`<div class="log ${esc(e.level)}"><time>${esc(e.timestamp.slice(11,23))}</time><b>${esc(e.level)}</b>${esc(JSON.stringify(e.data))}${e.latency_ms!=null?` · ${e.latency_ms.toFixed(2)}ms`:''}</div>`).join('');
  $('logs').scrollTop=$('logs').scrollHeight;
}
async function api(path,body){
  const res=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const data=await res.json(); if(!res.ok) throw Error(data.error||res.statusText); return data;
}
async function state(){
  const s=await api('/api/state'), c=s.connection;
  $('mode').value=c.mode; $('host').value=c.host; $('port').value=c.port; $('bug').checked=c.demo_bug;
  $('status').textContent=c.connected?'Connected':'Disconnected'; $('statusDot').style.background=c.connected?'#63ddbd':'#ff7787';
  $('build').textContent=c.demo_bug?'PLANTED':'FIXED'; $('bug').disabled=c.mode!=='sim';
  $('planner').textContent=s.llm?'LLM CONNECTED':'OFFLINE ENGINE';
  renderPins(s.measurement); report(s.report);
}
async function run(prompt){
  if(busy) return; busy=true; $('send').disabled=true; $('connect').disabled=true;
  rows=[]; renderSteps(); $('verdictTitle').textContent='Verifying hardware…';
  try {const result=await api('/api/prompt',{prompt}); report(result.report);}
  catch(e){chat(e.message); $('verdictTitle').textContent='Instruction could not complete';}
  finally{busy=false; $('send').disabled=false; $('connect').disabled=false;}
}
$('promptForm').onsubmit=e=>{e.preventDefault(); const text=$('prompt').value.trim(); if(text){run(text);$('prompt').value='';}};
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>run(b.dataset.prompt));
$('connect').onclick=async()=>{try{await api('/api/connect',{mode:$('mode').value,host:$('host').value,port:Number($('port').value),demo_bug:$('bug').checked});rows=[];$('steps').innerHTML='';await state();$('verdict').className='verdict neutral';$('verdictTitle').textContent='Ready to verify';$('verdictDetail').textContent='New connection. Run a safety plan.';$('verdictMeta').textContent='NO RUN YET';}catch(e){chat(e.message);$('status').textContent='Connection failed';}};
$('mode').onchange=()=>{$('bug').disabled=$('mode').value!=='sim';};
$('bug').onchange=()=>{$('build').textContent=$('bug').checked?'PLANTED':'FIXED';};
$('filter').onchange=renderLogs;
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'▶ Resume':'Ⅱ Pause';renderLogs();};
$('clear').onclick=()=>{logs=[];$('logs').innerHTML='';};
const events=new EventSource('/api/events');
events.onmessage=e=>{
  const event=JSON.parse(e.data); logs.push(event);if(logs.length>1000) logs.shift();renderLogs();
  if(event.level==='RX') renderPins(event.data);
  if(event.level==='USER'||event.level==='AGENT') chat(event.data.reply,event.level==='USER');
  if(event.level==='STEP'){rows.push(event.data);renderSteps();}
  if(event.level==='REPORT') report(event.data);
};
events.onerror=()=>{$('status').textContent='Reconnecting stream…';};
events.onopen=()=>{$('status').textContent='Connected';};
state().catch(e=>chat(e.message));
