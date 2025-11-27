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

function base64ToBlob(base64, mime){
  try{
    const binary = atob(base64);
    const len = binary.length;
    const bytes = new Uint8Array(len);
    for(let i=0;i<len;i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], {type: mime || 'application/octet-stream'});
  }catch(err){
    console.warn('Failed to decode base64 payload', err);
    return null;
  }
}

function setCameraStatus(state){
  const chip = $('cameraStatus');
  if(!chip) return;
  if(!state){
    chip.textContent = 'Camera: unknown';
    chip.classList.add('muted');
    return;
  }
  const online = !!state.online;
  const path = state.path || state.device || 'unknown';
  let label = `Camera: ${online ? 'online' : 'offline'} ${path}`.trim();
  if(!online && state.error){
    label += ` — ${state.error}`;
  }
  chip.textContent = label;
  chip.classList.toggle('muted', !online);
  if(!online){
    if(cameraImg){
      cameraImg.removeAttribute('src');
      delete cameraImg.dataset.snapshotTs;
    }
  }
  cameraWasOnline = online;
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
const cameraImg = $('cameraLive');
const snapshotWrap = $('snapshotPreviewWrap');
const snapshotImgOcr = $('snapshotPreviewOcr');
const snapshotTimestamp = $('snapshotTimestamp');
const snapshotDownloadBtn = $('btnDownloadSnapshot');
const snapshotLocation = $('snapshotLocation');
const snapshotOcrFull = $('snapshotOcrFull');
const snapshotOcrName = $('snapshotOcrName');
const snapshotOcrOracle = $('snapshotOcrOracle');
const snapshotOcrCollector = $('snapshotOcrCollector');
const snapshotOcrMeta = $('snapshotOcrMeta');
const snapshotOcrScryfallId = $('snapshotOcrScryfallId');
const btnCopyOcrText = $('btnCopyOcrText');
const snapshotNeighborCard = $('snapshotNeighborCard');
const snapshotNeighborName = $('snapshotNeighborName');
const snapshotNeighborSet = $('snapshotNeighborSet');
const snapshotNeighborScore = $('snapshotNeighborScore');
const snapshotNeighborStatus = $('snapshotNeighborStatus');
const snapshotNeighborId = $('snapshotNeighborId');
const snapshotCardName = $('snapshotCardName');
const snapshotCardPrinted = $('snapshotCardPrinted');
const snapshotCardSet = $('snapshotCardSet');
const snapshotCardYear = $('snapshotCardYear');
const snapshotCardValue = $('snapshotCardValue');
const snapshotCardMode = $('snapshotCardMode');
const snapshotCardMatch = $('snapshotCardMatch');
const snapshotCardCell = $('snapshotCardCell');
const snapshotCardReason = $('snapshotCardReason');
const gridPreview = $('gridPreview');

let cells = [];
let cameraObjUrl = null;
let cameraWasOnline = false;
let cameraSnapPending = false;
let cameraBlob = null;
let lastSnapshotMeta = null;
let snapshotAssets = {};
let lastSnapshotOcr = null;
let lastEmbeddingMatch = null;
let snapshotLookupKey = null;
let snapshotLookupSeq = 0;
let snapshotLookupResult = null;

function releaseSnapshotUrls(){
  const urls = new Set();
  if(cameraObjUrl){
    urls.add(cameraObjUrl);
  }
  Object.values(snapshotAssets || {}).forEach(asset=>{
    if(asset && asset.url){
      urls.add(asset.url);
    }
  });
  urls.forEach(url=>{
    try{ URL.revokeObjectURL(url); }
    catch(err){ console.warn('Failed to revoke URL', err); }
  });
  cameraObjUrl = null;
  cameraBlob = null;
  snapshotAssets = {};
}

function clearSnapshotPreview(){
  releaseSnapshotUrls();
  lastSnapshotMeta = null;
  if(snapshotImgOcr){
    snapshotImgOcr.removeAttribute('src');
  }
  if(snapshotTimestamp){
    snapshotTimestamp.textContent = 'No snapshot yet';
  }
  if(snapshotLocation){
    snapshotLocation.textContent = 'Not saved yet';
  }
  if(snapshotDownloadBtn){
    snapshotDownloadBtn.disabled = true;
  }
  clearSnapshotOcr();
}

function clearSnapshotOcr(){
  lastSnapshotOcr = null;
  if(snapshotOcrFull){
    snapshotOcrFull.textContent = 'No OCR text yet';
    snapshotOcrFull.classList.add('muted');
  }
  if(snapshotOcrName){
    snapshotOcrName.textContent = '—';
  }
  if(snapshotOcrOracle){
    snapshotOcrOracle.textContent = '—';
  }
  if(snapshotOcrCollector){
    snapshotOcrCollector.textContent = '—';
  }
  if(snapshotOcrMeta){
    snapshotOcrMeta.textContent = 'Engine: —';
  }
  if(btnCopyOcrText){
    btnCopyOcrText.disabled = true;
  }
  renderEmbeddingMatch(null);
}

function formatOcr(value){
  if(!value) return '';
  return String(value).trim();
}

function resolveBestScryfallId(info){
  if(!info || typeof info !== 'object') return '';
  const best = info.best;
  const card = best && best.card ? best.card : null;
  if(!card || typeof card !== 'object') return '';
  return (
    card.scryfall_id ||
    card.scryfallId ||
    card.scryfallID ||
    card.id ||
    card.uuid ||
    ''
  );
}

function setScryfallIdDisplay(value){
  if(!snapshotOcrScryfallId) return;
  const formatted = value && typeof value === 'string' ? value.trim() : '';
  snapshotOcrScryfallId.textContent = formatted || '—';
}

async function ensureSnapshotLookupForId(scryfallId, opts = {}){
  const id = (scryfallId || '').trim();
  const force = !!opts.force;
  const noIdMessage = opts.noIdMessage || 'Awaiting embedding match…';
  if(!id){
    snapshotLookupSeq += 1;
    snapshotLookupKey = null;
    snapshotLookupResult = null;
    resetSnapshotCardDetails();
    if(snapshotCardReason){
      snapshotCardReason.textContent = noIdMessage;
    }
    return;
  }
  if(!force && snapshotLookupKey === id && snapshotLookupResult){
    return;
  }
  snapshotLookupKey = id;
  const seq = ++snapshotLookupSeq;
  snapshotLookupResult = null;
  resetSnapshotCardDetails();
  if(snapshotCardReason){
    snapshotCardReason.textContent = opts.pendingText || 'Looking up assignment…';
  }
  const sortMode = (sortModeSelect && sortModeSelect.value) || 'alpha_exact';
  const payload = {
    scryfall_id: id,
    sorting: sortMode,
    sort_mode: sortMode,
    name: opts.name || (lastSnapshotOcr && lastSnapshotOcr.name) || id,
    confidence: typeof opts.confidence === 'number' ? opts.confidence : 1.0,
  };
  if(sortOperationSelect && !sortOperationSelect.disabled && sortOperationSelect.value){
    payload.sort_operation = sortOperationSelect.value;
  }
  try{
    const res = await api('/debug/assign_preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if(seq !== snapshotLookupSeq || snapshotLookupKey !== id){
      return;
    }
    snapshotLookupResult = res;
    renderSnapshotCardDetails(res);

    // After a successful identification/lookup, home the Z axis (with a short delay)
    // so the head is safe/ready. Only attempt homing if the lookup returned a target
    // (e.g. a cell or identified card). Use a delayed, non-blocking call so the UI
    // remains responsive and the user sees the identification result first.
    try{
      const looksLikeAssigned = Boolean(res && (res.cell || res.assignment || res.identified_name || (typeof res.id_score === 'number' && res.id_score > 0)));
      if(looksLikeAssigned){
        setTimeout(()=>{
          // Use fetch directly to ensure we call the backend even if the UI is
          // running in local demo mode (demoApi would intercept api()).
          try{
            fetch(`${BASE}/motion/home_z`, {method: 'POST'})
              .then(async (r)=>{
                if(!r.ok){
                  const txt = await r.text().catch(()=> '');
                  throw new Error(`${r.status} ${r.statusText} ${txt}`.trim());
                }
                try{ await r.json(); }catch(e){}
                toast('Homed Z');
              })
              .catch(err=> console.warn('Home Z failed', err));
          }catch(err){ console.warn('Home Z fetch scheduling failed', err); }
        }, 2000);
      }
    }catch(err){
      console.warn('Error while scheduling Home Z', err);
    }
  }catch(err){
    if(seq !== snapshotLookupSeq || snapshotLookupKey !== id){
      return;
    }
    snapshotLookupResult = null;
    resetSnapshotCardDetails();
    if(snapshotCardReason){
      const prefix = opts.errorPrefix || 'Lookup failed: ';
      snapshotCardReason.textContent = `${prefix}${err.message}`;
    }
  }
}

function refreshSnapshotLookup(){
  if(snapshotLookupKey){
    ensureSnapshotLookupForId(snapshotLookupKey, {force: true});
  }
}

function renderEmbeddingMatch(info){
  lastEmbeddingMatch = info || null;
  const scryfallId = resolveBestScryfallId(info);
  setScryfallIdDisplay(scryfallId);
  const best = info && info.best;
  const card = best && best.card ? best.card : null;
  if(!best || !card){
    if(snapshotNeighborCard){
      snapshotNeighborCard.classList.add('muted');
    }
    if(snapshotNeighborName){
      snapshotNeighborName.textContent = info && info.error ? 'No match' : 'Nearest card pending…';
    }
    if(snapshotNeighborSet){
      snapshotNeighborSet.textContent = info && info.error ? info.error : '—';
    }
    if(snapshotNeighborScore){
      snapshotNeighborScore.textContent = '—';
    }
    if(snapshotNeighborId){
      snapshotNeighborId.textContent = '—';
    }
    if(snapshotNeighborStatus){
      if(info && info.error){
        snapshotNeighborStatus.textContent = info.error;
      }else{
        snapshotNeighborStatus.textContent = 'Embedding match unavailable.';
      }
    }
    const message = info && info.error ? info.error : 'No embedding match yet';
    ensureSnapshotLookupForId('', {noIdMessage: message});
    return;
  }
  if(snapshotNeighborCard){
    snapshotNeighborCard.classList.remove('muted');
  }
  if(snapshotNeighborName){
    snapshotNeighborName.textContent = card.name || 'Unknown card';
  }
  if(snapshotNeighborSet){
    const parts = [];
    if(card.set) parts.push(String(card.set).toUpperCase());
    if(card.collector_number) parts.push(`#${card.collector_number}`);
    snapshotNeighborSet.textContent = parts.length ? parts.join(' • ') : '—';
  }
  if(snapshotNeighborId){
    snapshotNeighborId.textContent = scryfallId || '—';
  }
  if(snapshotNeighborScore){
    const scoreVal = typeof best.score === 'number' ? Number(best.score).toFixed(1) : '—';
    snapshotNeighborScore.textContent = scoreVal;
  }
  if(snapshotNeighborStatus){
    const bits = [];
    if(info && info.engine){
      bits.push(`Model: ${info.engine}`);
    }
    if(typeof best.distance === 'number'){
      bits.push(`dist ${Number(best.distance).toFixed(3)}`);
    }
    snapshotNeighborStatus.textContent = bits.length ? bits.join(' • ') : '';
  }
  const confidence = typeof best.confidence === 'number' ? best.confidence : undefined;
  ensureSnapshotLookupForId(scryfallId, {
    name: card.name || card.printed_name || undefined,
    confidence,
  });
}

function updateSnapshotOcr(ocrMap, ocrMeta, embeddingInfo){
  const map = ocrMap && typeof ocrMap === 'object' ? ocrMap : {};
  const meta = ocrMeta && typeof ocrMeta === 'object' ? ocrMeta : {};
  lastSnapshotOcr = map;
  const fullText = formatOcr(map.full_text || map.full || map.oracle);
  if(snapshotOcrFull){
    if(fullText){
      snapshotOcrFull.textContent = fullText;
      snapshotOcrFull.classList.remove('muted');
    }else{
      snapshotOcrFull.textContent = 'No OCR text yet';
      snapshotOcrFull.classList.add('muted');
    }
  }
  // We now present OCR as a single unified text block (full_text). Keep band fields as
  // placeholders for debugging but do not populate them from the primary OCR flow.
  if(snapshotOcrName){
    snapshotOcrName.textContent = '—';
  }
  if(snapshotOcrOracle){
    snapshotOcrOracle.textContent = '—';
  }
  if(snapshotOcrCollector){
    snapshotOcrCollector.textContent = '—';
  }
  if(snapshotOcrMeta){
    const parts = [];
    if(meta.engine) parts.push(`Engine: ${meta.engine}`);
    if(typeof meta.duration_ms === 'number') parts.push(`Duration: ${meta.duration_ms} ms`);
    if(meta.error) parts.push(`Error: ${meta.error}`);
    snapshotOcrMeta.textContent = parts.length ? parts.join(' • ') : 'Engine: —';
  }
  if(btnCopyOcrText){
    btnCopyOcrText.disabled = !fullText;
  }
  const embedding = embeddingInfo || meta.embedding;
  renderEmbeddingMatch(embedding);
}

function setSnapshotOcrPending(){
  if(snapshotOcrFull){
    snapshotOcrFull.textContent = 'Running OCR…';
    snapshotOcrFull.classList.remove('muted');
  }
  if(snapshotOcrMeta){
    snapshotOcrMeta.textContent = 'Running OCR…';
  }
  if(btnCopyOcrText){
    btnCopyOcrText.disabled = true;
  }
  renderEmbeddingMatch(null);
}

async function runOcrForAsset(asset){
  if(!asset || !asset.blob){
    throw new Error('Missing OCR-ready asset');
  }
  const form = new FormData();
  const filename = asset.path ? asset.path.split('/').pop() : 'snapshot-ocr.png';
  form.append('file', asset.blob, filename);
  const response = await fetch(`${BASE}/ocr/run`, {
    method: 'POST',
    body: form,
  });
  if(!response.ok){
    const text = await response.text().catch(()=> '');
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function captureSnapshot(opts = {}){
  if(!cameraImg || cameraSnapPending) return;
  const silent = !!opts.silent;
  const btn = $('btnCameraSnapshot');
  const originalText = btn ? btn.textContent : '';
  const previousAssets = snapshotAssets;
  const previousCameraUrl = cameraObjUrl;
  try{
    cameraSnapPending = true;
    if(btn && !silent){
      btn.disabled = true;
      btn.textContent = 'Capturing…';
    }
  // Step 1: Capture snapshot
  const res = await fetch(`${BASE}/camera/snapshot?offset_mm=40&ts=${Date.now()}`, {cache: 'no-store'});
  if(!res.ok){
    throw new Error(`${res.status} ${res.statusText}`.trim());
  }
  const data = await res.json();
  // Pause 5 seconds before next step
  await new Promise(resolve => setTimeout(resolve, 5000));
    const frames = Array.isArray(data?.frames) ? data.frames : [];
    const imageMap = (data && typeof data.images === 'object') ? data.images : {};
    const findFrame = (label) => {
      if(!label) return null;
      const direct = imageMap && imageMap[label];
      if(direct) return direct;
      return frames.find(f=> String(f?.label||'').toLowerCase() === label);
    };
    const frameToAsset = (frame, opts = {}) => {
      if(!frame || !frame.image) return null;
      const blob = base64ToBlob(frame.image, frame.mime || 'image/jpeg');
      if(!blob) return null;
      const asset = {
        blob,
        mime: frame.mime || 'image/jpeg',
        url: null,
        path: frame.path || '',
      };
      if(opts.makeUrl){
        asset.url = URL.createObjectURL(blob);
      }
      return asset;
    };

    const compositePreference = ['composite_rotated','composite','composite_aligned'];
    let compositeFrame = null;
    for(const label of compositePreference){
      const candidate = findFrame(label);
      if(candidate && candidate.image){
        compositeFrame = candidate;
        break;
      }
    }
    if(!compositeFrame || !compositeFrame.image){
      throw new Error('Composite frame missing');
    }

    const compositeAsset = frameToAsset(compositeFrame, {makeUrl: true});
    if(!compositeAsset){
      throw new Error('Unable to decode composite snapshot');
    }

  const newAssets = {};
  let ocrAssetForRun = null;
    newAssets.composite = compositeAsset;
    const compositeLabelUsed = String(compositeFrame.label || 'composite');
    newAssets[compositeLabelUsed] = compositeAsset;

    cameraBlob = compositeAsset.blob;
    cameraObjUrl = compositeAsset.url;

    cameraImg.src = cameraObjUrl;

    const ts = typeof data?.timestamp === 'string' ? data.timestamp : String(Date.now());
    cameraImg.dataset.snapshotTs = ts;

    const rotatedFrame = compositeLabelUsed === 'composite_rotated' ? compositeFrame : findFrame('composite_rotated');
    if(rotatedFrame && !newAssets.composite_rotated){
      const asset = compositeLabelUsed === 'composite_rotated' ? compositeAsset : frameToAsset(rotatedFrame, {makeUrl: false});
      if(asset){
        newAssets.composite_rotated = asset;
      }
    }
    const alignedFrame = compositeLabelUsed === 'composite_aligned' ? compositeFrame : findFrame('composite_aligned');
    if(alignedFrame && !newAssets.composite_aligned){
      const alignedAsset = compositeLabelUsed === 'composite_aligned'
        ? compositeAsset
        : frameToAsset(alignedFrame, {makeUrl: true});
      if(alignedAsset){
        if(!alignedAsset.url){
          alignedAsset.url = URL.createObjectURL(alignedAsset.blob);
        }
        newAssets.composite_aligned = alignedAsset;
      }
    }

    const ocrPreparedFrame = findFrame('ocr_prepared');
    if(ocrPreparedFrame && ocrPreparedFrame.image){
      const ocrAsset = frameToAsset(ocrPreparedFrame, {makeUrl: true});
      if(ocrAsset){
        newAssets.ocr_prepared = ocrAsset;
        if(!ocrAssetForRun){
          ocrAssetForRun = ocrAsset;
        }
        if(snapshotImgOcr){
          snapshotImgOcr.src = ocrAsset.url;
        }
      }
    }else if(snapshotImgOcr){
      snapshotImgOcr.removeAttribute('src');
    }

    const topFrame = findFrame('top');
    if(topFrame && topFrame.image){
      const blob = base64ToBlob(topFrame.image, topFrame.mime || 'image/jpeg');
      if(blob){
        newAssets.top = {
          blob,
          mime: topFrame.mime || 'image/jpeg',
          url: null,
          path: topFrame.path || '',
        };
      }
    }

    const bottomFrame = findFrame('bottom');
    if(bottomFrame && bottomFrame.image){
      const blob = base64ToBlob(bottomFrame.image, bottomFrame.mime || 'image/jpeg');
      if(blob){
        newAssets.bottom = {
          blob,
          mime: bottomFrame.mime || 'image/jpeg',
          url: null,
          path: bottomFrame.path || '',
        };
      }

    snapshotAssets = newAssets;
    }

    lastSnapshotMeta = {
      timestamp: ts,
      offset: data?.offset_mm,
      frames,
      images: imageMap,
      processing: data?.processing,
    };

    if(!ocrAssetForRun){
      ocrAssetForRun = newAssets.ocr_prepared || newAssets.composite_rotated || newAssets.composite;
    }

    const processing = data?.processing || {};
    const ocrFrame = findFrame('ocr_text');
    const hasImmediateOcr = Boolean(processing?.ocr_map || processing?.ocr_result || (ocrFrame && ocrFrame.text));
    if(processing?.ocr_map || processing?.ocr_result){
      updateSnapshotOcr(processing.ocr_map, processing.ocr_result, processing.embedding || processing.ocr_result?.embedding);
    }else if(ocrFrame && ocrFrame.text){
      updateSnapshotOcr(ocrFrame.text, ocrFrame.meta, ocrFrame.meta?.embedding);
    }else{
      clearSnapshotOcr();
    }

    if(ocrAssetForRun){
      try{
        if(!hasImmediateOcr){
          setSnapshotOcrPending();
        }
        const ocrResponse = await runOcrForAsset(ocrAssetForRun);
        if(ocrResponse?.ocr_map){
          updateSnapshotOcr(ocrResponse.ocr_map, ocrResponse.ocr_meta, ocrResponse.embedding || ocrResponse.ocr_meta?.embedding);
        }
      }catch(err){
        console.warn('OCR run failed', err);
        if(!hasImmediateOcr){
          toast(`OCR failed: ${err.message}`);
        }
      }
    }

    if(snapshotTimestamp){
      let capturedAt = new Date(ts);
      if(Number.isNaN(capturedAt.getTime())){
        capturedAt = new Date();
      }
      const orientationTag = data?.processing?.analysis?.determination || data?.processing?.orientation?.determination;
      const rotationDir = data?.processing?.orientation?.rotated?.direction;
      const orientationText = orientationTag ? ` • ${orientationTag}` : '';
      const rotationText = rotationDir ? ` • rotated ${rotationDir}` : '';
      snapshotTimestamp.textContent = `Captured ${capturedAt.toLocaleString()} (offset ${data?.offset_mm ?? 0} mm)${orientationText}${rotationText}`;
    }
    if(snapshotLocation){
      const lines = [];
      if(snapshotAssets.ocr_prepared || (ocrPreparedFrame && ocrPreparedFrame.path)){
        const ocrPath = snapshotAssets.ocr_prepared?.path || ocrPreparedFrame?.path || 'Not saved';
        lines.push(`OCR Prep: ${ocrPath}`);
      }
      if(topFrame){
        lines.push(`Top: ${topFrame.path || 'Not saved'}`);
      }
      if(bottomFrame){
        lines.push(`Bottom: ${bottomFrame.path || 'Not saved'}`);
      }
      snapshotLocation.textContent = lines.length ? lines.join('\n') : 'Not saved yet';
    }
    if(snapshotDownloadBtn){
      snapshotDownloadBtn.disabled = false;
    }

    if(previousCameraUrl){
      try{ URL.revokeObjectURL(previousCameraUrl); }
      catch(err){ console.warn('Failed to revoke previous camera URL', err); }
    }
    Object.values(previousAssets || {}).forEach(asset=>{
      if(asset && asset.url){
        try{ URL.revokeObjectURL(asset.url); }
        catch(err){ console.warn('Failed to revoke previous asset URL', err); }
      }
    });
    // After successful snapshot, home Z and extrude
    // Pause 5 seconds before homing Z and extruding
    await new Promise(resolve => setTimeout(resolve, 5000));
    try {
      const extrudeRes = await fetch(`${BASE}/motion/home_z_and_extrude`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      });
      if (extrudeRes.ok) {
        const extrudeData = await extrudeRes.json();
        toast(extrudeData.message || 'Z homed and extruded');
      } else {
        toast('Failed to home Z and extrude');
      }
    } catch (extrudeErr) {
      toast(`Extrude error: ${extrudeErr.message}`);
    }
  }catch(err){
    if(!silent){
      toast(`Snapshot failed: ${err.message}`);
    }
  }finally{
    if(btn && !silent){
      btn.disabled = false;
      btn.textContent = originalText || 'Capture Snapshot';
    }
    cameraSnapPending = false;
  }
}

window.addEventListener('beforeunload', ()=>{
  clearSnapshotPreview();
});

if(btnCopyOcrText){
  on(btnCopyOcrText, 'click', async ()=>{
    if(!lastSnapshotOcr) return;
    const fullText = formatOcr(lastSnapshotOcr.full_text || lastSnapshotOcr.full || lastSnapshotOcr.oracle);
    if(!fullText){
      toast('No OCR text to copy');
      return;
    }
    try{
      await navigator.clipboard.writeText(fullText);
      toast('OCR text copied');
    }catch(err){
      console.warn('Failed to copy OCR text', err);
      toast('Failed to copy OCR text');
    }
  });
}

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

const setupCardRefs = {
  name: lookupCardName,
  printed: lookupCardPrinted,
  set: lookupCardSet,
  year: lookupCardYear,
  value: lookupCardValue,
  mode: lookupCardMode,
  match: lookupCardMatch,
  cell: lookupCardCell,
  reason: lookupCardReason,
};

const snapshotCardRefs = {
  name: snapshotCardName,
  printed: snapshotCardPrinted,
  set: snapshotCardSet,
  year: snapshotCardYear,
  value: snapshotCardValue,
  mode: snapshotCardMode,
  match: snapshotCardMatch,
  cell: snapshotCardCell,
  reason: snapshotCardReason,
};

function resetCardDetailRefs(refs){
  if(!refs) return;
  ['name','printed','set','year','value','mode','match','cell','reason'].forEach((key)=>{
    const el = refs[key];
    if(el) el.textContent = '—';
  });
}

function formatUsdValue(value){
  if(value === null || value === undefined || value === ''){
    return '—';
  }
  const num = Number(value);
  if(Number.isFinite(num)){
    return `$${num.toFixed(2)}`;
  }
  return `$${value}`;
}

function renderCardDetailRefs(refs, payload){
  resetCardDetailRefs(refs);
  if(!refs || !payload || typeof payload !== 'object') return;
  const card = payload.card || {};
  if(refs.name) refs.name.textContent = card.name || '—';
  if(refs.printed) refs.printed.textContent = card.printed_name || card.flavor_name || '—';

  if(refs.set){
    const setParts = [];
    const setName = card.set_name || card.setName;
    if(setName) setParts.push(String(setName));
    const code = card.set_code || card.set;
    if(code) setParts.push(String(code).toUpperCase());
    const collector = card.collector_number || card.collectorNumber;
    if(collector) setParts.push(`#${collector}`);
    refs.set.textContent = setParts.length ? setParts.join(' · ') : '—';
  }

  if(refs.year){
    const year = card.released_year || (card.released_at ? String(card.released_at).slice(0, 4) : '');
    refs.year.textContent = year || '—';
  }

  if(refs.value){
    const prices = card.prices || {};
    const usd = card.price_usd ?? prices.usd;
    refs.value.textContent = formatUsdValue(usd);
  }

  if(refs.mode){
    const label = payload.mode_label || payload.mode;
    refs.mode.textContent = label || '—';
  }

  if(refs.match){
    const details = (payload.reason_details && typeof payload.reason_details === 'object') ? payload.reason_details : {};
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
    refs.match.textContent = matchText;
  }

  if(refs.cell) refs.cell.textContent = payload.cell || '—';
  if(refs.reason){
    const reasonText = payload.reason || payload.error || '—';
    refs.reason.textContent = reasonText;
  }
}

function resetLookupDetails(){
  resetCardDetailRefs(setupCardRefs);
}

function renderLookupDetails(payload){
  renderCardDetailRefs(setupCardRefs, payload);
}

function resetSnapshotCardDetails(){
  resetCardDetailRefs(snapshotCardRefs);
}

function renderSnapshotCardDetails(payload){
  renderCardDetailRefs(snapshotCardRefs, payload);
}

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
  }catch(e){
    toast(`Pause failed: ${e.message}`);
  }
});
on($('btnResume'), 'click', async ()=>{
  try{
    await api('/run/resume', {method:'POST'});
    runLoop.start();
    hide($('btnResume'));
    show($('btnPause'));
  }catch(e){
    toast(`Resume failed: ${e.message}`);
  }
});
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

function setGridPlaceholder(message, opts = {}){
  if(!gridPreview) return;
  gridPreview.innerHTML = '';
  const note = document.createElement('div');
  note.className = `grid-placeholder ${opts.muted ? 'muted' : ''}`.trim();
  note.textContent = message;
  gridPreview.appendChild(note);
}

async function loadGrid(){
  if(!gridPreview){
    cells = [];
    return cells;
  }
  setGridPlaceholder('Loading grid…', {muted: true});
  try{
    const res = await api('/grid/cells');
    const list = Array.isArray(res?.cells) ? res.cells : [];
    if(list.length === 0){
      throw new Error('No cells returned');
    }
    cells = list;
    renderGridPreview('gridPreview', cells);
    populatePositions();
    return cells;
  }catch(err){
    console.warn('Failed to load grid', err);
    setGridPlaceholder(`Failed to load grid: ${err.message || err}`, {muted: true});
    throw err;
  }
}

function populatePositions(){
  const sel = $('positionSelect');
  if(!sel) return;
  sel.innerHTML = '<option disabled selected>Select a cell</option>';
  cells.forEach(c=>{
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.id;
    sel.appendChild(o);
  });
}
on($('btnReloadGrid'), 'click', ()=>{
  loadGrid()
    .then(()=> toast('Grid loaded'))
    .catch((err)=> toast(`Grid load failed: ${err.message}`));
});

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
on($('btnCameraSnapshot'),'click', ()=> captureSnapshot());
on(snapshotDownloadBtn, 'click', ()=>{
  const orderedLabels = ['composite_rotated','composite','composite_aligned','ocr_prepared','top','bottom'];
  const seenAssets = new Set();
  const downloadTargets = [];
  orderedLabels.forEach(label => {
    const asset = snapshotAssets[label];
    if(asset && asset.blob && !seenAssets.has(asset)){
      downloadTargets.push([label, asset]);
      seenAssets.add(asset);
    }
  });

  if(downloadTargets.length === 0){
    toast('No snapshot captured yet');
    return;
  }

  const meta = lastSnapshotMeta || {};
  let ts = meta.timestamp || cameraImg?.dataset?.snapshotTs;
  let isoName;
  try{
    const dt = ts ? new Date(ts) : new Date();
    isoName = dt.toISOString().replace(/[:.]/g,'-');
  }catch{
    ts = Date.now();
    isoName = new Date(ts).toISOString().replace(/[:.]/g,'-');
  }

  downloadTargets.forEach(([label, asset])=>{
    const mime = asset.mime || 'application/octet-stream';
    const ext = mime.includes('png') ? 'png' : (mime.includes('jpeg') || mime.includes('jpg') ? 'jpg' : 'bin');
    const filename = `snapshot-${isoName}-${label}.${ext}`;
    const anchor = document.createElement('a');
    let downloadUrl = asset.url;
    let needsCleanup = false;
    if(!downloadUrl){
      downloadUrl = URL.createObjectURL(asset.blob);
      needsCleanup = true;
    }
    anchor.href = downloadUrl;
    anchor.download = filename;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    if(needsCleanup && downloadUrl){
      setTimeout(()=> URL.revokeObjectURL(downloadUrl), 5000);
    }
  });

  toast(downloadTargets.length > 1 ? 'Downloaded snapshot bundle' : 'Downloaded snapshot');
});

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

function getJogDistance() {
  const sel = $('jogDistance');
  const value = sel ? parseFloat(sel.value) : NaN;
  return Number.isFinite(value) && value > 0 ? value : 1;
}

async function homeAxis(axis){
  if(!axis) return;
  const lower = axis.toLowerCase();
  try{
    await api(`/motion/home_${lower}`, {method: 'POST'});
    toast(`Homed ${lower.toUpperCase()}`);
  }catch(e){
    toast(`Home ${lower.toUpperCase()} failed: ${e.message}`);
  }
}

on($('btnJogXMinus'), 'click', ()=> jogAxis('X', -getJogDistance()));
on($('btnJogXPlus'), 'click', ()=> jogAxis('X',  getJogDistance()));
on($('btnJogYMinus'), 'click', ()=> jogAxis('Y', -getJogDistance()));
on($('btnJogYPlus'), 'click', ()=> jogAxis('Y',  getJogDistance()));
on($('btnJogZMinus'), 'click', ()=> jogAxis('Z', -getJogDistance()));
on($('btnJogZPlus'), 'click', ()=> jogAxis('Z',  getJogDistance()));
on($('btnJogUp'), 'click', ()=> jogAxis('Z',  getJogDistance()));
on($('btnJogDown'), 'click', ()=> jogAxis('Z', -getJogDistance()));
on($('btnHomeX'), 'click', ()=> homeAxis('x'));
on($('btnHomeY'), 'click', ()=> homeAxis('y'));
on($('btnHomeZ'), 'click', ()=> homeAxis('z'));
on($('btnSetJog100'), 'click', ()=>{
  const sel = $('jogDistance');
  if(!sel) return;
  const hasOption = Array.from(sel.options || []).some(opt => opt.value === '100');
  if(!hasOption){
    const opt = document.createElement('option');
    opt.value = '100';
    opt.textContent = '100mm';
    sel.appendChild(opt);
  }
  sel.value = '100';
  toast('Jog distance set to 100mm');
});

// Extruder control handlers
on($('btnExtrude'),'click', async ()=> {
  const btn = $('btnExtrude');
  const original = btn ? btn.textContent : null;
  try{
    if(btn){ btn.disabled = true; btn.textContent = 'Extruding...'; }
    await api('/extruder/extrude', {method: 'POST', body: JSON.stringify({amount:0.2, feed:50})});
    toast('Extruded 0.2mm (E+0.2)');
  }catch(e){
    toast(e.message || String(e));
  }finally{
    if(btn){ btn.disabled = false; if(original) btn.textContent = original; }
  }
});
on($('btnZDropExtrude'),'click', async ()=>{
  const btn = $('btnZDropExtrude');
  const original = btn ? btn.textContent : null;
  try{
    if(btn){ btn.disabled = true; btn.textContent = 'Z dropping...'; }
    await api('/motion/z_drop_and_extrude',{method:'POST'});
    toast('Z drop + extrude');
  }catch(e){ toast(e.message || String(e)); }
  finally{ if(btn){ btn.disabled = false; if(original) btn.textContent = original; } }
});

on($('btnRetract'),'click', async ()=>{
  const btn = $('btnRetract');
  const original = btn ? btn.textContent : null;
  try{
    if(btn){ btn.disabled = true; btn.textContent = 'Retracting...'; }
    await api('/extruder/retract',{method:'POST', body: JSON.stringify({amount:0.2, feed:50})});
    toast('Retracted 0.2mm (E-0.2)');
  }catch(e){ toast(e.message || String(e)); }
  finally{ if(btn){ btn.disabled = false; if(original) btn.textContent = original; } }
});

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
  refreshSnapshotLookup();
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
    refreshSnapshotLookup();
  }catch(err){
    console.warn('Unable to load sort modes', err);
    sortModeSelect.innerHTML = '<option value="alpha_exact">Alphabetical (fallback)</option>';
    sortModeSelect.disabled = false;
    sortModeSelect.value = 'alpha_exact';
    sortModeReady = true;
    if(typeof doPreview === 'function') doPreview();
    lookupScryfallId();
    refreshSnapshotLookup();
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
    refreshSnapshotLookup();
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
    refreshSnapshotLookup();
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
  // Basic placeholder for scanDevice to prevent ReferenceError
  async function scanDevice() {
    // TODO: Implement actual device scanning logic
    console.log('scanDevice called');
    // Example: update device list UI or fetch device status from backend
    // You can add more logic here as needed
    return Promise.resolve();
  }

// Start polling motion status every second
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
    case '/extruder/extrude':
    case '/extruder/retract':
    case '/motion/z_drop_and_extrude':
    case '/gcode/send':
      return {ok:true, lines:['ok']};
    
    // vacuum endpoints removed in demo mode

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
  case '/logs/tail': return {text:`[info] system ok\n[info] limit switch: false\n`};

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
