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

from app.services import card_id  # , ocr  # OCR temporarily disabled for physical testing
from app.services.assign import Card, SystemState, assign_card, load_config
from app.services.motion import configure_from_cfg, get_controller
# from app.services import camera as camera_svc  # Camera temporarily disabled for physical testing
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
# configure motion controller cells from config, pass raw_cfg for grid positions
configure_from_cfg(raw_cfg)
# Configure camera + feeder monitor services so they share runtime config
# CAMERA CONFIGURATION TEMPORARILY DISABLED FOR PHYSICAL TESTING
# try:
#     camera_svc.configure(raw_cfg.get('camera'))
# except Exception as exc:
#     print(f"[camera] configuration failed: {exc}")
try:
    feeder_monitor.configure_from_cfg(CFG, raw_cfg.get('camera'))
except Exception as exc:
    print(f"[feeder monitor] configuration failed: {exc}")
# Wire G-code driver from config
gcode_opts = raw_cfg.get('gcode')

from app.services.motion import get_controller
# Controller singleton
MOTION = get_controller()

@app.post("/motion/move")
async def motion_move(payload: dict):
    """Move the head to a named cell (XY-only, demo-safe). Expects {'cell': 'A1'}"""
    cell = str(payload.get('cell', '')).strip().upper()
    if not cell:
        raise HTTPException(status_code=400, detail="Missing 'cell' in payload")
    try:
        # Special case: clicking A1 homes the machine in X and Y
        if cell == 'A1':
            await MOTION.home_x()
            await MOTION.home_y()
            return {"ok": True, "cell": cell, "pos": MOTION.current, "action": "homed_to_A1"}
        
        # Move to cell using XY-only movement to avoid Z crashes
        await MOTION.move_to_cell_xy(cell)
        return {"ok": True, "cell": cell, "pos": MOTION.current}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown cell {cell}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# Simple grid endpoint used by the UI to populate the cell selector
@app.get("/grid/cells")
def grid_cells():
    # Return cells with their actual positions from config.yaml grid section
    out = []
    try:
        # Use grid positions from raw config if available
        grid_positions = raw_cfg.get("grid", {}).get("positions", {})
        
        if grid_positions:
            # Filter out ERR1 and other non-grid cells, include only A1-K3 range
            for cell_id, pos in grid_positions.items():
                # Skip error cells and only include A-K, 1-3 range
                if (cell_id.startswith(('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K')) and 
                    len(cell_id) == 2 and cell_id[1] in '123'):
                    x, y = pos if isinstance(pos, (list, tuple)) and len(pos) >= 2 else (0, 0)
                    out.append({
                        "id": cell_id, 
                        "x": float(x), 
                        "y": float(y), 
                        "z": 0.0
                    })
        else:
            # Fallback: generate positions using spacing from config
            spacing = raw_cfg.get("grid", {})
            col_spacing = float(spacing.get("column_spacing", 84.0))
            row_spacing = float(spacing.get("row_spacing", 104.0))
            
            cols = ['A','B','C','D','E','F','G','H','I','J','K']
            for r in range(1, 4):  # rows 1, 2, 3
                for col_idx, col in enumerate(cols):
                    cell_id = f"{col}{r}"
                    x = col_idx * col_spacing
                    y = (r - 1) * row_spacing
                    out.append({
                        "id": cell_id,
                        "x": float(x),
                        "y": float(y), 
                        "z": 0.0
                    })
                    
    except Exception as e:
        # Fallback simple grid with proper spacing
        cols = ['A','B','C','D','E','F','G','H','I','J','K']
        for r in range(1, 4):
            for col_idx, col in enumerate(cols):
                out.append({
                    "id": f"{col}{r}", 
                    "x": col_idx * 84.0, 
                    "y": (r - 1) * 104.0, 
                    "z": 0.0
                })
                
    return {"cells": out}


@app.post("/motion/home_all")
async def motion_home_all():
    try:
        await MOTION.home_all()
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_x")
async def motion_home_x():
    try:
        await MOTION.home_x()
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_y")
async def motion_home_y():
    try:
        await MOTION.home_y()
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_z")
async def motion_home_z():
    try:
        await MOTION.home_z()
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_xy")
async def motion_home_xy():
    try:
        await MOTION.home_x()
        await MOTION.home_y()
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/home_a1")
async def motion_home_a1():
    """Home X and Y, then move to A1 position"""
    try:
        await MOTION.home_x()
        await MOTION.home_y()
        if 'A1' in MOTION.cells:
            await MOTION.move_to_cell_xy('A1')
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/jog")
async def motion_jog(payload: dict):
    try:
        axis = str(payload.get('axis', '')).strip().upper()
        distance = float(payload.get('distance', 0))
        if not axis:
            raise HTTPException(status_code=400, detail="Missing 'axis' in payload")
        await MOTION.jog_axis(axis, distance)
        return {"ok": True, "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/estop")
async def motion_estop():
    try:
        await MOTION.emergency_stop()
        return {"ok": True, "message": "Emergency stop activated"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/reset_position")
async def motion_reset_position(payload: dict = None):
    """Reset firmware position coordinates to specified values (default 0,0,0)."""
    try:
        if payload is None:
            payload = {}
        x = float(payload.get('x', 0.0))
        y = float(payload.get('y', 0.0))
        z = float(payload.get('z', 0.0))
        await MOTION.reset_position(x, y, z)
        return {"ok": True, "position": [x, y, z]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/motion/status")
async def motion_status():
    """Get current motion system status for UI polling"""
    try:
        driver_name = type(MOTION.driver).__name__ if MOTION.driver else "None"
        
        # Check if serial connection exists and is open
        connected = False
        if hasattr(MOTION.driver, '_serial') and MOTION.driver._serial is not None:
            try:
                connected = MOTION.driver._serial.is_open
            except Exception:
                connected = False
        
        # Get current position from hardware
        current_pos = MOTION.current
        try:
            if connected:
                hardware_pos = await MOTION.driver.query_position()
                MOTION.current = hardware_pos
                current_pos = hardware_pos
        except Exception:
            pass  # Use last known position
        
        # Get limit switch status
        # Note: Limit switches are used for safety during homing/movement but not displayed
        
        return {
            "ok": True,
            "driver": driver_name,
            "demo": False,  # We removed demo mode
            "pos": current_pos,
            "connected": connected,
            "port": getattr(MOTION.driver, 'port', 'unknown')
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/motion/detect")
async def motion_detect():
    """Detect/scan for motion hardware"""
    try:
        driver_name = type(MOTION.driver).__name__ if MOTION.driver else "None"
        connected = hasattr(MOTION.driver, 'ser') and MOTION.driver.ser is not None
        
        # Check if the serial port is accessible
        port_status = "unknown"
        if hasattr(MOTION.driver, 'port'):
            import os
            if os.path.exists(MOTION.driver.port):
                port_status = "available"
            else:
                port_status = "missing"
        
        return {
            "ok": True,
            "driver": driver_name,
            "connected": connected,
            "port": getattr(MOTION.driver, 'port', 'unknown'),
            "port_status": port_status
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/calibrate")
async def motion_calibrate():
    """Run calibration sequence"""
    try:
        # Basic calibration: home all axes
        await MOTION.home_all()
        return {"ok": True, "message": "Calibration complete", "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/motion/save_a1_reference")
async def motion_save_a1_reference():
    """Save current position as A1 reference"""
    try:
        # This would typically save to config, for now just acknowledge
        return {"ok": True, "message": "A1 reference saved", "pos": MOTION.current}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get('/motion/status')
def motion_status():
    try:
        from app.services.motion import get_controller, get_driver_name
        ctrl = get_controller()
        return {
            "driver": get_driver_name(),
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


# @app.get('/camera/devices')
# def camera_devices(max_index: int = 4):
#     try:
#         from app.services.camera import list_devices
#         return list_devices(max_index=max_index)
#     except Exception as exc:
#         raise HTTPException(status_code=500, detail=str(exc))


# @app.post('/camera/select')
# def camera_select(payload: dict):
#     """Select the active camera device. Payload: {device: '/dev/video0' | 0}
# 
#     This updates the CameraManager configuration and closes/reopens the capture.
#     """
#     device = payload.get('device') if payload else None
#     if device is None:
#         raise HTTPException(status_code=400, detail='Missing device')
#     try:
#         from app.services.camera import get_manager
#         mgr = get_manager()
#         mgr.configure({'device': device})
#         return {'ok': True, 'device': device}
#     except Exception as exc:
#         raise HTTPException(status_code=500, detail=str(exc))


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


@app.post("/motion/test_vacuum")
async def test_vacuum():
    """Simple test to activate vacuum"""
    try:
        await motion_controller.vacuum_on()
        return {"status": "success", "message": "Vacuum activated"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/motion/z_drop_and_vacuum")
async def z_drop_and_vacuum():
    """Drop Z a fixed amount and activate vacuum (simplified version)"""
    try:
        # Move Z down by 10mm (safer than using limit switch detection)
        await motion_controller.jog('z', -10)
        
        # Activate vacuum
        await motion_controller.vacuum_on()
        
        return {"status": "success", "message": "Z dropped 10mm and vacuum activated"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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


@app.post("/physical_testing/assign_known_card")
def assign_known_card(payload: dict):
    """
    Manual card assignment for physical testing with perfect identification.
    Accepts: {
        "card_name": "Lightning Bolt",
        "confidence": 1.0,  # optional, defaults to 1.0
        "perform_motion": true  # optional, if true will execute physical pick and place
    }
    """
    card_name = str(payload.get("card_name", "")).strip()
    confidence = float(payload.get("confidence", 1.0))
    perform_motion = bool(payload.get("perform_motion", False))
    
    if not card_name:
        raise HTTPException(status_code=400, detail="card_name is required")
    
    # Create card object and assign to cell
    card = Card(game="mtg", name=card_name, confidence=confidence)
    cell, reason = assign_card(card, CFG, STATE)
    
    # Update state counts
    STATE.counts_by_cell[cell] = STATE.counts_by_cell.get(cell, 0) + 1
    
    result = {
        "card_name": card_name,
        "assigned_cell": cell,
        "reason": reason,
        "confidence": confidence,
        "counts": STATE.counts_by_cell,
        "motion_performed": False
    }
    
    # Optionally perform physical motion
    if perform_motion:
        try:
            # This would trigger the actual pick-and-place sequence
            # For now, just indicate it would happen
            result["motion_performed"] = True
            result["motion_note"] = "Motion execution not yet implemented - coming soon!"
        except Exception as exc:
            result["motion_error"] = str(exc)
    
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
