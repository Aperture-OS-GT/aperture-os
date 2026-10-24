const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = {esp32_safety:'ESP32 safety',mpu6050_gyro:'Gyroscope',display:'Display'};
let pins = [], selected = null, waveformPin = null, busy = false, paused = false, logs = [], cursor = 0, historyView = null;
let previous = [];
try { previous = JSON.parse(localStorage.getItem('aperture.sessions') || '[]').slice(0,8); } catch (_) { previous = []; }
let current = freshSession();
function freshSession() { return {id:Date.now(),name:'New experiment',entries:[],report:null}; }
function persist() { try { localStorage.setItem('aperture.sessions',JSON.stringify(previous.slice(0,8))); } catch (_) { /* In-memory sessions still work if storage is unavailable. */ } }
function api(path, data) {
  return fetch(path, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(async response => { const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Harness unavailable'); return result; });
}
function sessionButtons() {
  $('sessions').innerHTML = [current,...previous].map((s,n) => '<button class="session '+((historyView===null?n===0:s.id===historyView)?'active':'')+'" data-session="'+s.id+'"><i class="verdict-dot '+(s.report?.status==='complete'?(s.report.pass?'pass':'fail'):'')+'"></i>'+esc(s.name)+'<small>'+(n===0?'Current session':new Date(s.id).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))+'</small></button>').join('');
}
function addEntry(entry) { current.entries.push(entry); if (!historyView) renderChat(); }
function message(text,user=false) { addEntry({type:'message',text,user}); }
function wireText(pin,request) {
  const physical = pins.find(p=>p.bcm===pin)?.physical;
  const component = request.component.toLowerCase().includes('gyro')?'Gyroscope':request.component;
  return 'Connect Pi GPIO'+pin+' (pin '+(physical || '?')+') → '+component+' '+request.dut_pin;
}
function requestEntry(data) {
  let entry = current.entries.findLast(e=>e.type==='request' && e.pi_pin===data.pi_pin);
  if (entry && entry.status==='pending') Object.assign(entry,data);
  else if (data.status==='pending') current.entries.push({type:'request',...data});
  if (!historyView) renderChat();
}
function decodedText(decoded) {
  if (!decoded) return '';
  return ['wx','wy','wz'].filter(k=>decoded[k]!==undefined).map(k=>k+': '+Number(decoded[k]).toFixed(2)).join(', ');
}
function entryHTML(entry) {
  if (entry.type==='message') return '<div class="message '+(entry.user?'user':'')+'"><span class="avatar">'+(entry.user?'↗':'◈')+'</span><div class="message-body"><small>'+(entry.user?'YOU':'APERTURE')+'<span>JUST NOW</span></small><p>'+esc(entry.text)+'</p></div></div>';
  if (entry.type==='request') {
    const pending=entry.status==='pending', rejected=entry.status==='rejected';
    return '<div class="request-card '+(pending?'':rejected?'rejected':'resolved')+'"><div class="request-title">'+(pending?'◉ ':rejected?'× ':'✓ ')+esc(wireText(entry.pi_pin,entry.request))+'</div><p>'+esc(entry.request.reason)+(pending?' · 3.3V logic, shared GND.':' · '+(rejected?'Rejected — request this wire again to proceed.':'Connection confirmed by operator.'))+'</p>'+(pending&&!historyView?'<div class="request-actions"><button data-confirm="'+entry.pi_pin+'">Connected ✓</button><button class="reject" data-reject="'+entry.pi_pin+'">Reject</button></div>':'')+'</div>';
  }
  const r=entry.row, failed=r.issues?.length || r.pass===false;
  return '<article class="step-card '+(failed?'failed':'')+'"><div class="step-top"><span class="badge '+(failed?'fail':'')+'">'+(failed?'✕ FAIL':'✓ PASS')+'</span><strong>'+esc(r.step || 'Observation')+'</strong><small>'+esc(r.latency_ms ?? 0)+' ms</small></div><p>'+esc(r.reasoning || '')+'</p>'+(r.decoded?'<p class="decoded">'+esc(decodedText(r.decoded))+'</p>':'')+(r.number!==undefined?'<p>Measured number: '+esc(r.number)+'</p>':'')+(r.telemetry?'<p>Serial safety claim: '+esc(r.telemetry.safety)+' · Physical GPIO23: '+esc(r.physical?.['23'])+'</p>':'')+(r.issues||[]).map(i=>'<p class="issue">'+esc(i)+'</p>').join('')+'</article>';
}
function renderChat() {
  const session=historyView?previous.find(s=>s.id===historyView):current;
  if (!session) return;
  const nearBottom=$('chat').scrollHeight-$('chat').scrollTop-$('chat').clientHeight<100;
  $('chat').innerHTML=session.entries.map(entryHTML).join('');
  $('sessionTitle').textContent=historyView?'Previous run · read only':session.name;
  $('promptForm').inert=!!historyView;
  $('export').onclick = historyView ? event => { event.preventDefault(); download(session.report); } : null;
  showReport(session.report);
  if(nearBottom) $('chat').scrollTop=$('chat').scrollHeight;
  setDisabled();
}
function download(report) {
  if(!report) return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download='aperture-report.json';a.click();URL.revokeObjectURL(url);
}
function showReport(report) {
  $('verdict').hidden=!report;
  if(!report) return;
  const waiting=report.status==='waiting_for_connections';
  const mismatch=(report.steps||[]).find(r=>r.issues?.some(i=>i.includes('DISCREPANCY')));
  $('verdict').className='verdict '+(waiting?'waiting':report.pass?'':'fail');
  const title=waiting?'Waiting for your connections':mismatch?'FIRMWARE LIED':report.pass?'Physical verification passed':'Verification failed';
  const detail=waiting?'Confirm all required wires to start the profile.':mismatch?mismatch.issues[0]:'Profile '+(report.profile || selected?.profile.id || '')+' · '+(report.steps||[]).length+' measured steps. '+(report.pass?'All expectations passed.':'Review the evidence above.');
  $('verdict').innerHTML='<strong>'+esc(title)+'</strong><p>'+esc(detail)+'</p>';
}
function renderPins() {
  $('pins').innerHTML=pins.map(p=>{
    const gpio=p.bcm!==null, pending=!!p.request;
    const state=!gpio?(p.kind==='GND'?'ground':p.kind.startsWith('ID_')?'reserved':'power'):pending?'pending':p.connected?(p.value?'high':'low'):'';
    const label=p.request?((p.request.component.toLowerCase().includes('gyro')?'Gyro':p.request.component.split(' ')[0])+' '+p.request.dut_pin):p.label?.replace('MPU6050 gyroscope','Gyro').replace('ESP32 safety controller','ESP32').replace('Binary numeric display DUT','Display');
    const title=pending?wireText(p.bcm,p.request):'Physical pin '+p.physical+' · '+(gpio?'GPIO'+p.bcm:p.kind)+(p.connected?' · '+p.label:'');
    return '<button class="pin '+(pending?'pending ':'')+(!gpio?(p.kind==='GND'?'ground ':'rail '):'')+(waveformPin===p.bcm&&gpio?'selected':'')+'" '+(gpio?'data-pin="'+p.bcm+'"':'disabled')+' title="'+esc(title)+'" aria-label="'+esc(title)+'"><span class="physical">'+p.physical+'</span><i class="pin-dot '+state+'"></i><span class="pin-info"><span class="pin-name">'+(gpio?'GPIO'+p.bcm:esc(p.kind))+'</span>'+(label?'<span class="dut-label">'+esc(label)+'</span>':'')+(p.connected&&p.bcm===24&&p.decoded?'<span class="decoded">24 '+esc(decodedText(p.decoded))+'</span>':'')+'</span>'+(p.connected&&p.direction?'<span class="direction '+p.direction+'">'+(p.direction==='tx'?'TX →':'RX ←')+'</span>':'')+'</button>';
  }).join('');
}
async function wave() {
  if(waveformPin===null) return;
  const pin=waveformPin, state=pins.find(p=>p.bcm===pin);
  const data=await api('/api/waveform?pin='+pin);
  if(pin!==waveformPin) return;
  $('waveform').hidden=false;
  $('waveform').className='waveform '+(state?.direction==='tx'?'transmit':'');
  $('waveTitle').textContent='GPIO'+pin+' / '+(state?.direction==='tx'?'Transmit':state?.direction==='rx'?'Receiving':'Unconfigured');
  $('waveDecoded').textContent=decodedText(state?.decoded);
  const values=data.samples.slice(-80), path=[];
  values.forEach((s,i)=>{ const x=values.length>1?i*400/(values.length-1):0,y=s.value?12:49; if(!i)path.push('M'+x+','+y);else path.push('H'+x+'V'+y); });
  if(values.length===1)path.push('H400');
  $('trace').innerHTML='<path d="M0 12H400 M0 49H400" fill="none" stroke="#253446" stroke-dasharray="3 5"/>'+(values.length?'<path d="'+path.join(' ')+'" stroke="currentColor" stroke-width="2" fill="none"/>':'<text x="0" y="32" fill="#7f91a7" font-size="11">No samples yet — connect and run a profile.</text>');
  $('waveState').textContent=values.length+' samples · '+(state?.connected?'LIVE':'NOT CONNECTED');
}
function setDisabled() {
  document.querySelectorAll('[data-confirm],[data-reject],.chips button,#send,#newSession,#connect,#profile,#flash').forEach(b=>b.disabled=busy||!!historyView);
  if(!busy&&!historyView&&selected?.profile.id!=='esp32_safety')$('flash').disabled=true;
  $('activity').textContent=historyView?'◌ Viewing saved evidence · select current session to resume':busy?'◌ Measuring hardware…':'● Ready for your next experiment';
}
async function action(fn) {
  if(busy) return;
  busy=true;setDisabled();
  try { await fn(); } catch(error) { message(error.message); }
  finally {busy=false;setDisabled();}
}
async function selectProfile(id, newSession=false, prompt=null) {
  if(newSession) {
    if(current.entries.length) { previous.unshift(JSON.parse(JSON.stringify(current)));previous=previous.slice(0,8);persist(); }
    current=freshSession();
  }
  historyView=null;waveformPin=null;$('waveform').hidden=true;
  current.name=names[id]+' verification';current.report=null;
  selected=null;
  if(prompt) message(prompt,true);
  message('Preparing '+names[id]+'. Connect the requested header pins below. I’ll run the tests when every required wire is confirmed.');
  sessionButtons();renderChat();
  selected=await api('/api/profiles/select',{profile:id});
  pins=(await api('/api/header')).pins;renderPins();
}
async function instruction(prompt) {
  if(/reset/i.test(prompt)) { await selectProfile($('profile').value,true,prompt);return; }
  if(/flash/i.test(prompt)) { await api('/api/prompt',{prompt});return; }
  const profile=/gyro/i.test(prompt)?'mpu6050_gyro':/esp32/i.test(prompt)?'esp32_safety':/display/i.test(prompt)?'display':null;
  if(profile) {
    $('profile').value=profile;
    if(selected?.profile.id!==profile) { await selectProfile(profile,true,prompt);return; }
  }
  await api('/api/prompt',{prompt});
}
function renderLogs() {
  if(paused) return;
  $('logs').innerHTML=logs.filter(e=>$('filter').value==='ALL'||e.level===$('filter').value).map(e=>'<div class="log '+esc(e.level)+'"><time>'+esc(e.timestamp.slice(11,23))+'</time><b>'+esc(e.level)+'</b>'+esc(JSON.stringify(e.data))+(e.latency_ms!==null?' · '+Number(e.latency_ms).toFixed(2)+' ms':'')+'</div>').join('');
  $('logs').scrollTop=$('logs').scrollHeight;
}
$('profile').onchange=()=>action(()=>selectProfile($('profile').value,true));
$('newSession').onclick=()=>action(()=>selectProfile($('profile').value,true));
$('reset').onclick=()=>action(()=>selectProfile($('profile').value,true));
$('promptForm').onsubmit=e=>{e.preventDefault();const prompt=$('prompt').value.trim();if(!prompt)return;$('prompt').value='';action(()=>instruction(prompt));};
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('promptForm').requestSubmit();}};
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>action(()=>instruction(b.dataset.prompt)));
$('chat').onclick=event=>{
  const confirm=event.target.closest('[data-confirm]'),reject=event.target.closest('[data-reject]');
  if(!confirm&&!reject) return;
  action(async()=>{
    await api('/api/requests/'+(confirm?'confirm':'reject'),{pi_pin:Number((confirm||reject).dataset[confirm?'confirm':'reject'])});
    const profiles=await api('/api/profiles');selected=profiles.selected;
    if(confirm&&selected?.ready) { message('All required wires are confirmed. Running '+names[selected.profile.id]+' against physical evidence.');await api('/api/profiles/run',{}); }
    else if(reject) message('Wire rejected. Tests remain blocked. Select the profile again to request a new wiring plan.');
  });
};
$('pins').onclick=event=>{const button=event.target.closest('[data-pin]');if(button){waveformPin=Number(button.dataset.pin);renderPins();wave().catch(e=>message(e.message));}};
$('closeWave').onclick=()=>{waveformPin=null;$('waveform').hidden=true;renderPins();};
$('sessions').onclick=event=>{const button=event.target.closest('[data-session]');if(!button)return;historyView=Number(button.dataset.session)===current.id?null:Number(button.dataset.session);sessionButtons();renderChat();};
async function flashFirmware() {
  const env=$('flashEnv').value, manual=$('flashManual').checked;
  message('Flash the '+(env==='fixed'?'fixed':'DEMO_BUG')+' firmware over the Pi header UART.',true);
  const result=await api('/api/flash',{env,build:$('flashBuild').checked,manual_boot:manual});
  if(result.status==='waiting_for_connections'){message('Flashing needs confirmed wires on Pi GPIO '+result.missing.join(', ')+' first: TX0, RX0, EN'+(manual?'':', BOOT')+'. Confirm them in the cards above.');return;}
  message('Flashed '+env+' in '+result.seconds+' s ('+(result.build_tag||'no build tag')+'). Run the safety plan again to re-verify.');
}
$('flash').onclick=()=>action(flashFirmware);
$('connect').onclick=()=>action(async()=>{await api('/api/connect',{mode:$('mode').value,host:$('host').value,port:Number($('port').value),demo_bug:$('bug').checked});$('connectionNote').textContent='Harness connected. Wiring confirmations reset.';await refreshState();});
$('mode').onchange=()=>{$('bug').disabled=$('mode').value!=='sim';};
$('filter').onchange=renderLogs;
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'▶ Resume':'Ⅱ Pause';renderLogs();};
$('clear').onclick=()=>{logs=[];$('logs').innerHTML='';$('eventCount').textContent='0 events ⌃';};
async function refreshState() {
  const state=await api('/api/state');
  const connection=state.connection;
  $('linkStatus').innerHTML='⇄ '+esc({sim:'Sim',tcp:'TCP',rfcomm:'Bluetooth'}[connection.mode])+' <span class="online-dot"></span> '+(connection.connected?'Connected':'Disconnected')+' ⌄';
  $('mode').value=connection.mode;$('host').value=connection.host;$('port').value=connection.port;$('bug').checked=connection.demo_bug;
  $('planner').textContent=state.llm?'AI ENGINE':'OFFLINE ENGINE';
  selected=state.profile;
  if(selected){$('profile').value=selected.profile.id;current.name=names[selected.profile.id]+' verification';}
  pins=(await api('/api/header')).pins;renderPins();
  const requests=(await api('/api/requests')).requests;
  requests.forEach(r=>requestEntry({pi_pin:r.pi_pin,status:'pending',request:{dut_pin:r.dut_pin,component:r.component,reason:r.reason}}));
  if(state.report){current.report=state.report;if(!current.entries.some(e=>e.type==='step'))(state.report.steps||[]).forEach(row=>addEntry({type:'step',row}));}
  sessionButtons();renderChat();
  return state.event_cursor;
}
async function start() {
  message('Your hardware. Ground truth. Choose a device profile, confirm its wiring, and I’ll verify the signals — not just what the firmware says.');
  cursor=await refreshState();
  const events=new EventSource('/api/events');
  events.onmessage=e=>{
    const event=JSON.parse(e.data);if(event.id<=cursor)return;cursor=event.id;
    logs.push(event);if(logs.length>1000)logs.shift();$('eventCount').textContent=logs.length+' events ⌃';renderLogs();
    if(event.level==='PIN_STATE'){pins=event.data.pins;renderPins();if(waveformPin!==null)wave().catch(()=>{});}
    if(event.level==='REQUEST')requestEntry(event.data);
    if(event.level==='PROFILE'){selected=event.data;$('profile').value=selected.profile.id;current.name=names[selected.profile.id]+' verification';sessionButtons();}
    if(event.level==='USER'||event.level==='AGENT')message(event.data.reply,event.level==='USER');
    if(event.level==='STEP')addEntry({type:'step',row:event.data});
    if(event.level==='REPORT'){current.report=event.data;renderChat();sessionButtons();}
    if(event.level==='FLASH'){const d=event.data;$('flashNote').textContent=d.stage==='upload'?'Uploading '+(d.name||'')+' '+d.received+'/'+d.size+' bytes':d.stage==='build'?'Building '+d.env+' with PlatformIO…':d.stage==='start'?'Resetting ESP32 into bootloader, writing '+d.images.length+' images…':d.stage==='done'?'Flashed '+d.env+' in '+d.seconds+' s':d.stage==='waiting'?'Waiting for wires: GPIO '+d.missing.join(', '):'';}
  };
  events.onerror=()=>{$('linkStatus').textContent='⇄ Event stream reconnecting…';};
  events.onopen=()=>{refreshState().catch(()=>{});};
}
setInterval(async()=>{
  if(waveformPin===null||busy||historyView)return;
  const state=pins.find(p=>p.bcm===waveformPin);
  if(!state?.connected)return;
  try {
    if(state.direction==='rx')await api('/api/gpio',{cmd:'sample',pins:[waveformPin],duration_ms:200,rate_hz:50});
    await wave();
  } catch(error) { $('waveState').textContent=error.message; }
},2000);
start().catch(error=>message(error.message));
