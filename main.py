# app/main.py (or similar)
import asyncio
import logging
import os
from typing import List, Optional

import cv2
import numpy as np
import yaml
from fastapi import File, Form, HTTPException, UploadFile
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
import base64
import tempfile

from app.services import card_id, ocr
from app.services.assign import Card, SystemState, assign_card, load_config
from app.services.motion import configure_from_cfg, get_controller
from app.services import camera as camera_svc
from app.services import feeder_monitor

app = FastAPI()

# Serve the single-page UI and static assets from the `app/static/` folder
# - GET / will return app/static/index.html
# - static assets (JS/CSS) will be available under /static/
static_dir = os.path.join("app", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def read_index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        resp = FileResponse(index_path)
        # prevent aggressive caching during development so UI JS updates are picked up
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        return resp
    # If index.html is missing, return a small JSON explaining the issue
    raise HTTPException(status_code=404, detail="Web UI not found. Ensure app/static/index.html exists.")


@app.middleware("http")
async def dev_cache_control(request, call_next):
    # Set no-store for the main JS bundle to avoid stale scripts during iterative development.
    path = request.url.path
    resp = await call_next(request)
    if path == '/static/app.js' or path == '/':
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp

# Load raw YAML so we can extract optional gcode/driver options in addition to the
# typed Config used by assignment logic.
with open("config.yaml", "r", encoding="utf8") as cfg_fh:
    raw_cfg = yaml.safe_load(cfg_fh)
CFG = load_config(raw_cfg)
STATE = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})
# configure motion controller cells from config
configure_from_cfg(CFG)
# Configure camera + feeder monitor services so they share runtime config
try:
    camera_svc.configure(raw_cfg.get('camera'))
except Exception as exc:
    print(f"[camera] configuration failed: {exc}")
try:
    feeder_monitor.configure_from_cfg(CFG, raw_cfg.get('camera'))
except Exception as exc:
    print(f"[feeder monitor] configuration failed: {exc}")
# Wire demo vs real G-code driver based on config. If a `gcode` section is
# present in the YAML we assume the operator intends to talk to real hardware
# and default demo to False unless `demo: true` is explicitly set. If no
# `gcode` section exists we default to demo=True for safety (simulated driver).
gcode_opts = raw_cfg.get('gcode')
if 'demo' in raw_cfg:
    demo_flag = bool(raw_cfg.get('demo'))
else:
    demo_flag = not bool(gcode_opts)

from app.services.motion import set_demo_mode
set_demo_mode(demo_flag, gcode_opts=gcode_opts)

# Controller singleton (driver already selected by set_demo_mode)
MOTION = get_controller()


@app.get("/camera/preview")
async def camera_preview():
    try:
        jpeg = await camera_svc.get_manager().grab_jpeg(quality=82, max_age=0.5)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return Response(content=jpeg, media_type="image/jpeg")


@app.get("/camera/stream")
async def camera_stream():
    stream_log = logging.getLogger("sort.camera.stream")

    async def _frame_iter():
        boundary = b"--frame"
        while True:
            try:
                frame = await camera_svc.get_manager().grab_jpeg(quality=75)
            except Exception as exc:  # pragma: no cover - streaming error path
                stream_log.warning("Camera stream error: %s", exc)
                await asyncio.sleep(0.5)
                continue
            yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(_frame_iter(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/camera/ocr_snapshot")
async def camera_ocr_snapshot():
    try:
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(None, camera_svc.get_manager().snapshot_for_ocr)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        result = ocr.process_card_image(frame)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OCR processing failed: {exc}")
    full = result.get('regions', {}).get('full', {})
    return {
        "text": full.get('text') or '',
        "confidence": full.get('confidence', 0.0),
        "result": result,
    }


@app.get("/camera/feeders")
async def camera_feeders():
    monitor = feeder_monitor.get_monitor()
    if not monitor or not getattr(monitor, 'enabled', False):
        raise HTTPException(status_code=503, detail="Feeder monitor unavailable")
    data = await monitor.measure()
    return {"results": data}


@app.post("/motion/move")
async def motion_move(payload: dict):
    """Move the head to a named cell (XY-only, demo-safe). Expects {'cell': 'A1'}"""
    cell = str(payload.get('cell', '')).strip().upper()
    if not cell:
        raise HTTPException(status_code=400, detail="Missing 'cell' in payload")
    try:
        # Perform a full move to the configured cell (X/Y/Z) when possible
        await MOTION.move_to_cell(cell)
        return {"ok": True, "cell": cell, "pos": MOTION.current}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown cell {cell}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# Simple grid endpoint used by the UI to populate the cell selector
@app.get("/grid/cells")
def grid_cells():
    # CFG.cells is a mapping of cell_id -> Cell dataclass (from load_config)
    out = []
    try:
        # Prefer the controller's configured cells if available
        ctrl = MOTION
        if ctrl and getattr(ctrl, 'cells', None):
            for cid, pos in ctrl.cells.items():
                out.append({"id": cid, "x": float(pos.get('x', 0.0)), "y": float(pos.get('y', 0.0)), "z": float(pos.get('z', 0.0))})
        else:
            for cid, cell in CFG.cells.items():
                out.append({"id": cid, "x": float(getattr(cell, 'x', 0) or 0.0), "y": float(getattr(cell, 'y', 0) or 0.0), "z": float(getattr(cell, 'z', 0) or 0.0)})
    except Exception:
        # Fallback simple grid if CFG not loaded correctly
        cols = ['A','B','C','D','E','F','G','H','I','J','K']
        for r in range(1,4):
            for c in cols:
                out.append({"id": f"{c}{r}", "x": 0, "y": 0, "z": 0})
    return {"cells": out}


@app.post("/motion/home_all")
async def motion_home_all():
    try:
        await MOTION.home_all()
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_a1")
async def motion_home_a1():
    """Home the machine into the back-left corner (A1) using limit switches.
    This will attempt to use the driver's move_until_limit where available and
    fall back to home_all if not supported.
    """
    try:
        # if CFG knows an A1 reference position, provide it
        a1_pos = None
        if hasattr(CFG, 'cells') and 'A1' in getattr(CFG, 'cells'):
            pos = getattr(CFG, 'cells')['A1']
            x = float(getattr(pos, 'x', 0.0) or 0.0)
            y = float(getattr(pos, 'y', 0.0) or 0.0)
            z = float(getattr(pos, 'z', 0.0) or 0.0)
            a1_pos = (x, y, z)
        await MOTION.home_to_a1(a1_pos=a1_pos)
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_xy")
async def motion_home_xy():
    """Simple home XY: move XY to (0,0) while preserving Z (demo-friendly)."""
    try:
        # preserve current Z
        cur_z = float(MOTION.current[2]) if MOTION.current is not None else 0.0
        await MOTION.driver.set_speed(MOTION.default_speed)
        await MOTION.driver.move_absolute(0.0, 0.0, cur_z, MOTION.default_speed)
        MOTION.current = (0.0, 0.0, cur_z)
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_z")
async def motion_home_z():
    """Home only Z axis (move Z to 0 keeping X/Y)."""
    try:
        cur_x, cur_y, _ = MOTION.current
        await MOTION.driver.set_speed(MOTION.default_speed)
        await MOTION.driver.move_absolute(cur_x, cur_y, 0.0, MOTION.default_speed)
        MOTION.current = (cur_x, cur_y, 0.0)
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/estop")
async def motion_estop():
    try:
        await MOTION.driver.stop()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post('/demo/mode')
def demo_mode(payload: dict):
    """Toggle demo mode on/off. Expects {'demo': true, 'gcode_opts': {...}}"""
    try:
        demo = bool(payload.get('demo', False))
        gcode_opts = payload.get('gcode_opts')
        persist = bool(payload.get('persist', False))
        from app.services.motion import set_demo_mode, is_demo_mode, get_driver_name
        result = set_demo_mode(demo, gcode_opts=gcode_opts)
        # set_demo_mode now returns a diagnostic dict; merge it into the response
        if isinstance(result, dict):
            # If caller asked to persist and we successfully created a GCodeDriver, write config.yaml
            if persist and result.get('ok') and gcode_opts and isinstance(gcode_opts, dict):
                try:
                    # load raw yaml, update gcode section and write back
                    raw = yaml.safe_load(open('config.yaml')) or {}
                    raw['gcode'] = gcode_opts
                    with open('config.yaml','w',encoding='utf8') as fh:
                        yaml.safe_dump(raw, fh)
                    result['persisted'] = True
                except Exception as e:
                    result['persisted'] = False
                    result['persist_error'] = str(e)
            return {"ok": result.get('ok', True), "demo": is_demo_mode(), "driver": result.get('driver', get_driver_name()), "error": result.get('error')}
        return {"ok": True, "demo": is_demo_mode(), "driver": get_driver_name()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get('/motion/status')
def motion_status():
    try:
        from app.services.motion import get_controller, is_demo_mode, get_driver_name
        ctrl = get_controller()
        return {
            "driver": get_driver_name(),
            "demo": is_demo_mode(),
            "pos": tuple(ctrl.current),
            "homed": bool(ctrl.homed),
            "cells_configured": len(getattr(ctrl, 'cells', {}) or {}),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post('/simulate/assign_move')
def simulate_assign_move(payload: dict):
    """Simulate assignment and return the cell plus G-code that would be used.
    Expects {name: str, game: str (optional), action: 'pick'|'place' (optional)}"""
    try:
        name = str(payload.get('name', '')).strip()
        if not name:
            raise HTTPException(status_code=400, detail='Missing name')
        game = payload.get('game', 'mtg')
        action = payload.get('action', 'pick')
        from app.services.assign import Card
        from app.services.assign import assign_card
        from app.services.motion import render_gcode_for_cell

        card = Card(game=game, name=name, confidence=1.0)
        cell, reason = assign_card(card, CFG, STATE)
        try:
            gcode = render_gcode_for_cell(cell, action=action)
        except KeyError:
            gcode = ['; no gcode available for unknown cell']
        return {"cell": cell, "reason": reason, "gcode": gcode}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get('/motion/detect')
def motion_detect():
    """Detect serial devices on the host and report whether the configured
    gcode serial port (if any) is present. Returns a JSON with 'ports' list
    and a 'connected' boolean and optional 'matched' port.
    """
    try:
        try:
            # lazy import so server can run without pyserial installed
            from serial.tools import list_ports
        except Exception:
            return {"ports": [], "connected": False, "error": "pyserial not available"}

        ports = []
        for p in list_ports.comports():
            ports.append({"device": p.device, "description": str(p.description)})

        # see if CFG contains a configured gcode port
        raw = globals().get('raw_cfg') or {}
        gcode = raw.get('gcode') if isinstance(raw, dict) else None
        cfg_port = None
        if isinstance(gcode, dict):
            cfg_port = gcode.get('port')
        matched = None
        note = None
        if cfg_port:
            for p in ports:
                if p['device'] == cfg_port or (cfg_port in p.get('description','')):
                    matched = p
                    break
        else:
            # No configured port: try to heuristically find a likely controller
            # Prefer ACM/USB devices or descriptions mentioning known firmware/serial chips
            heuristics = ['acm', 'usb', 'cdc', 'marlin', 'arduino', 'ch340', 'cp210x']
            for p in ports:
                dev = (p.get('device') or '').lower()
                desc = (p.get('description') or '').lower()
                if any(h in dev for h in ['ttyacm', 'ttyusb']) or any(h in desc for h in heuristics):
                    matched = p
                    note = 'auto-detected'
                    break

        connected = bool(matched)
        out = {"ports": ports, "connected": connected, "matched": matched}
        if note:
            out['note'] = note
        return out
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get('/camera/devices')
def camera_devices(max_index: int = 4):
    try:
        from app.services.camera import list_devices
        return list_devices(max_index=max_index)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post('/camera/select')
def camera_select(payload: dict):
    """Select the active camera device. Payload: {device: '/dev/video0' | 0}

    This updates the CameraManager configuration and closes/reopens the capture.
    """
    device = payload.get('device') if payload else None
    if device is None:
        raise HTTPException(status_code=400, detail='Missing device')
    try:
        from app.services.camera import get_manager
        mgr = get_manager()
        mgr.configure({'device': device})
        return {'ok': True, 'device': device}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# Plunger / vacuum control endpoints used by the UI
@app.post("/plunger/down")
async def plunger_down():
    try:
        await MOTION.driver.plunger_down()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/plunger/up")
async def plunger_up():
    try:
        await MOTION.driver.plunger_up()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/vacuum/on")
async def vacuum_on():
    try:
        await MOTION.driver.vacuum_on()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/vacuum/off")
async def vacuum_off():
    try:
        await MOTION.driver.vacuum_off()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/set_current")
async def motion_set_current(payload: dict):
    """Set the server's notion of the current head position using a named cell id or explicit x/y/z."""
    if not payload:
        raise HTTPException(status_code=400, detail="Missing payload")
    # allow {cell: 'A1'} or {x:..., y:..., z:...}
    cell = payload.get('cell')
    if cell:
        cell = str(cell).strip().upper()
        if cell not in CFG.cells:
            raise HTTPException(status_code=404, detail=f"Unknown cell {cell}")
        pos = CFG.cells[cell]
        # pos may be a dataclass Cell without x/y/z; default to 0
        x = float(getattr(pos, 'x', 0.0) or 0.0)
        y = float(getattr(pos, 'y', 0.0) or 0.0)
        z = float(getattr(pos, 'z', 0.0) or 0.0)
        MOTION.current = (x, y, z)
        return {"ok": True, "pos": MOTION.current}

    try:
        x = float(payload.get('x', MOTION.current[0]))
        y = float(payload.get('y', MOTION.current[1]))
        z = float(payload.get('z', MOTION.current[2]))
        MOTION.current = (x, y, z)
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post('/gcode/send')
async def gcode_send(payload: dict):
    """Send a raw G-code command to the active driver and return the firmware response lines.

    Payload: {"cmd": "M110"}
    """
    cmd = payload.get('cmd') if payload else None
    if not cmd or not isinstance(cmd, str):
        raise HTTPException(status_code=400, detail="Missing or invalid 'cmd' in payload")
    try:
        drv = MOTION.driver
        if not hasattr(drv, 'send_gcode'):
            raise HTTPException(status_code=400, detail="Current driver does not support raw G-code send")
        lines = await drv.send_gcode(cmd)
        return {"ok": True, "cmd": cmd, "lines": lines}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get('/gcode/mcodes')
def gcode_mcodes_get():
    """Return the active driver's mcodes mapping (if available).

    This is useful to verify which M-codes are configured for plunger/vacuum.
    """
    try:
        drv = MOTION.driver
        mc = getattr(drv, 'mcodes', None)
        return {"mcodes": mc}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post('/gcode/mcodes')
def gcode_mcodes_update(payload: dict):
    """Update the active driver's mcodes mapping in-memory. Payload: {"mcodes": {"plunger_down": "M42 P.. S.."}}

    This does not persist to config.yaml; use the device adoption UI to persist.
    """
    try:
        mc = payload.get('mcodes') if payload else None
        if not isinstance(mc, dict):
            raise HTTPException(status_code=400, detail="Missing or invalid 'mcodes' mapping")
        drv = MOTION.driver
        if not hasattr(drv, 'mcodes'):
            raise HTTPException(status_code=400, detail="Current driver does not support mcodes mapping")
        # update in-place
        drv.mcodes.update(mc)
        return {"ok": True, "mcodes": drv.mcodes}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _default_card_db_path() -> Optional[str]:
    """Return the default card database path if available."""
    env_path = os.environ.get("SORTME_CARD_DB_PATH")
    if env_path:
        env_path = os.path.expanduser(env_path)
        if os.path.exists(env_path):
            return env_path
    local_path = os.path.join("data", "demo_cards.json")
    if os.path.exists(local_path):
        return local_path
    return None


_CARD_DB_CACHE: Optional[List[dict]] = None
_CARD_DB_PATH: Optional[str] = None


def _load_card_db(path: str) -> List[dict]:
    """Load and cache the local card DB to avoid repeated disk hits."""

    global _CARD_DB_CACHE, _CARD_DB_PATH

    if not path:
        raise ValueError("Card database path is required")

    path = os.path.expanduser(path)
    if _CARD_DB_CACHE is None or _CARD_DB_PATH != path:
        cards = card_id.load_local_db(path)
        _CARD_DB_CACHE = cards
        _CARD_DB_PATH = path
    return _CARD_DB_CACHE

@app.get("/debug/alpha_map")
def alpha_map():
    return {"letter_to_cell": CFG.letter_to_cell}

@app.post("/debug/reset_counts")
def reset_counts():
    for k in STATE.counts_by_cell.keys():
        STATE.counts_by_cell[k] = 0
    return {"ok": True}

@app.post("/debug/assign")
def debug_assign(payload: dict):
    name = str(payload.get("name","")).strip()
    conf = float(payload.get("confidence", 1.0))
    card = Card(game=payload.get("game","mtg"), name=name, confidence=conf)
    cell, reason = assign_card(card, CFG, STATE)
    STATE.counts_by_cell[cell] = STATE.counts_by_cell.get(cell, 0) + 1
    return {"cell": cell, "reason": reason, "counts": STATE.counts_by_cell}

# Non-mutating preview endpoint for the UI assignment preview
@app.post("/debug/assign_preview")
def debug_assign_preview(payload: dict):
    name = str(payload.get("name","")).strip()
    conf = float(payload.get("confidence", 1.0))
    card = Card(game=payload.get("game","mtg"), name=name, confidence=conf)
    # reuse same assignment logic but DO NOT increment STATE
    cell, reason = assign_card(card, CFG, STATE)
    first = (name[:1].upper() if name and name[0].isalpha() else "A")
    return {"cell": cell, "reason": reason, "first": first}


@app.post("/demo/batch_identify")
async def demo_batch_identify(
    files: List[UploadFile] = File(...),
    db_path: Optional[str] = Form(None),
    use_filename_expected: bool = Form(True),
    ocr_only: bool = Form(False),
):
    """
    Run a batch OCR + identification pass for uploaded images.

    Expected usage (for quick demo workflows):
      - Upload multiple card images.
      - Optionally use the filename (e.g. "Lightning Bolt__B1.jpg") to
        supply the expected card name and/or cell.
      - Returns per-image OCR details, identification guesses, assignments,
        and aggregate accuracy stats.
    """

    if not files:
        raise HTTPException(status_code=400, detail="No images uploaded")

    active_db_path = db_path or _default_card_db_path()
    cards_db = None
    # Try to load a card DB if a path is provided; otherwise allow OCR-only operation
    if active_db_path:
        try:
            cards_db = _load_card_db(active_db_path)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise HTTPException(status_code=400, detail=f"Failed to load card DB: {exc}")

    results = []
    name_matches = 0
    cell_matches = 0
    both_matches = 0

    # local state snapshot so we don't mutate live counts
    state_snapshot = SystemState(counts_by_cell=dict(STATE.counts_by_cell))

    for idx, upload in enumerate(files, start=1):
        file_result = {
            "index": idx,
            "filename": upload.filename,
        }

        try:
            raw = await upload.read()
            if not raw:
                raise ValueError("Empty file")

            buffer = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("Unsupported image format")

            ocr_res = ocr.process_card_image(img, game="mtg")
            regions = ocr_res.get("regions", {})
            region_texts = {key: (val.get("text", "") if isinstance(val, dict) else "") for key, val in regions.items()}

            # If ocr_only flag present, skip identification and assignment and return simplified OCR-only data
            if ocr_only:
                # Build a single aggregated text string from the region_texts (preserve readable order if available)
                ordered_keys = [k for k in ['name','type_line','oracle','collector','full'] if k in region_texts]
                # append any other keys in their existing order
                ordered_keys += [k for k in region_texts.keys() if k not in ordered_keys]
                parts = [str(region_texts.get(k)) for k in ordered_keys if region_texts.get(k) is not None and region_texts.get(k) != ""]
                aggregated = "\n".join(parts) if parts else ""

                # Return only filename and the aggregated OCR text and the simple per-region strings
                file_result.update({
                    "ocr_text": aggregated,
                    "region_texts": region_texts,  # simple map of region -> text (strings only)
                })
                results.append(file_result)
                continue

            # If a cards DB is available, run identification. If not, but precomputed embeddings exist,
            # still run identification using the embeddings-only path.
            embeddings_dir = os.path.join("data", "embeddings")
            has_embeddings = os.path.exists(os.path.join(embeddings_dir, 'embeddings.npy')) and os.path.exists(os.path.join(embeddings_dir, 'cards_metadata.json'))

            if cards_db or has_embeddings:
                identify_res = card_id.identify_card_from_ocr(
                    region_texts,
                    cards_list=cards_db if cards_db else None,
                    embeddings_dir=embeddings_dir if has_embeddings else None,
                )
                best = identify_res.get("best") or {}
                identified_name = (best.get("name") or best.get("title") or region_texts.get("name") or "").strip()
                id_score = float(identify_res.get("score", 0.0))
            else:
                identify_res = {}
                best = {}
                identified_name = (region_texts.get("name") or "").strip()
                id_score = 0.0

            card_conf = min(1.0, id_score / 100.0) if id_score > 0 else 0.0

            card = Card(
                game="mtg",
                name=identified_name,
                set_code=(best.get("set") or best.get("set_code")),
                collector_number=(best.get("collector_number") or best.get("collector")),
                confidence=card_conf,
            )

            cell, reason = assign_card(card, CFG, state_snapshot)

            expected_name = None
            expected_cell = None
            if use_filename_expected and upload.filename:
                base = os.path.splitext(os.path.basename(upload.filename))[0]
                if "__" in base:
                    parts = base.split("__", 1)
                    expected_name = parts[0].replace("_", " ").strip()
                    expected_cell = parts[1].strip().upper() or None
                else:
                    expected_name = base.replace("_", " ").strip()

            if expected_cell is None and expected_name:
                tmp_card = Card(game="mtg", name=expected_name, confidence=1.0)
                expected_cell, _ = assign_card(tmp_card, CFG, state_snapshot)

            match_name = False
            if expected_name and identified_name:
                match_name = expected_name.lower() == identified_name.lower()

            match_cell = False
            if expected_cell and cell:
                match_cell = expected_cell.upper() == cell.upper()

            if match_name:
                name_matches += 1
            if match_cell:
                cell_matches += 1
            if match_name and match_cell:
                both_matches += 1

            file_result.update(
                {
                    "expected": {
                        "name": expected_name,
                        "cell": expected_cell,
                    },
                    "ocr": {
                        "rotation": ocr_res.get("rotation_detected"),
                        "rotation_confidence": ocr_res.get("rotation_confidence"),
                        "regions": regions,
                    },
                    "region_texts": region_texts,
                    "identify": identify_res,
                    "identify_debug": identify_res.get("debug"),
                    "identified_name": identified_name,
                    "id_score": id_score,
                    "assignment": {
                        "cell": cell,
                        "reason": reason,
                    },
                    "match_name": match_name,
                    "match_cell": match_cell,
                }
            )

        except Exception as exc:
            file_result.update({"error": str(exc)})

        results.append(file_result)

    summary = {
        "total": len(results),
        "db_path": active_db_path,
        "name_matches": name_matches,
        "cell_matches": cell_matches,
        "both_matches": both_matches,
    }

    return {
        "summary": summary,
        "results": results,
    }



@app.post('/ocr/upload')
async def ocr_upload(file: UploadFile = File(...)):
    """Accept a single uploaded image and run the OCR pipeline (process_card_image).

    Returns the OCR result (same shape as process_card_image) and, when available,
    a base64 JPEG of the preprocessed name-region for debugging under `debug_name_image`.
    """
    if not file:
        raise HTTPException(status_code=400, detail='No file uploaded')
    try:
        # save the file to a temp location so process_card_image can read by path
        suffix = os.path.splitext(file.filename or '')[1] or '.jpg'
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as fh:
            tmp_path = fh.name
            content = await file.read()
            fh.write(content)

        # run OCR
        from app.services import ocr as ocr_svc
        result = ocr_svc.process_card_image(tmp_path)

        # Try identification using card database / embeddings when available
        identify_info = None
        try:
            # build region_texts expected by identify_card_from_ocr
            regions = result.get('regions', {})
            region_texts = {}
            # prefer 'name' region if present
            if 'name' in regions and regions['name'].get('text'):
                region_texts['name'] = regions['name']['text']
            else:
                # fallback to full region text
                region_texts['full'] = regions.get('full', {}).get('text', '')

            # load local card DB if available
            active_db_path = _default_card_db_path()
            cards_db = None
            if active_db_path:
                try:
                    cards_db = _load_card_db(active_db_path)
                except Exception:
                    cards_db = None

            embeddings_dir = os.path.join('data', 'embeddings')
            if not os.path.exists(embeddings_dir):
                embeddings_dir = None

            id_res = card_id.identify_card_from_ocr(
                region_texts,
                cards_list=cards_db if cards_db else None,
                embeddings_dir=embeddings_dir if embeddings_dir else None,
            )
            best = id_res.get('best') or {}
            identified_name = (best.get('name') or best.get('title') or region_texts.get('name') or '').strip()
            id_score = float(id_res.get('score', 0.0) or 0.0)

            # run assignment using existing CFG/STATE
            card = Card(game='mtg', name=identified_name, confidence=min(1.0, id_score / 100.0))
            cell, reason = assign_card(card, CFG, STATE)
            identify_info = {
                'identified_name': identified_name,
                'id_score': id_score,
                'assignment': {'cell': cell, 'reason': reason},
                'identify_debug': id_res,
            }
        except Exception:
            identify_info = None

        # try to load debug name-region image if written by the OCR routine
        debug_img_path = '/tmp/ocr_name_region.jpg'
        debug_b64 = None
        if os.path.exists(debug_img_path):
            with open(debug_img_path, 'rb') as fh:
                debug_b64 = base64.b64encode(fh.read()).decode('ascii')

        # cleanup temp upload
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        out = {'result': result, 'debug_name_image': debug_b64, 'identify': identify_info}
        return out
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
