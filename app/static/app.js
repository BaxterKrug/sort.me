/* sort.me UI controller (Excel-style grid + K3 as error cell)
   - Uses backend endpoints where available (set BASE).
   - Demo Mode simulates endpoints for local testing with no backend.
*/
const BASE = ""; // e.g., "/api"
let demo = false;

// ------------- tiny DOM helpers -------------
const $ = (id) => document.getElementById(id);
const on = (el, ev, fn) => { if (el) el.addEventListener(ev, fn); };
const show = (el) => { if (el) el.classList.remove('hidden'); };
const hide = (el) => { if (el) el.classList.add('hidden'); };
const toast = (msg) => {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  $('toasts').appendChild(t);
  setTimeout(()=> t.remove(), 3200);
};

function setCameraStatus(state){
  const chip = $('cameraStatus');
  if(!chip) return;
  if(!state || state.error){
    chip.textContent = 'Camera: error';
    chip.classList.add('muted');
    return;
  }
  const online = !!state.online;
  const path = state.path || state.device || 'unknown';
  chip.textContent = `Camera: ${online ? 'online' : 'offline'} ${path}`.trim();
  chip.classList.toggle('muted', !online);
}

async function pollCameraStatus(){
  try{
    const info = await api('/camera/status');
    setCameraStatus(info);
  }catch(err){
    setCameraStatus({error: err.message});
  }
}

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

on($('btnToSetup'), 'click', ()=>{
  panelSetup.scrollIntoView({behavior:'smooth', block:'start'});
});
on($('btnBackToCal'), 'click', ()=>{
  panelCalibrate.scrollIntoView({behavior:'smooth', block:'start'});
});
on($('demoToggle'), 'change', async (e)=>{
  demo = e.target.checked;
  toast(`Demo Mode ${demo?'ON':'OFF'}`);
  resetSimState();
  try{
    await api('/demo/mode', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({demo})});
    console.log('Server demo mode set ->', demo);
  }catch(err){ console.warn('Failed to set server demo mode', err); }
});

// Demo batch tester refs
const demoBatchFiles = $('demoBatchFiles');
const demoDbPath = $('demoDbPath');
const demoFilenameExpect = $('demoFilenameExpect');
const demoBatchSummary = $('demoBatchSummary');
const demoBatchWrap = $('demoBatchTableWrap');
const demoBatchTableBody = $('demoBatchTableBody');

const sortModeSelect = $('sortSelect');
const sortOperationSelect = $('sortOperationSelect');
const setupLookupInput = $('lookupScryfallSetup');
const setupLookupButton = $('btnLookupScryfallSetup');
const lookupCardName = $('lookupCardName');
const lookupCardPrinted = $('lookupCardPrinted');
const lookupCardSet = $('lookupCardSet');
const lookupCardYear = $('lookupCardYear');
const lookupCardValue = $('lookupCardValue');
const lookupCardMode = $('lookupCardMode');
const lookupCardMatch = $('lookupCardMatch');
const lookupCardCell = $('lookupCardCell');
const lookupCardReason = $('lookupCardReason');

let sortModeReady = false;
let sortOperationReady = false;

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
on($('btnHomeX'), 'click', ()=> api('/motion/home_x',{method:'POST'}).then(()=>toast('Homed X axis')).catch(e=>toast(e.message)));
on($('btnHomeY'), 'click', ()=> api('/motion/home_y',{method:'POST'}).then(()=>toast('Homed Y axis')).catch(e=>toast(e.message)));
on($('btnHomeZ'), 'click', ()=> api('/motion/home_z',{method:'POST'}).then(()=>toast('Homed Z axis')).catch(e=>toast(e.message)));

// Jog controls
on($('btnJogXPlus'), 'click', ()=> {
  const distance = parseFloat($('jogDistance').value) || 1.0;
  jogAxis('X', distance);
});
on($('btnJogXMinus'), 'click', ()=> {
  const distance = parseFloat($('jogDistance').value) || 1.0;
  jogAxis('X', -distance);
});
on($('btnJogYPlus'), 'click', ()=> {
  const distance = parseFloat($('jogDistance').value) || 1.0;
  jogAxis('Y', distance);
});
on($('btnJogYMinus'), 'click', ()=> {
  const distance = parseFloat($('jogDistance').value) || 1.0;
  jogAxis('Y', -distance);
});
on($('btnJogZPlus'), 'click', ()=> {
  const distance = parseFloat($('jogDistance').value) || 1.0;
  jogAxis('Z', distance);
});
on($('btnJogZMinus'), 'click', ()=> {
  const distance = parseFloat($('jogDistance').value) || 1.0;
  jogAxis('Z', -distance);
});

    // Test vacuum button
    const btnTestVacuum = document.getElementById('btnTestVacuum');
    if (btnTestVacuum) {
        btnTestVacuum.addEventListener('click', async () => {
            try {
                const response = await fetch('/motion/test_vacuum', {
                    method: 'POST'
                });
                const result = await response.json();
                console.log('Test vacuum result:', result);
            } catch (error) {
                console.error('Error testing vacuum:', error);
            }
        });
    }

    // Z drop and vacuum button
    const btnZDropVacuum = document.getElementById('btnZDropVacuum');
    if (btnZDropVacuum) {
        btnZDropVacuum.addEventListener('click', async () => {
            try {
                const response = await fetch('/motion/z_drop_and_vacuum', {
                    method: 'POST'
                });
                const result = await response.json();
                console.log('Z drop and vacuum result:', result);
            } catch (error) {
                console.error('Error with Z drop and vacuum:', error);
            }
        });
    }

on($('btnVacuumOff'), 'click', ()=> {
  api('/vacuum/off', {method:'POST'})
    .then(() => toast('Vacuum off'))
    .catch(e => toast(e.message));
});

// ------------- Grid / Cells -------------
let cells = []; // [{id,x,y,z}, ...]

async function loadGrid(){
  try{
    const res = await api('/grid/cells');
    cells = res?.cells ?? [];
  }catch(e){
    // Fallback Excel-style: columns A..K, rows 1..3; (A-row = feeders), K3 is error cell
    cells = [];
    // Expanded grid: A1 to K3 (11 columns × 3 rows) - exclude ERR1
    const cols = ['A','B','C','D','E','F','G','H','I','J','K'];
    const columnSpacing = 84.0; // mm between columns
    const rowSpacing = 104.0;   // mm between rows
    
    for(let r=1;r<=3;r++){
      for(let colIndex=0; colIndex<cols.length; colIndex++){
        const col = cols[colIndex];
        const id = `${col}${r}`;
        const x = colIndex * columnSpacing;  // 0, 84, 168, 252, 336, 420, 504, 588, 672, 756, 840
        const y = (r-1) * rowSpacing;        // 0, 104, 208
        cells.push({id, x, y, z:0});
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
  host.style.gridTemplateColumns = `repeat(${Math.max(1, cols.length)}, minmax(50px, 1fr))`;
  host.style.gap = '4px';

  // Sort cells for proper display order (A1, A2, A3, B1, B2, B3, etc.)
  const sortedCells = [...list].sort((a, b) => {
    const aRow = parseInt(a.id.replace(/[A-Z]/g, '')) || 0;
    const bRow = parseInt(b.id.replace(/[A-Z]/g, '')) || 0;
    if (aRow !== bRow) return aRow - bRow;
    const aCol = a.id.replace(/[0-9]/g, '');
    const bCol = b.id.replace(/[0-9]/g, '');
    return aCol.localeCompare(bCol);
  });

  sortedCells.forEach(c=>{
    const el = document.createElement('div');
    el.className = 'cell';
    el.textContent = c.id;
    el.title = `${c.id}: (${c.x?.toFixed(1) || 0},${c.y?.toFixed(1) || 0},${c.z?.toFixed(1) || 0})mm`;
    
    if(c.id === 'K3'){ 
      el.classList.add('err'); 
      el.title += ' • Error cell'; 
    }

    // Highlight feeder cells (column A) in green
    if (c.id && c.id[0] === 'A') {
      el.classList.add('feeder');
      el.style.backgroundColor = '#e6f9e6';
      el.style.borderColor = '#4CAF50';
      el.title += ' • Feeder';
    }

    // Make cells clickable so clicking a cell in the main grid will move the head
    el.style.cursor = 'pointer';
    el.classList.add('clickable');
    el.addEventListener('click', async ()=>{
      try{
        await api('/motion/move', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cell: c.id})});
        toast(`Moved to ${c.id}`);
      }catch(e){ toast(`Move failed: ${e.message}`); }
    });

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
  host.style.gridTemplateColumns = `repeat(${Math.max(1, cols.length)}, minmax(50px, 1fr))`;
  host.style.gap = '4px';

  // Sort cells for proper display order
  const sortedCells = [...cells].sort((a, b) => {
    const aRow = parseInt(a.id.replace(/[A-Z]/g, '')) || 0;
    const bRow = parseInt(b.id.replace(/[A-Z]/g, '')) || 0;
    if (aRow !== bRow) return aRow - bRow;
    const aCol = a.id.replace(/[0-9]/g, '');
    const bCol = b.id.replace(/[0-9]/g, '');
    return aCol.localeCompare(bCol);
  });

  sortedCells.forEach(c=>{
    const el = document.createElement('div');
    el.className = 'cell'; 
    el.textContent = c.id; 
    el.title = `${c.id}: (${c.x?.toFixed(1) || 0},${c.y?.toFixed(1) || 0},${c.z?.toFixed(1) || 0})mm`;
    
    if(c.id === 'K3'){ 
      el.classList.add('err'); 
      el.title += ' • Error cell'; 
    }

    // Highlight feeder cells (column A) in green
    if (c.id && c.id[0] === 'A') {
      el.classList.add('feeder');
      el.style.backgroundColor = '#e6f9e6';
      el.style.borderColor = '#4CAF50';
      el.title += ' • Feeder';
    }

    el.addEventListener('click', async ()=>{
      try{
        await api('/motion/move', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cell: c.id})});
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
  const sort = (sortModeSelect && sortModeSelect.value) || 'alpha_exact';
  if(!game || !sort){ toast('Select game and sorting'); return; }
  try{
    const payload = {
      game, sorting: sort,
      feeder_estimate: Number($('feederCapacity').value||0),
      divert_uncertain: $('divertUncertain').checked
    };
    payload.sort_mode = sort;
    await api('/run/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    runLoop.start();
    panelRun.scrollIntoView({behavior:'smooth', block:'start'});
  }catch(e){ toast(`Start failed: ${e.message}`); }
});

on($('btnEndRun'), 'click', async ()=>{
  if(!confirm('End the current run?')) return;
  try{
    await api('/run/end', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({notes: $('runNotes').value})});
    runLoop.stop(); toast('Run ended');
    panelCalibrate.scrollIntoView({behavior:'smooth', block:'start'});
  }catch(e){ toast(`End failed: ${e.message}`); }
});

on($('btnManualDivert'),'click', ()=> api('/run/divert_current',{method:'POST'}).then(()=>toast('Current card diverted to K3')).catch(e=>toast(e.message)));

on($('btnMoveToCell'), 'click', async ()=>{
  const id = $('positionSelect').value;
  if(!id){ toast('Select a cell'); return; }
  try{
    await api('/motion/move',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cell:id})});
    toast(`Moved to ${id}`);
  }catch(e){ toast(`Move failed: ${e.message}`); }
});
// Set the controller's current position (manual override)
on($('btnSetCurrent'), 'click', async ()=>{
  const id = $('positionSelect').value;
  if(!id){ toast('Select a cell'); return; }
  try{
    const res = await api('/motion/set_current', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cell: id})});
    toast(`Set current to ${id}`);
    // optional: log returned position object for debugging
    console.log('set_current ->', res);
  }catch(e){ toast(`Set current failed: ${e.message}`); }
});
on($('btnHomeAll2'),'click', ()=> api('/motion/home_all',{method:'POST'}).then(()=>toast('Homed all')).catch(e=>toast(e.message)));
on($('btnHomeXY'),'click',  ()=> api('/motion/home_xy',{method:'POST'}).then(()=>toast('Homed XY')).catch(e=>toast(e.message)));
on($('btnHomeZRun'),'click',   ()=> api('/motion/home_z',{method:'POST'}).then(()=>toast('Homed Z')).catch(e=>toast(e.message)));

async function jogAxis(axis, distance) {
  try {
    const res = await api('/motion/jog', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({axis: axis, distance: distance})
    });
    console.log(`jog ${axis} ${distance} ->`, res);
  } catch(e) {
    toast(`Jog ${axis} failed: ${e.message}`);
  }
}

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

// Simulate assign and show generated G-code
on($('btnSimulateAssign'), 'click', async ()=>{
  const name = $('simCardName').value.trim();
  if(!name){ toast('Enter a card name'); return; }
  try{
    const res = await api('/simulate/assign_move', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
    if(!res || typeof res !== 'object'){
      toast('Simulate failed: unexpected response from server');
      return;
    }
    let out = `Assigned to ${res.cell || '(unknown)'} — ${res.reason || '(no reason)'}\n\n`;
    if(Array.isArray(res.gcode) && res.gcode.length){
      out += res.gcode.join('\n');
    } else if(typeof res.gcode === 'string'){
      out += res.gcode;
    } else {
      out += '; no gcode preview available';
    }
    $('logOutput').textContent = out;
    $('dlgLogs').showModal();
  }catch(e){ toast(`Simulate failed: ${e.message}`); }
});

function resetLookupDetails(){
  if(lookupCardName) lookupCardName.textContent = '—';
  if(lookupCardPrinted) lookupCardPrinted.textContent = '—';
  if(lookupCardSet) lookupCardSet.textContent = '—';
  if(lookupCardYear) lookupCardYear.textContent = '—';
  if(lookupCardValue) lookupCardValue.textContent = '—';
  if(lookupCardMode) lookupCardMode.textContent = '—';
  if(lookupCardMatch) lookupCardMatch.textContent = '—';
  if(lookupCardCell) lookupCardCell.textContent = '—';
  if(lookupCardReason) lookupCardReason.textContent = '—';
}

function renderLookupDetails(payload){
  resetLookupDetails();
  if(!payload || typeof payload !== 'object') return;
  const card = payload.card || {};
  if(lookupCardName) lookupCardName.textContent = card.name || '—';
  if(lookupCardPrinted) lookupCardPrinted.textContent = card.printed_name || card.flavor_name || '—';

  if(lookupCardSet){
    const setParts = [];
    const setName = card.set_name || card.setName;
    if(setName) setParts.push(String(setName));
    const code = card.set_code || card.set;
    if(code) setParts.push(String(code).toUpperCase());
    const collector = card.collector_number || card.collectorNumber;
    if(collector) setParts.push(`#${collector}`);
    lookupCardSet.textContent = setParts.length ? setParts.join(' · ') : '—';
  }

  if(lookupCardYear){
    const year = card.released_year || (card.released_at ? String(card.released_at).slice(0, 4) : '');
    lookupCardYear.textContent = year || '—';
  }

  if(lookupCardValue){
    const usd = card.price_usd || (card.prices && card.prices.usd);
    if(usd === null || usd === undefined || usd === ''){
      lookupCardValue.textContent = '—';
    }else{
      const num = Number(usd);
      lookupCardValue.textContent = Number.isFinite(num) ? `$${num.toFixed(2)}` : `$${usd}`;
    }
  }

  if(lookupCardMode){
    const label = payload.mode_label || payload.mode;
    lookupCardMode.textContent = label || '—';
  }

  if(lookupCardMatch){
    const details = payload.reason_details || {};
    let matchText = '—';
    if(details.kind === 'sort_op'){
      const opLabel = details.operation || 'override';
      matchText = `Sort op: ${opLabel}`;
    }else if(details.divert){
      matchText = details.reason ? `Divert: ${details.reason}` : 'Divert';
    }else if(details.overflow){
      const key = details.key || details.mode;
      matchText = key ? `Overflow (${key})` : 'Overflow';
    }else if(details.key){
      matchText = details.key;
    }
    lookupCardMatch.textContent = matchText;
  }

  if(lookupCardCell) lookupCardCell.textContent = payload.cell || '—';
  if(lookupCardReason) lookupCardReason.textContent = payload.reason || '—';
}

async function lookupScryfallId(){
  if(!setupLookupInput){
    return;
  }
  const scryId = setupLookupInput.value.trim();
  if(!scryId){
    resetLookupDetails();
    return;
  }
  const sortMode = (sortModeSelect && sortModeSelect.value) || 'alpha_exact';
  const payload = {
    scryfall_id: scryId,
    sorting: sortMode,
    name: scryId,
    confidence: 1.0,
  };
  payload.sort_mode = sortMode;
  if(sortOperationSelect && !sortOperationSelect.disabled && sortOperationSelect.value){
    payload.sort_operation = sortOperationSelect.value;
  }
  try{
    const res = await api('/debug/assign_preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    renderLookupDetails(res);
  }catch(err){
    toast(`Lookup failed: ${err.message}`);
  }
}

if(setupLookupButton){
  on(setupLookupButton, 'click', lookupScryfallId);
}
if(setupLookupInput){
  on(setupLookupInput, 'keydown', (ev)=>{
    if(ev.key === 'Enter'){
      ev.preventDefault();
      lookupScryfallId();
    }
  });
}

async function setSortMode(modeId){
  if(!sortModeSelect) return;
  try{
    const res = await api('/sorting/mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: modeId}),
    });
    const activeId = typeof res?.active === 'string' ? res.active : (typeof modeId === 'string' ? modeId : '');
    if(activeId){
      sortModeSelect.value = activeId;
    }
    if(res?.ok){
      const label = res.label || activeId || 'mode';
      toast(`Sort mode set to ${label}`);
    }else if(res?.message){
      toast(res.message);
    }
  }catch(err){
    toast(`Failed to set sort mode: ${err.message}`);
    sortModeReady = false;
    await loadSortModes();
    return;
  }
  if(typeof doPreview === 'function') doPreview();
  lookupScryfallId();
}

async function loadSortModes(){
  if(!sortModeSelect) return;
  sortModeReady = false;
  sortModeSelect.disabled = true;
  sortModeSelect.innerHTML = '<option value="">Loading sort modes…</option>';
  try{
    const res = await api('/sorting/modes');
    const modes = Array.isArray(res?.modes) ? res.modes : [];
    sortModeSelect.innerHTML = '';
    if(modes.length === 0){
      const opt = document.createElement('option');
      opt.value = 'alpha_exact';
      opt.textContent = 'Alphabetical (A–Z)';
      sortModeSelect.appendChild(opt);
      sortModeSelect.value = 'alpha_exact';
      sortModeSelect.disabled = false;
      sortModeReady = true;
      return;
    }
    modes.forEach((mode)=>{
      const opt = document.createElement('option');
      opt.value = mode.id;
      let label = mode.label || mode.id;
      if(mode.type === 'year') label = 'Year';
      else if(mode.type === 'set') label = 'Set';
      else if(mode.type === 'alpha') label = 'Alphabetical (A–Z)';
      const countLabel = typeof mode.count === 'number' && mode.count > 0 ? ` (${mode.count})` : '';
      opt.textContent = `${label}${countLabel}`;
      sortModeSelect.appendChild(opt);
    });
    let active = res?.active && modes.find(m=>m.id === res.active) ? res.active : null;
    if(!active && res?.default && modes.find(m=>m.id === res.default)){
      active = res.default;
    }
    if(!active){
      active = modes[0].id;
    }
    sortModeSelect.value = active;
    sortModeSelect.disabled = false;
    sortModeReady = true;
    if(typeof doPreview === 'function') doPreview();
    lookupScryfallId();
  }catch(err){
    console.warn('Unable to load sort modes', err);
    sortModeSelect.innerHTML = '<option value="alpha_exact">Alphabetical (fallback)</option>';
    sortModeSelect.disabled = false;
    sortModeSelect.value = 'alpha_exact';
    sortModeReady = true;
    if(typeof doPreview === 'function') doPreview();
    lookupScryfallId();
  }
}

async function setSortOperation(opId){
  if(!sortOperationSelect) return;
  try{
    const res = await api('/sorting/operation', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({operation: opId})
    });
    const activeId = typeof res?.active === 'string' ? res.active : (typeof opId === 'string' ? opId : '');
    if(activeId && sortOperationSelect){
      sortOperationSelect.value = activeId;
    }
    if(res?.ok){
      const option = sortOperationSelect.querySelector(`option[value="${activeId}"]`);
      const label = option?.textContent || activeId || 'default';
      toast(`Sort operation set to ${label}`);
    }else if(res?.message){
      toast(res.message);
    }
    lookupScryfallId();
  }catch(err){
    toast(`Failed to set sort operation: ${err.message}`);
    sortOperationReady = false;
    await loadSortOperations();
  }
}

async function loadSortOperations(){
  if(!sortOperationSelect) return;
  sortOperationReady = false;
  sortOperationSelect.disabled = true;
  sortOperationSelect.innerHTML = '<option value="">Loading operations…</option>';
  try{
    const res = await api('/sorting/operations');
    const ops = Array.isArray(res?.operations) ? res.operations : [];
    sortOperationSelect.innerHTML = '';
    if(ops.length === 0){
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'No operations configured';
      sortOperationSelect.appendChild(opt);
      sortOperationSelect.disabled = true;
      resetLookupDetails();
      return;
    }
    ops.forEach((op)=>{
      const opt = document.createElement('option');
      opt.value = op.id;
      const countLabel = typeof op.count === 'number' ? ` (${op.count})` : '';
      opt.textContent = `${op.label || op.id}${countLabel}`;
      sortOperationSelect.appendChild(opt);
    });
    const active = (res?.active && ops.find(o=>o.id === res.active)) ? res.active : ops[0].id;
    sortOperationSelect.value = active;
    sortOperationSelect.disabled = false;
    sortOperationReady = true;
    lookupScryfallId();
  }catch(err){
    console.warn('Unable to load sort operations', err);
    sortOperationSelect.innerHTML = '<option value="">Operations unavailable</option>';
    sortOperationSelect.disabled = true;
    resetLookupDetails();
  }
}

if(sortOperationSelect){
  on(sortOperationSelect, 'change', (ev)=>{
    if(!sortOperationReady || sortOperationSelect.disabled) return;
    const opId = ev.target.value;
    setSortOperation(opId);
  });
}

if(sortModeSelect){
  on(sortModeSelect, 'change', (ev)=>{
    if(!sortModeReady || sortModeSelect.disabled) return;
    const modeId = ev.target.value;
    setSortMode(modeId);
  });
}

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

// Motion status polling (driver name, demo flag, position)
async function pollMotionStatus(){
  try{
    const s = await api('/motion/status');
    const chip = $('motionStatus');
    if(!chip) return;
    const pos = s.pos || [0,0,0];
    const driver = s.driver || 'unknown';
    const connected = s.connected ? 'CONNECTED' : 'DISCONNECTED';
    const port = s.port || 'unknown';
    const connColor = s.connected ? '#4CAF50' : '#f44336';
    
    chip.innerHTML = `
      <div style="display: flex; align-items: center; gap: 8px;">
        <span style="color: ${connColor}; font-weight: bold;">●</span>
        <span>Motion: ${driver} (${connected}) on ${port}</span>
        <span>@ ${Number(pos[0]).toFixed(1)},${Number(pos[1]).toFixed(1)},${Number(pos[2]).toFixed(1)}</span>
      </div>
    `;
    
    // Update position display
    const posDisplay = $('currentPosition');
    if(posDisplay){
      posDisplay.textContent = `Position: X=${Number(pos[0]).toFixed(1)} Y=${Number(pos[1]).toFixed(1)} Z=${Number(pos[2]).toFixed(1)}`;
    }
  }catch(e){ 
    const chip = $('motionStatus');
    if(chip) chip.textContent = 'Motion: ERROR - ' + e.message;
  }
}

// Start polling motion status every second
setInterval(pollMotionStatus, 1000);
pollMotionStatus();
on($('btnScanDevice'), 'click', async ()=>{ await scanDevice(); toast('Scan complete'); });

setInterval(pollCameraStatus, 4000);
pollCameraStatus();

// Auto-scan periodically when not in demo mode so UI updates when device is plugged/unplugged
setInterval(()=>{ if(!demo) scanDevice(); }, 5000);
scanDevice();

// Device modal: show detected ports and allow adopting one as gcode port
on($('btnScanDevice'), 'click', async ()=>{
  // show modal
  const dlg = $('dlgDevice');
  const list = $('deviceList'); list.innerHTML = '';
  const res = await scanDevice();
  const ports = res?.ports || [];
  ports.forEach(p=>{
    const li = document.createElement('li');
    li.style.padding = '6px 8px';
    li.style.borderBottom = '1px solid #eee';
    const radio = document.createElement('input'); radio.type='radio'; radio.name='device'; radio.value = p.device;
    const lbl = document.createElement('label'); lbl.textContent = ` ${p.device} — ${p.description || ''}`;
    li.appendChild(radio); li.appendChild(lbl);
    list.appendChild(li);
  });
  dlg.showModal();
});

on($('btnCloseDevice'), 'click', ()=> $('dlgDevice').close());

on($('btnAdoptPort'), 'click', async ()=>{
  // find selected radio
  const radios = Array.from(document.querySelectorAll('input[name=device]'));
  const chosen = radios.find(r=>r.checked);
  if(!chosen){ toast('Select a device first'); return; }
  const port = chosen.value;
  const baud = Number($('deviceBaud').value || 115200);
  const persist = $('devicePersist')?.checked || false;
  try{
    // POST to /demo/mode to switch driver; pass gcode_opts
  const res = await api('/demo/mode', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({demo:false, gcode_opts:{port, baud}, persist})});
    if(res && res.error){
      toast(`Adopt failed: ${res.error}`);
    } else {
      toast(`Adopted ${port} @ ${baud}`);
      $('dlgDevice').close();
    }
    // refresh status regardless
    await pollMotionStatus();
    await scanDevice();
  }catch(e){ toast(`Adopt failed: ${e.message}`); }
});

// Camera modal: show detected camera devices and allow selecting active camera
on($('btnScanCamera'), 'click', async ()=>{
  const dlg = $('dlgCamera');
  const list = $('camList'); list.innerHTML = '';
  try{
    // ask server for candidate camera devices (probe up to 6 indices)
    const res = await api('/camera/devices?max_index=6');
    const candidates = res?.candidates || [];
    candidates.forEach(c => {
      const li = document.createElement('li');
      li.style.padding = '6px 8px'; li.style.borderBottom = '1px solid #eee';
      const radio = document.createElement('input'); radio.type='radio'; radio.name='camera'; radio.value = String(c.id);
      const lbl = document.createElement('label'); lbl.style.marginLeft = '8px';
      lbl.textContent = ` ${c.id} ${c.type?('('+c.type+')'):''} ${c.available?'' : '• unavailable'}`;
      li.appendChild(radio); li.appendChild(lbl);
      list.appendChild(li);
    });
    dlg.showModal();
  }catch(err){ toast(`Camera scan failed: ${err.message}`); }
});

on($('btnCloseCam'), 'click', ()=> $('dlgCamera').close());

on($('btnSelectCam'), 'click', async ()=>{
  // find selected radio
  const radios = Array.from(document.querySelectorAll('input[name=camera]'));
  const chosen = radios.find(r=>r.checked);
  if(!chosen){ toast('Select a camera first'); return; }
  const devVal = chosen.value;
  const persist = $('camPersist')?.checked || false;
  let device = devVal;
  // try to coerce numeric ids back to numbers when appropriate
  if(/^[0-9]+$/.test(devVal)){
    device = Number(devVal);
  }
  try{
    const res = await api('/camera/select', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({device})});
    if(res && res.ok){
      toast(`Camera selected: ${device}`);
      $('dlgCamera').close();
      // refresh preview immediately
  try{ refreshCamera(); pollCameraStatus(); }catch(e){}
      // optional persist: attempt to write to config via /demo/mode persistence trick is not supported here
      if(persist) toast('Note: persisting camera to config.yaml is not implemented on this endpoint');
    }else{
      toast(`Camera select failed`);
    }
  }catch(e){ toast(`Select failed: ${e.message}`); }
});

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
  const sort = (sortModeSelect && sortModeSelect.value) || 'alpha_exact';
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
let previewDeb;
on($('previewName'), 'input', ()=>{
  clearTimeout(previewDeb); previewDeb = setTimeout(doPreview, 250);
});
on($('previewConf'), 'input', ()=> { clearTimeout(previewDeb); previewDeb = setTimeout(doPreview, 250); });

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
  const ok = !(reason?.startsWith('overflow') || reason?.startsWith('divert'));
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
    // Use api() wrapper so demo mode is honored (demoApi will handle the simulated response)
    const data = await api('/demo/batch_identify', {method:'POST', body: form});
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
      const columnSpacing = 84.0; // mm between columns  
      const rowSpacing = 104.0;   // mm between rows
      
      for(let r=1;r<=3;r++){
        for(let colIndex=0; colIndex<cols.length; colIndex++){
          const col = cols[colIndex];
          const id = `${col}${r}`;
          const x = colIndex * columnSpacing;  // 0, 84, 168, 252, 336, 420, 504, 588, 672, 756, 840
          const y = (r-1) * rowSpacing;        // 0, 104, 208
          cells.push({id, x, y, z:0});
        }
      }
      return {cells};
    }

    // Motion / actuators (no-ops)
    case '/motion/estop':
    case '/motion/home_all':
    case '/motion/home_x':
    case '/motion/home_y':
    case '/motion/home_z':
    case '/motion/home_xy':
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
    case '/camera/dual_ocr_snapshot': {
      const payload = opts?.body ? JSON.parse(opts.body) : {};
      const cell = payload.cell || 'A1';
      const offsetMm = payload.offset_mm || 44.0;
      return {
        success: true,
        dual_mode: true,
        offset_mm: offsetMm,
        cell: cell,
        final_position: [125.0, 44.0, 15.0],
        card_name: 'Lightning Bolt',
        set_code: 'M11',
        rules_text: 'Lightning Bolt deals 3 damage to any target',
        orientation: {
          determination: 'img1_top (score: 3.25 vs 1.80)',
          top_half_shape: [480, 640],
          bottom_half_shape: [480, 640]
        },
        regions: {
          card_name: { text: 'Lightning Bolt', confidence: 85.2 },
          set_code: { text: 'M11', confidence: 92.1 },
          rules_text: { text: 'Lightning Bolt deals 3 damage to any target', confidence: 78.5 }
        }
      };
    }

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

    // Demo batch identify — simulate the server-side /demo/batch_identify endpoint
    case '/demo/batch_identify': {
      // opts.body may be a FormData object when called from the UI; in demo mode we'll simulate results
      const files = [];
      try{
        // If opts.body is a FormData-like object, try to extract filenames
        if(opts?.body && typeof opts.body === 'object' && opts.body.entries){
          for(const pair of opts.body.entries()){ if(pair[0]==='files'){ files.push(pair[1]?.name || 'file'); } }
        }
      }catch(e){ /* ignore */ }

      const results = files.map((fname, idx) => {
        // simple heuristic: if filename contains 'error' return an error, otherwise simulate OCR -> identify
        if(/error|bad/i.test(fname)){
          return {index: idx+1, filename: fname, error: 'Simulated read error'};
        }
        // Simulate OCR by deriving name from filename before __ or extension
        const base = fname.split('__')[0].split('.').slice(0, -1).join('.') || fname.replace(/\.[^.]+$/, '');
        const region_texts = {name: base.replace(/_/g,' ').trim()};
        const identified_name = region_texts.name;
        const id_score = 95.0;
        const card_conf = Math.min(1.0, id_score / 100.0);
        const assignment = {cell: letterMap[firstLetter(region_texts.name)] || ERROR_CELL, reason: `alpha_exact:${firstLetter(region_texts.name)}`};
        return {
          index: idx+1,
          filename: fname,
          ocr: {rotation:0, rotation_confidence:1.0, regions: {name: {text: region_texts.name}}},
          region_texts,
          identify: {best: {name: identified_name}, score: id_score},
          identified_name,
          id_score,
          assignment,
          match_name: true,
          match_cell: true
        };
      });

      const summary = {total: results.length, db_path: demoDbPath?.value || null, name_matches: results.filter(r=>r.match_name).length, cell_matches: results.filter(r=>r.match_cell).length, both_matches: results.filter(r=>r.match_name && r.match_cell).length };
      return {summary, results};
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

    // Simulate assignment + return rendered G-code preview (client-side demo)
    case '/simulate/assign_move': {
      const body = JSON.parse(opts?.body || '{}');
      const name = (body.name || '').trim();
      const action = (body.action || 'pick');
      const conf = Number(body.confidence ?? 1.0);
      const fl = /^[A-Z]/i.test(name) ? name[0].toUpperCase() : 'A';
      const cell = conf < 0.80 ? ERROR_CELL : (letterMap[fl] || ERROR_CELL);
      const reason = conf < 0.80 ? 'divert:low_confidence' : `alpha_exact:${fl}`;

      // Render a simple G-code preview similar to server.render_gcode_for_cell
      const x = (cell.charCodeAt(0) - 65) * 25; // simple positioning grid
      const y = (Number(cell.replace(/[^0-9]/g,'')) - 1) * 25;
      const z = 0.0;
      const travel_feed = 200;
      const pick_feed = 100;
      const mc_plunge = 'M110';
      const mc_vac_on = 'M100';
      const mc_vac_off = 'M101';
      const mc_plunge_up = 'M111';

      const lines = [];
      lines.push(`; Simulated G-code for action=${action} cell=${cell}`);
      lines.push('G21');
      lines.push('G90');
      const safe_z = z + 10.0;
      if(action === 'pick'){
        lines.push(`G1 X${x.toFixed(3)} Y${y.toFixed(3)} Z${safe_z.toFixed(3)} F${travel_feed}`);
        lines.push(mc_plunge);
        lines.push(`G1 Z${(z-5.0).toFixed(3)} F${pick_feed}`);
        lines.push(mc_vac_on);
        lines.push('G4 P0.05');
        lines.push(mc_plunge_up);
        lines.push(`G1 Z${safe_z.toFixed(3)} F${travel_feed}`);
      } else {
        lines.push(`G1 X${x.toFixed(3)} Y${y.toFixed(3)} Z${safe_z.toFixed(3)} F${travel_feed}`);
        lines.push(`G1 Z${(z-5.0).toFixed(3)} F${pick_feed}`);
        lines.push(mc_vac_off);
        lines.push('G4 P0.03');
        lines.push(`G1 Z${safe_z.toFixed(3)} F${travel_feed}`);
      }
      return {cell, reason, gcode: lines};
    }

    default: return {ok:true};
  }
}

// ------------- boot -------------
resetLookupDetails();
loadGrid();
loadSortModes();
loadSortOperations();
