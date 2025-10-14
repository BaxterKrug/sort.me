/* sort.me UI controller (Excel-style grid + K3 as error cell)
   - Uses backend endpoints where available (set BASE).
   - Demo Mode simulates endpoints for local testing with no backend.
*/
const BASE = ""; // e.g., "/api"
let demo = false;

// ------------- tiny DOM helpers -------------
const $ = (id) => document.getElementById(id);
const on = (el, ev, fn) => el.addEventListener(ev, fn);
const show = (el) => el.classList.remove('hidden');
const hide = (el) => el.classList.add('hidden');
const toast = (msg) => {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  $('toasts').appendChild(t);
  setTimeout(()=> t.remove(), 3200);
};

// ------------- API wrapper -------------
function api(path, opts){
  if(demo) return demoApi(path, opts);
  return fetch(`${BASE}${path}`, opts).then(async r=>{
    if(!r.ok){
      const text = await r.text().catch(()=> "");
      throw new Error(`${r.status} ${r.statusText} ${text}`.trim());
    }
    const ct = r.headers.get('content-type') || "";
    if(ct.includes('application/json')) return r.json();
    return r.text();
  });
}

// ------------- Panels / Nav -------------
const panelCalibrate = $('panelCalibrate');
const panelSetup = $('panelSetup');
const panelRun = $('panelRun');

on($('btnToSetup'), 'click', ()=>{ hide(panelCalibrate); show(panelSetup); });
on($('btnBackToCal'), 'click', ()=>{ show(panelCalibrate); hide(panelSetup); });
on($('demoToggle'), 'change', (e)=>{ demo = e.target.checked; toast(`Demo Mode ${demo?'ON':'OFF'}`); resetSimState(); });

// Demo batch tester refs
const demoBatchFiles = $('demoBatchFiles');
const demoDbPath = $('demoDbPath');
const demoFilenameExpect = $('demoFilenameExpect');
const demoBatchSummary = $('demoBatchSummary');
const demoBatchWrap = $('demoBatchTableWrap');
const demoBatchTableBody = $('demoBatchTableBody');

// ------------- E-STOP / Pause / Resume -------------
on($('btnEStop'), 'click', async ()=>{
  if(!confirm('EMERGENCY STOP — confirm?')) return;
  try{ await api('/motion/estop', {method:'POST'}); toast('E-STOP sent'); }
  catch(e){ toast(`E-STOP error: ${e.message}`); }
});
on($('btnPause'), 'click', async ()=>{
  try{
    await api('/run/pause', {method:'POST'});
    runLoop.stop();
    hide($('btnPause'));
    show($('btnResume'));
    toast('Paused');
  }catch(e){ toast(`Pause failed: ${e.message}`); }
});
on($('btnResume'), 'click', async ()=>{
  try{
    await api('/run/resume', {method:'POST'});
    runLoop.start();
    hide($('btnResume'));
    show($('btnPause'));
    toast('Resumed');
  }catch(e){ toast(`Resume failed: ${e.message}`); }
});

// ------------- Calibration -------------
on($('btnHomeAll'), 'click', ()=> api('/motion/home_all',{method:'POST'}).then(()=>toast('Homed all')).catch(e=>toast(e.message)));
on($('btnPlungerDown'), 'click', ()=> api('/plunger/down',{method:'POST'}).then(()=>toast('Plunger down')).catch(e=>toast(e.message)));
on($('btnPlungerUp'), 'click', ()=> api('/plunger/up',{method:'POST'}).then(()=>toast('Plunger up')).catch(e=>toast(e.message)));
on($('btnVacuumOn'), 'click', ()=> api('/vacuum/on',{method:'POST'}).then(()=>toast('Vacuum on')).catch(e=>toast(e.message)));
on($('btnVacuumOff'), 'click', ()=> api('/vacuum/off',{method:'POST'}).then(()=>toast('Vacuum off')).catch(e=>toast(e.message)));

on($('btnSnap'), 'click', async ()=>{
  try{
    const res = await api('/camera/ocr_snapshot',{method:'POST'});
    $('ocrPreview').textContent = (res && res.text) ? res.text : '(no text)';
  }catch(e){ toast(`OCR failed: ${e.message}`); }
});

function refreshCamera(){
  $('cameraFeed').src = demo
    ? 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMzIwIiBoZWlnaHQ9IjE3MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCBmaWxsPSIjMDAwIiB3aWR0aD0iMTAwJSIgaGVpZ2h0PSIxMDAlIi8+PHRleHQgeD0iNTAlIiB5PSI1MCUiIGZpbGw9IiNmZmYiIHRleHQtYW5jaG9yPSJtaWRkbGUiPkRFTU8gQ0FNRVJBPC90ZXh0Pjwvc3ZnPg=='
    : `${BASE}/camera/preview?t=${Date.now()}`;
  $('cameraLive').src = demo
    ? $('cameraFeed').src
    : `${BASE}/camera/stream?t=${Date.now()}`;
}
setInterval(refreshCamera, 2000); refreshCamera();

// ------------- Grid / Cells -------------
let cells = []; // [{id,x,y,z}, ...]

async function loadGrid(){
  try{
    const res = await api('/grid/cells');
    cells = res?.cells ?? [];
  }catch(e){
    // Fallback Excel-style: columns A..K, rows 1..3; (A-row = feeders), K3 is error cell
    cells = [];
    // full spreadsheet from A..K
    const cols = ['A','B','C','D','E','F','G','H','I','J','K'];
    for(let r=1;r<=3;r++){
      for(const col of cols){
        const id = `${col}${r}`;
        cells.push({id, x: (col.charCodeAt(0)-65)*20, y: r*20, z:0});
      }
    }
  }
  renderGridPreview('gridPreview', cells);
  populatePositions();
}
function renderGridPreview(hostId, list){
  const host = $(hostId);
  host.innerHTML = '';

  // compute unique column letters (A..K etc.) and set grid columns dynamically
  const cols = Array.from(new Set(list.map(c => c.id.replace(/[0-9]/g, ''))));
  cols.sort((a,b)=> a.localeCompare(b));
  host.style.display = 'grid';
  host.style.gridTemplateColumns = `repeat(${Math.max(1, cols.length)}, minmax(40px, 1fr))`;
  host.style.gap = '6px';

  list.forEach(c=>{
    const el = document.createElement('div');
    el.className = 'cell';
    el.textContent = c.id;
    el.title = `(${c.x},${c.y},${c.z})`;
    if(c.id === 'K3'){ el.classList.add('err'); el.title += ' • Error cell'; }

    // Highlight feeder cells (column A) in green
    if (c.id && c.id[0] === 'A') {
      el.classList.add('feeder');
      el.style.backgroundColor = '#e6f9e6';
      el.style.borderColor = '#4CAF50';
    }

    host.appendChild(el);
  });
}
function populatePositions(){
  const sel = $('positionSelect'); sel.innerHTML = '<option disabled selected>Select a cell</option>';
  cells.forEach(c=>{
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.id;
    sel.appendChild(o);
  });
}
on($('btnReloadGrid'), 'click', ()=> loadGrid().then(()=>toast('Grid loaded')));

on($('btnTestCellMoves'), 'click', ()=>{
  const dlg = $('dlgTestMoves');
  const host = $('testGrid');
  host.innerHTML = '';

  // set grid columns in the test dialog to match current cells
  const cols = Array.from(new Set(cells.map(c => c.id.replace(/[0-9]/g, ''))));
  cols.sort((a,b)=> a.localeCompare(b));
  host.style.display = 'grid';
  host.style.gridTemplateColumns = `repeat(${Math.max(1, cols.length)}, minmax(40px, 1fr))`;
  host.style.gap = '6px';

  cells.forEach(c=>{
    const el = document.createElement('div');
    el.className = 'cell'; el.textContent = c.id; el.title = `(${c.x},${c.y},${c.z})`;
    if(c.id === 'K3'){ el.classList.add('err'); el.title += ' • Error cell'; }

    // Highlight feeder cells (column A) in green
    if (c.id && c.id[0] === 'A') {
      el.classList.add('feeder');
      el.style.backgroundColor = '#e6f9e6';
      el.style.borderColor = '#4CAF50';
    }

    el.addEventListener('click', async ()=>{
      try{
        await api('/motion/move_to', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cell_id:c.id})});
        toast(`Moved to ${c.id}`);
      }catch(e){ toast(`Move failed: ${e.message}`); }
    });
    host.appendChild(el);
  });
  dlg.showModal();
});
on($('btnCloseTestMoves'),'click',()=> $('dlgTestMoves').close());

// ------------- Run Controls -------------
on($('btnStartRun'), 'click', async ()=>{
  const game = $('gameSelect').value;
  const sort = $('sortSelect').value;
  if(!game || !sort){ toast('Select game and sorting'); return; }
  try{
    const payload = {
      game, sorting: sort,
      feeder_estimate: Number($('feederCapacity').value||0),
      divert_uncertain: $('divertUncertain').checked
    };
    await api('/run/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    hide(panelSetup); show(panelRun);
    runLoop.start();
  }catch(e){ toast(`Start failed: ${e.message}`); }
});

on($('btnEndRun'), 'click', async ()=>{
  if(!confirm('End the current run?')) return;
  try{
    await api('/run/end', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({notes: $('runNotes').value})});
    runLoop.stop(); toast('Run ended'); hide(panelRun); show(panelCalibrate);
  }catch(e){ toast(`End failed: ${e.message}`); }
});

on($('btnManualDivert'),'click', ()=> api('/run/divert_current',{method:'POST'}).then(()=>toast('Current card diverted to K3')).catch(e=>toast(e.message)));

on($('btnMoveToCell'), 'click', async ()=>{
  const id = $('positionSelect').value;
  if(!id){ toast('Select a cell'); return; }
  try{
    await api('/motion/move_to',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cell_id:id})});
    toast(`Moved to ${id}`);
  }catch(e){ toast(`Move failed: ${e.message}`); }
});
on($('btnHomeAll2'),'click', ()=> api('/motion/home_all',{method:'POST'}).then(()=>toast('Homed all')).catch(e=>toast(e.message)));
on($('btnHomeXY'),'click',  ()=> api('/motion/home_xy',{method:'POST'}).then(()=>toast('Homed XY')).catch(e=>toast(e.message)));
on($('btnHomeZ'),'click',   ()=> api('/motion/home_z',{method:'POST'}).then(()=>toast('Homed Z')).catch(e=>toast(e.message)));
on($('btnVacOnRun'),'click', ()=> api('/vacuum/on',{method:'POST'}).then(()=>toast('Vacuum on')).catch(e=>toast(e.message)));
on($('btnVacOffRun'),'click',()=> api('/vacuum/off',{method:'POST'}).then(()=>toast('Vacuum off')).catch(e=>toast(e.message)));
on($('btnPlunge'),'click',   ()=> api('/plunger/down',{method:'POST'}).then(()=>toast('Plunge')).catch(e=>toast(e.message)));
on($('btnRetract'),'click',  ()=> api('/plunger/up',{method:'POST'}).then(()=>toast('Retract')).catch(e=>toast(e.message)));

// ------------- Error cell helpers (UI still available for exports) -------------
on($('btnExportCSV'),'click', async ()=>{
  try{
    const res = await api('/errors/export'); // optional backend support
    if(typeof res === 'string' && res.startsWith('data:')){ const a = document.createElement('a'); a.href = res; a.download = 'k3_error_export.csv'; a.click(); }
    else if(res?.url){ location.href = res.url; }
    else { toast('No export available'); }
  }catch(e){ toast(`Export failed: ${e.message}`); }
});
on($('btnClearErrors'),'click', ()=> api('/errors/clear',{method:'POST'}).then(()=>{
  $('errorList').innerHTML = ''; toast('Cleared (K3 log)');
}).catch(e=>toast(e.message)));

// ------------- Logs -------------
on($('btnOpenLogs'),'click', async ()=>{
  try{
    const res = await api('/logs/tail');
    $('logOutput').textContent = res?.text ?? '';
    $('dlgLogs').showModal();
  }catch(e){ toast(`Log fetch failed: ${e.message}`); }
});
on($('btnCloseLogs'),'click', ()=> $('dlgLogs').close());

// ------------- Run status loop -------------
const runLoop = (()=>{
  let timer = null;
  async function tick(){
    try{
      const s = await api('/run/status');
      $('runState').textContent = s.state ?? 'Unknown';
      $('batchProgress').textContent = `${s.completed||0} / ${s.total||0}`;
      $('countsOK').textContent = s.good||0;
      $('countsErr').textContent = s.err||0;
      $('throughput').textContent = `${s.throughput_cpm||0} cpm`;
      $('progressBar').style.width = `${s.progress_pct||0}%`;
      $('currentCard').textContent = s.current_card || '—';
      renderErrors(s.errors||[]);
    }catch(e){ /* ignore polling error to avoid toast spam */ }
  }
  function renderErrors(list){
    const host = $('errorList'); host.innerHTML='';
    list.forEach(err=>{
      const card = document.createElement('div');
      card.className='error-card';
      const t = document.createElement('div'); t.className='thumb';
      const img = document.createElement('img'); img.src = err.thumb || '';
      t.appendChild(img);
      const m = document.createElement('div'); m.className='meta';
      m.textContent = `${err.reason || 'Uncertain'} • ${err.id || ''}`;
      card.appendChild(t); card.appendChild(m);
      host.appendChild(card);
    });
  }
  return {
    start(){ if(timer) return; tick(); timer = setInterval(tick, 1000); },
    stop(){ if(!timer) return; clearInterval(timer); timer=null; }
  };
})();

// ------------- Simulator & Preview shared state -------------
let simQueue = [];
let simIndex = 0;
let simTimer = null;
let alphaMap = null;

// Shared map fetch (used by preview + simulator)
async function fetchAlphaMap(){
  try{
    const res = await api('/debug/alpha_map');
    alphaMap = res.letter_to_cell || null;
  }catch(e){ alphaMap = null; }
}

// ------------- Assignment Preview -------------
function firstLetter(name){
  return /^[A-Z]/i.test(name?.trim()||"") ? name.trim()[0].toUpperCase() : 'A';
}

async function previewAssign(name, confidence){
  const sort = $('sortSelect').value || 'alpha_exact';
  // Prefer a non-mutating backend preview
  try{
    const res = await api('/debug/assign_preview', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, confidence, sorting: sort})
    });
    return res; // {cell, reason, first}
  }catch(e){
    // Local fallback for alpha_exact (no capacity knowledge)
    if(sort === 'alpha_exact'){
      if(!alphaMap) await fetchAlphaMap();
      const fl = firstLetter(name);
      const cell = (confidence < 0.80) ? 'K3' : (alphaMap?.[fl] || 'K3');
      const reason = (confidence < 0.80) ? 'divert:low_confidence' : `alpha_exact:${fl}`;
      return {cell, reason, first: fl};
    }
    throw e;
  }
}

async function doPreview(){
  const name = $('previewName').value.trim();
  const conf = parseFloat($('previewConf').value || '1') || 1;
  if(!name){
    $('prevFirst').textContent='—'; $('prevCell').textContent='—'; $('prevReason').textContent='—';
    return;
  }
  try{
    const out = await previewAssign(name, conf);
    $('prevFirst').textContent = out.first || firstLetter(name);
    $('prevCell').textContent  = out.cell  || '—';
    $('prevReason').textContent= out.reason|| '—';
  }catch(err){
    toast(`Preview failed: ${err.message}`);
  }
}

// Wire preview UI
on($('btnPreview'), 'click', doPreview);
on($('btnUseOCR'), 'click', ()=>{
  const text = $('ocrPreview')?.textContent || '';
  if(text && text !== 'No text yet'){
    $('previewName').value = (text.split('—')[0].trim() || text.trim());
    doPreview();
  }
});
let previewDeb;
on($('previewName'), 'input', ()=>{
  clearTimeout(previewDeb); previewDeb = setTimeout(doPreview, 250);
});
on($('previewConf'), 'input', ()=> { clearTimeout(previewDeb); previewDeb = setTimeout(doPreview, 250); });
on($('sortSelect'), 'change', doPreview);

// ------------- Simulator -------------
function parseSimInput(){
  const lines = $('simInput').value.split('\n').map(s=>s.trim()).filter(Boolean);
  return lines.map(line=>{
    const parts = line.split(',');
    const name = parts[0].trim();
    const confidence = parts[1] ? parseFloat(parts[1]) : 1.0;
    return {name, confidence: isNaN(confidence) ? 1.0 : confidence};
  });
}

function simRow(idx, name, first, cell, reason){
  const host = $('simTable');
  const row = document.createElement('div'); row.className='sim-row';
  const s1 = document.createElement('span'); s1.textContent = idx+1;
  const s2 = document.createElement('span'); s2.textContent = name;
  const s3 = document.createElement('span'); s3.textContent = first;
  const s4 = document.createElement('span'); s4.textContent = cell;
  const s5 = document.createElement('span'); s5.textContent = reason;
  const ok = reason.startsWith('alpha_exact');
  [s1,s2,s3,s4,s5].forEach(el=> el.className = ok ? 'sim-ok' : 'sim-divert');
  row.append(s1,s2,s3,s4,s5);
  host.appendChild(row);
  host.scrollTop = host.scrollHeight;
}

function updateSimProgress(){
  $('simProgress').textContent = `${Math.min(simIndex, simQueue.length)} / ${simQueue.length}`;
}

async function simStep(){
  if(simIndex >= simQueue.length){ stopSim(); return; }
  const item = simQueue[simIndex];
  const first = firstLetter(item.name);
  try{
    const res = await api('/debug/assign', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name:item.name, confidence:item.confidence})
    });
    simRow(simIndex, item.name, first, res.cell, res.reason);
  }catch(e){
    simRow(simIndex, item.name, first, 'ERR', `error:${e.message}`);
  }
  simIndex++; updateSimProgress();
}

function runSimLoop(){
  if(simTimer) return;
  simTimer = setInterval(async ()=>{
    if(simIndex >= simQueue.length){ stopSim(); return; }
    await simStep();
  }, 500);
}
function stopSim(){ if(simTimer){ clearInterval(simTimer); simTimer=null; } hide($('btnSimStop')); show($('btnSimRun')); }
function resetSimUI(){
  $('simTable').innerHTML = `<div class="sim-row sim-header">
    <span>#</span><span>Name</span><span>First</span><span>Cell</span><span>Reason</span>
  </div>`;
  simIndex = 0; updateSimProgress();
}
function resetSimState(){ simQueue = []; simIndex = 0; if(simTimer){ clearInterval(simTimer); simTimer=null; } resetSimUI(); }

// Buttons
on($('btnSimLoadSample'),'click', ()=>{
  $('simInput').value = `Ancestral Recall
Birds of Paradise,0.72
Counterspell
Zurzoth, Chaos Rider
Island
Serra Angel
Mox Emerald
Wheel of Fortune
★Foil Surprise,0.95`;
});
on($('btnSimReset'),'click', async ()=>{
  resetSimState();
  try{ await api('/debug/reset_counts', {method:'POST'}); toast('Counts reset'); }
  catch(e){ toast('Reset (demo): ok'); }
});
on($('btnSimStep'),'click', async ()=>{
  if(!alphaMap) await fetchAlphaMap();
  if(simQueue.length===0){ simQueue = parseSimInput(); resetSimUI(); updateSimProgress(); }
  await simStep();
});
on($('btnSimRun'),'click', async ()=>{
  if(!alphaMap) await fetchAlphaMap();
  if(simQueue.length===0){ simQueue = parseSimInput(); resetSimUI(); updateSimProgress(); }
  show($('btnSimStop')); hide($('btnSimRun'));
  runSimLoop();
});
on($('btnSimStop'),'click', stopSim);

// ------------- Demo OCR Batch Tester -------------
async function runDemoBatchTest(){
  if(!demoBatchFiles){ toast('Batch tester unavailable'); return; }
  const files = demoBatchFiles.files || [];
  if(files.length === 0){ toast('Select one or more images first'); return; }

  const btn = $('btnDemoBatchRun');
  const originalText = btn ? btn.textContent : 'Run Batch Test';
  if(btn){
    btn.disabled = true;
    btn.textContent = 'Running…';
  }

  const form = new FormData();
  Array.from(files).forEach((file)=> form.append('files', file, file.name));
  const dbPath = (demoDbPath?.value || '').trim();
  if(dbPath) form.append('db_path', dbPath);
  form.append('use_filename_expected', demoFilenameExpect?.checked ? 'true' : 'false');

  try{
    const res = await fetch(`${BASE}/demo/batch_identify`, {method:'POST', body: form});
    if(!res.ok){
      const text = await res.text().catch(()=> '');
      throw new Error(text || res.statusText);
    }
    const data = await res.json();
    renderDemoBatchResults(data);
    toast('Batch test complete');
  }catch(err){
    toast(`Batch test failed: ${err.message}`);
  }finally{
    if(btn){
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }
}

function renderDemoBatchResults(payload){
  if(!demoBatchTableBody || !demoBatchSummary || !demoBatchWrap){
    console.warn('Batch tester elements missing');
    return;
  }
  demoBatchTableBody.innerHTML = '';
  const rows = payload?.results || [];
  if(rows.length === 0){
    demoBatchSummary.textContent = 'No results returned.';
    hide(demoBatchWrap);
    return;
  }

  const summary = payload?.summary || {};
  const total = summary.total || rows.length;
  const matchName = summary.name_matches ?? '-';
  const matchCell = summary.cell_matches ?? '-';
  const matchBoth = summary.both_matches ?? '-';
  const dbInfo = summary.db_path ? ` • DB: ${summary.db_path}` : '';
  demoBatchSummary.textContent = `Processed ${total} image${total===1?'':'s'}. Name matches: ${matchName}/${total}, Cell matches: ${matchCell}/${total}, Both: ${matchBoth}/${total}${dbInfo}`;

  const createCell = (text)=>{
    const td = document.createElement('td');
    td.textContent = text ?? '—';
    return td;
  };

  rows.forEach((row, idx)=>{
    const tr = document.createElement('tr');
    if(row.error){
      tr.classList.add('error-row');
    }else if(row.match_name && row.match_cell){
      tr.classList.add('match-row');
    }else if(row.match_name || row.match_cell){
      tr.classList.add('partial-row');
    }else{
      tr.classList.add('mismatch-row');
    }

    const expectedName = row?.expected?.name || '—';
    const expectedCell = row?.expected?.cell || '—';
    const ocrName = row?.region_texts?.name || '—';
    const identified = row?.identified_name || '—';
    const cell = row?.assignment?.cell || '—';
    const reason = row?.error || row?.assignment?.reason || '';
    const idScore = typeof row?.id_score === 'number' ? row.id_score.toFixed(1) : '—';
    let matchLabel = '—';
    if(row.error){
      matchLabel = 'Error';
    }else if(row.match_name && row.match_cell){
      matchLabel = '✓ Name & Cell';
    }else if(row.match_name){
      matchLabel = 'Name only';
    }else if(row.match_cell){
      matchLabel = 'Cell only';
    }else{
      matchLabel = 'No match';
    }

    tr.appendChild(createCell(idx+1));
    tr.appendChild(createCell(row.filename || '—'));
    tr.appendChild(createCell(expectedName));
    tr.appendChild(createCell(ocrName));
    tr.appendChild(createCell(identified));
    tr.appendChild(createCell(cell));
    tr.appendChild(createCell(expectedCell));
    tr.appendChild(createCell(matchLabel));
    tr.appendChild(createCell(idScore));
    tr.appendChild(createCell(reason));

    if(row?.ocr){
      const rot = row.ocr.rotation ?? 0;
      const rotConf = row.ocr.rotation_confidence ?? 0;
      tr.title = `Rotation: ${rot}° (conf ${rotConf.toFixed ? rotConf.toFixed(2) : rotConf})`;
    }

    demoBatchTableBody.appendChild(tr);
  });

  show(demoBatchWrap);
}

function clearDemoBatch(){
  if(demoBatchFiles) demoBatchFiles.value = '';
  if(demoDbPath) demoDbPath.value = '';
  if(demoFilenameExpect) demoFilenameExpect.checked = true;
  if(demoBatchSummary) demoBatchSummary.textContent = '';
  if(demoBatchTableBody) demoBatchTableBody.innerHTML = '';
  if(demoBatchWrap) hide(demoBatchWrap);
}

on($('btnDemoBatchRun'), 'click', runDemoBatchTest);
on($('btnDemoBatchClear'), 'click', clearDemoBatch);

// ------------- Demo API (no backend required) -------------
async function demoApi(path, opts){
  await new Promise(r=>setTimeout(r, 160)); // simulate latency

  // simple in-memory counts for demo capacity
  window.__demo_counts ||= {};
  // build full A1..K3 grid and a letter map that maps A..Z to the first 26 cells
  const colsAll = ['A','B','C','D','E','F','G','H','I','J','K'];
  const gridCells = [];
  for(let r=1;r<=3;r++){ for(const col of colsAll){ gridCells.push(`${col}${r}`); } }
  const ERROR_CELL = 'K3'; // unified error pile
  // assign letters A..Z to the first 26 cells, skipping the error cell if encountered
  const assignable = gridCells.filter(c => c !== ERROR_CELL);
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
  const letterMap = {};
  alphabet.forEach((L, i) => { letterMap[L] = assignable[i] || ERROR_CELL; });

  switch(path){
    // Grid
    case '/grid/cells': {
      const cells = [];
      const cols = ['A','B','C','D','E','F','G','H','I','J','K']; // Excel-style columns (A..K)
      for(let r=1;r<=3;r++){ for(const col of cols){ cells.push({id:`${col}${r}`, x:0,y:0,z:0}); } }
      return {cells};
    }

    // Motion / actuators (no-ops)
    case '/motion/estop':
    case '/motion/home_all':
    case '/motion/home_xy':
    case '/motion/home_z':
    case '/motion/move_to':
    case '/plunger/down':
    case '/plunger/up':
    case '/vacuum/on':
    case '/vacuum/off':
      return {ok:true};

    // Camera / OCR
    case '/camera/preview':
    case '/camera/stream':
      return {}; // images handled by refreshCamera with data uri
    case '/camera/ocr_snapshot':
      return {text:'Lightning Bolt — M11'};

    // Run control & status
    case '/run/start': return {ok:true};
    case '/run/pause': return {ok:true};
    case '/run/resume': return {ok:true};
    case '/run/end': return {ok:true};
    case '/run/status': {
      const total = 100, completed = Math.min(Math.floor((Date.now()/1000)%total), total);
      const err = Math.floor(completed*0.05);
      return {
        state:'Running',
        total, completed, good: completed-err, err,
        throughput_cpm: 18,
        progress_pct: Math.floor((completed/total)*100),
        current_card: completed%2===0 ? 'Island — UNH' : 'Blue-Eyes White Dragon',
        errors: Array.from({length:Math.min(err,6)}).map((_,j)=>({id:`K3-${j+1}`, reason: (j%2?'Unreadable OCR':'Overflow'), thumb:''}))
      };
    }

    // Errors (optional)
    case '/errors/export': {
      const csv = "data:text/csv;base64," + btoa("cell,reason\nK3,example\n");
      return csv;
    }
    case '/errors/clear': return {ok:true};

    // Logs
    case '/logs/tail': return {text:`[info] system ok\n[info] vacuum -19.2 kPa\n[info] limit switch: false\n`};

    // Maps / Debug
    case '/debug/alpha_map': return {letter_to_cell: letterMap};
    case '/debug/reset_counts': { window.__demo_counts = {}; return {ok:true}; }

    // Non-mutating preview (does NOT change counts)
    case '/debug/assign_preview': {
      const body = JSON.parse(opts?.body || '{}');
      const name = (body.name || '').trim();
      const conf = Number(body.confidence ?? 1.0);
      const fl = /^[A-Z]/i.test(name) ? name[0].toUpperCase() : 'A';
      const reason = conf < 0.80 ? 'divert:low_confidence' : `alpha_exact:${fl}`;
      return {cell: conf < 0.80 ? ERROR_CELL : letterMap[fl], reason, first: fl};
    }

    // Mutating assign used by simulator (updates demo counts & capacity)
    case '/debug/assign': {
      const body = JSON.parse(opts?.body || '{}');
      const name = (body.name || '').trim();
      const conf = Number(body.confidence ?? 1.0);
      const fl = /^[A-Z]/i.test(name) ? name[0].toUpperCase() : 'A';
      if(conf < 0.80){
        window.__demo_counts[ERROR_CELL] = (window.__demo_counts[ERROR_CELL]||0)+1;
        return {cell:ERROR_CELL, reason:'divert:low_confidence', counts:window.__demo_counts};
      }
      // demo capacity: 2 each (error cell unlimited)
      const tgt = letterMap[fl];
      window.__demo_counts[tgt] = (window.__demo_counts[tgt]||0)+1;
      if(window.__demo_counts[tgt] > 2){
        window.__demo_counts[ERROR_CELL] = (window.__demo_counts[ERROR_CELL]||0)+1;
        return {cell:ERROR_CELL, reason:`overflow:${fl}`, counts:window.__demo_counts};
      }
      return {cell:tgt, reason:`alpha_exact:${fl}`, counts:window.__demo_counts};
    }

    default: return {ok:true};
  }
}

// ------------- boot -------------
loadGrid();
