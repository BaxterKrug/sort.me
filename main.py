from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from urllib import error as urlerror, request as urlrequest
import serial.serialutil

import cv2  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
import yaml  # type: ignore[import-not-found]
from fastapi import FastAPI, File, HTTPException, UploadFile, Request  # type: ignore[import-not-found]
from fastapi.responses import FileResponse, Response, HTMLResponse  # type: ignore[import-not-found]
from fastapi.staticfiles import StaticFiles  # type: ignore[import-not-found]

from app.services import card_id
from app.services import camera as camera_svc
from app.services import motion
from app.services import feeder_monitor
from app.services import ocr_pipeline
from app.services import sort_session
from app.services.assign import (
    DEFAULT_SORT_MODE,
    DEFAULT_SORT_OPERATION,
    Card,
    SystemState,
    assign_card,
    configure_detail_provider,
    load_config,
)
from app.services.motion import configure_from_cfg, get_controller, is_demo_mode

LOG = logging.getLogger("sort.api")

app = FastAPI()

static_dir = Path("app") / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _load_raw_config() -> Dict[str, Any]:
    try:
        with open("config.yaml", "r", encoding="utf8") as cfg_fh:
            data = yaml.safe_load(cfg_fh) or {}
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        LOG.warning("Unable to load config.yaml: %s", exc)
        return {}


raw_cfg = _load_raw_config()
CFG = load_config(raw_cfg)
STATE = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})
STATE.active_sort_operation = CFG.default_sort_operation
STATE.active_sort_mode = CFG.default_sort_mode

configure_from_cfg(raw_cfg)

# Configure camera with fake hardware flag if set
camera_cfg = raw_cfg.get("camera") if isinstance(raw_cfg, dict) else None
if camera_cfg is None:
    camera_cfg = {}
# Pass the use_fake_hardware flag to camera configuration
use_fake_hardware = raw_cfg.get("use_fake_hardware", False)
camera_cfg["use_fake"] = use_fake_hardware

# If no device specified or device not available, auto-detect best camera
if not camera_cfg.get("use_fake", False):
    specified_device = camera_cfg.get("device")
    if specified_device is None:
        LOG.info("No camera device specified in config, attempting auto-detection...")
        try:
            devices = camera_svc.list_devices(max_index=10)
            if devices.get("recommended") is not None:
                camera_cfg["device"] = devices["recommended"]
                LOG.info(f"Auto-selected camera device: {devices['recommended']}")
            elif devices.get("count", 0) > 0:
                camera_cfg["device"] = devices["candidates"][0]["id"]
                LOG.info(f"Selected first available camera: {devices['candidates'][0]['id']}")
            else:
                LOG.warning("No cameras detected, will use device 0 as fallback")
                camera_cfg["device"] = 0
        except Exception as e:
            LOG.warning(f"Camera auto-detection failed: {e}, using default device 0")
            camera_cfg["device"] = 0

# Configure and verify camera
try:
    camera_svc.configure(camera_cfg)
    LOG.info(f"Camera configured: device={camera_cfg.get('device')}, use_fake={camera_cfg.get('use_fake', False)}")
    
    # Verify camera is actually working by checking status
    mgr = camera_svc.get_manager()
    camera_info = mgr.info(ensure_capture=True)
    
    if camera_info.get("online"):
        LOG.info(f"✓ Camera verification successful: {camera_info.get('resolution')} @ {camera_info.get('fps')}fps")
        # Try to grab a test frame to ensure it's really working
        try:
            test_frame = mgr.grab_frame_sync(max_age=0.0)
            if test_frame is not None and test_frame.size > 0:
                LOG.info(f"✓ Camera test frame captured successfully: shape={test_frame.shape}")
            else:
                LOG.error("✗ Camera test frame is invalid (empty or None)")
        except Exception as frame_exc:
            LOG.error(f"✗ Failed to capture test frame: {frame_exc}")
    else:
        error_msg = camera_info.get("error", "Unknown error")
        LOG.error(f"✗ Camera verification failed: {error_msg}")
        if camera_cfg.get("fallback_image"):
            LOG.warning(f"Camera offline but fallback image configured: {camera_cfg.get('fallback_image')}")
        else:
            LOG.error("No fallback image configured - camera operations will fail!")
            
except Exception as exc:  # pragma: no cover - best effort on startup
    LOG.error(f"✗ Camera setup failed: {exc}")
    import traceback
    LOG.error(traceback.format_exc())

try:
    feeder_monitor.configure_from_cfg(CFG, camera_cfg)
except Exception as exc:  # pragma: no cover - best effort on startup
    LOG.warning("Feeder monitor configuration failed: %s", exc)

MOTION = get_controller()
SESSION = sort_session.get_manager()
ERROR_LOG: List[Dict[str, Any]] = []

# Auto-sort loop control
AUTO_SORT_RUNNING = False
AUTO_SORT_TASK: Optional[asyncio.Task] = None
AUTO_SORT_STATS = {"cards_processed": 0, "errors": 0, "started_at": None}

# Snapshot protection to prevent concurrent requests
SNAPSHOT_IN_PROGRESS = False

CARD_METADATA_CACHE: Dict[str, Dict[str, Any]] = {}
CARD_DETAILS_CACHE: Dict[str, Dict[str, Any]] = {}
SET_CACHE: Dict[str, Dict[str, Any]] = {}


@app.on_event("startup")
async def startup_event():
    """Print system status summary on startup."""
    LOG.info("=" * 60)
    LOG.info("SYSTEM STARTUP SUMMARY")
    LOG.info("=" * 60)
    
    # Camera status
    camera_info = camera_svc.get_manager().info(ensure_capture=False)
    if camera_info.get("online"):
        LOG.info(f"✓ Camera: ONLINE (device={camera_info.get('device')}, {camera_info.get('resolution')})")
    else:
        error = camera_info.get("error", "Unknown error")
        LOG.warning(f"✗ Camera: OFFLINE ({error})")
    
    # Motion controller status
    try:
        if is_demo_mode():
            LOG.info("✓ Motion: DEMO MODE (fake hardware)")
        else:
            LOG.info(f"✓ Motion: Configured")
    except Exception as e:
        LOG.warning(f"✗ Motion: Error - {e}")
    
    # Configuration
    LOG.info(f"  Cells configured: {len(CFG.cells)}")
    LOG.info(f"  Sort mode: {CFG.default_sort_mode}")
    LOG.info(f"  Sort operation: {CFG.default_sort_operation}")
    
    LOG.info("=" * 60)
    LOG.info("Server ready at http://0.0.0.0:8000")
    LOG.info("=" * 60)


async def _await_motion_completion(
    ctrl: motion.MotionController,
    target: Optional[Tuple[float, float, float]] = None,
    *,
    tolerance: float = 0.5,
    timeout: float = 8.0,
    poll_interval: float = 0.05,
) -> Tuple[float, float, float]:
    """Wait for the motion controller to report a stable position near ``target``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_pos: Optional[Tuple[float, float, float]] = None
    last_error: Optional[Exception] = None

    try:
        if hasattr(ctrl.driver, "send_gcode"):
            await ctrl.driver.send_gcode("M400")  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - hardware specific
        LOG.debug("M400 wait failed: %s", exc)

    while loop.time() <= deadline:
        try:
            pos = await ctrl.driver.query_position()
            current = (float(pos[0]), float(pos[1]), float(pos[2]))
            
            # CRITICAL SAFETY CHECK: Detect runaway motion
            # If any axis exceeds reasonable limits, stop immediately
            MAX_X = 500.0  # Maximum X position in mm
            MAX_Y = 150.0  # Maximum Y position in mm  
            MAX_Z = 200.0  # Maximum Z position in mm
            
            if current[0] < -10.0 or current[0] > MAX_X:
                LOG.error(f"SAFETY: X position out of bounds: {current[0]:.1f}mm (limit: 0-{MAX_X})")
                raise RuntimeError(f"X-axis runaway detected: {current[0]:.1f}mm exceeds safe limits")
            if current[1] < -10.0 or current[1] > MAX_Y:
                LOG.error(f"SAFETY: Y position out of bounds: {current[1]:.1f}mm (limit: 0-{MAX_Y})")
                raise RuntimeError(f"Y-axis runaway detected: {current[1]:.1f}mm exceeds safe limits")
            if current[2] < -10.0 or current[2] > MAX_Z:
                LOG.error(f"SAFETY: Z position out of bounds: {current[2]:.1f}mm (limit: 0-{MAX_Z})")
                raise RuntimeError(f"Z-axis runaway detected: {current[2]:.1f}mm exceeds safe limits")
            
            ctrl.current = current
            last_pos = current
            if target is None:
                return current
            if all(abs(current[i] - target[i]) <= tolerance for i in range(3)):
                return current
        except RuntimeError as safety_exc:
            # Re-raise safety exceptions immediately
            raise
        except Exception as exc:  # pragma: no cover - hardware specific
            last_error = exc
            LOG.debug("query_position while waiting for completion failed: %s", exc)

        await asyncio.sleep(poll_interval)

    if last_pos is not None:
        LOG.warning(
            "Timed out waiting for motion completion (target=%s, last=%s)",
            target,
            last_pos,
        )
        return last_pos

    raise RuntimeError(
        "Motion controller did not report position; controller may still be booting"
        + (f": {last_error}" if last_error else "")
    )


async def _move_axis_relative(
    ctrl: motion.MotionController,
    axis: str,
    delta: float,
    speed: float,
    *,
    timeout: float = 12.0,
) -> None:
    """Move a single axis by ``delta`` millimeters without touching other axes."""
    if abs(delta) <= 1e-6:
        return

    driver = ctrl.driver
    axis = axis.upper()
    feed = int(speed)

    if hasattr(driver, "send_gcode"):
        # Use relative positioning to avoid relying on possibly stale absolute Z values.
        try:
            await driver.send_gcode("G91", timeout=1.5)  # relative mode
            await driver.send_gcode(f"G1 {axis}{delta:.3f} F{feed}", timeout=timeout)
        finally:
            try:
                await driver.send_gcode("G90", timeout=1.5)
            except Exception:
                LOG.warning("Failed to restore absolute positioning after relative move", exc_info=True)
    else:
        x, y, z = ctrl.current
        if axis == "X":
            x += delta
        elif axis == "Y":
            y += delta
        elif axis == "Z":
            z += delta
        await driver.move_absolute(x, y, z, speed)

SCRYFALL_TIMEOUT = float(os.environ.get("SCRYFALL_TIMEOUT", "4.0"))
SCRYFALL_ENABLED = os.environ.get("SCRYFALL_LOOKUPS", "1") != "0"


def _card_metadata_path() -> Optional[Path]:
    sorting_cfg = raw_cfg.get("sorting") if isinstance(raw_cfg, dict) else {}
    meta_path = None
    if isinstance(sorting_cfg, dict):
        meta_path = sorting_cfg.get("card_metadata_path")
    if isinstance(meta_path, str) and meta_path:
        path = Path(meta_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    default_path = Path("data/embeddings/cards_metadata.json")
    return default_path


def _embeddings_dir_path() -> Path:
    sorting_cfg = raw_cfg.get("sorting") if isinstance(raw_cfg, dict) else {}
    directory = None
    if isinstance(sorting_cfg, dict):
        directory = sorting_cfg.get("embeddings_dir")
    if isinstance(directory, str) and directory:
        path = Path(directory)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    return Path("data/embeddings")


EMBEDDINGS_DIR = _embeddings_dir_path()


def _load_cards_metadata() -> Dict[str, Dict[str, Any]]:
    global CARD_METADATA_CACHE
    if CARD_METADATA_CACHE:
        return CARD_METADATA_CACHE
    path = _card_metadata_path()
    if not path or not path.exists():
        LOG.debug("Card metadata file %s not found", path)
        CARD_METADATA_CACHE = {}
        return CARD_METADATA_CACHE
    try:
        with path.open("r", encoding="utf8") as fh:
            raw_meta = json.load(fh)
        mapping: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_meta, list):
            for item in raw_meta:
                if not isinstance(item, dict):
                    continue
                card_id = str(item.get("id") or "").strip().lower()
                if not card_id:
                    continue
                mapping[card_id] = {
                    "scryfall_id": item.get("id"),
                    "name": item.get("name"),
                    "set_code": item.get("set"),
                    "collector_number": item.get("collector_number"),
                    "printed_name": item.get("printed_name"),
                    "flavor_name": item.get("flavor_name"),
                    "set_name": item.get("set_name"),
                    "released_at": item.get("released_at"),
                    "released_year": item.get("released_year"),
                    "prices": item.get("prices"),
                }
        CARD_METADATA_CACHE = mapping
    except Exception as exc:  # pragma: no cover - robustness
        LOG.warning("Failed to load card metadata from %s: %s", path, exc)
        CARD_METADATA_CACHE = {}
    return CARD_METADATA_CACHE


def _fetch_scryfall_json(url: str) -> Optional[Dict[str, Any]]:
    if not SCRYFALL_ENABLED:
        return None
    try:
        req = urlrequest.Request(url, headers={"User-Agent": "sort.me/1.0"})
        with urlrequest.urlopen(req, timeout=SCRYFALL_TIMEOUT) as resp:  # type: ignore[arg-type]
            if getattr(resp, "status", 200) != 200:
                return None
            data = resp.read()
        return json.loads(data.decode("utf8"))
    except (urlerror.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - network variability
        LOG.debug("Scryfall fetch failed for %s: %s", url, exc)
        return None


def _get_set_info(set_code: Optional[str]) -> Dict[str, Any]:
    if not set_code:
        return {}
    code = str(set_code).strip().lower()
    if not code:
        return {}
    if code in SET_CACHE:
        return SET_CACHE[code]
    data = _fetch_scryfall_json(f"https://api.scryfall.com/sets/{code}")
    info: Dict[str, Any] = {}
    if data:
        info = {
            "code": data.get("code"),
            "name": data.get("name"),
            "released_at": data.get("released_at"),
        }
        released_at = info.get("released_at")
        if isinstance(released_at, str) and released_at:
            info["released_year"] = released_at.split("-")[0]
    SET_CACHE[code] = info
    return info


def _get_card_details(scryfall_id: Optional[str]) -> Dict[str, Any]:
    sid = str(scryfall_id or "").strip()
    if not sid:
        return {}
    key = sid.lower()
    if key in CARD_DETAILS_CACHE:
        return CARD_DETAILS_CACHE[key]
    meta = _load_cards_metadata().get(key, {})
    details: Dict[str, Any] = {
        "scryfall_id": sid,
        "name": meta.get("name"),
        "set_code": meta.get("set_code"),
        "collector_number": meta.get("collector_number"),
        "printed_name": meta.get("printed_name"),
        "flavor_name": meta.get("flavor_name"),
        "price_usd": meta.get("price_usd"),
    }
    if meta.get("set_name"):
        details["set_name"] = meta.get("set_name")
    if meta.get("released_at"):
        details["released_at"] = meta.get("released_at")
    if meta.get("released_year"):
        details["released_year"] = meta.get("released_year")
    if meta.get("prices"):
        details["prices"] = meta.get("prices")
    meta_prices = meta.get("prices")
    if isinstance(meta_prices, dict) and not details.get("price_usd"):
        details["price_usd"] = meta_prices.get("usd")
    card_json = _fetch_scryfall_json(f"https://api.scryfall.com/cards/{sid}")
    if card_json:
        details["name"] = card_json.get("name") or details.get("name")
        details["set_code"] = card_json.get("set") or details.get("set_code")
        details["collector_number"] = card_json.get("collector_number") or details.get("collector_number")
        details["set_name"] = card_json.get("set_name")
        details["released_at"] = card_json.get("released_at")
        details["flavor_name"] = card_json.get("flavor_name") or details.get("flavor_name")
        details["printed_name"] = (
            card_json.get("printed_name")
            or details.get("printed_name")
            or card_json.get("flavor_name")
            or details.get("flavor_name")
        )
        if card_json.get("prices"):
            details["prices"] = card_json.get("prices")
        prices = card_json.get("prices")
        if isinstance(prices, dict):
            usd_price = prices.get("usd") or prices.get("usd_foil") or prices.get("usd_etched")
            if usd_price:
                details["price_usd"] = usd_price
        released_at = details.get("released_at")
        if isinstance(released_at, str) and released_at:
            details["released_year"] = released_at.split("-")[0]

    set_code = details.get("set_code")
    if set_code and not details.get("set_name"):
        set_info = _get_set_info(set_code)
        if set_info:
            details.setdefault("set_name", set_info.get("name"))
            released_at = set_info.get("released_at")
            if isinstance(released_at, str) and released_at and not details.get("released_at"):
                details["released_at"] = released_at
                details["released_year"] = released_at.split("-")[0]

    if not details.get("printed_name") and details.get("flavor_name"):
        details["printed_name"] = details["flavor_name"]

    CARD_DETAILS_CACHE[key] = details
    return details


configure_detail_provider(_get_card_details)


def _normalize_operation_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    op = str(value).strip().lower()
    return op or None


def _normalize_mode_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    mode = str(value).strip().lower()
    return mode or None


def _summarize_reason(reason: Optional[str]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"raw": reason}
    if not reason:
        return summary
    parts = [segment for segment in str(reason).split(":") if segment != ""]
    if not parts:
        return summary

    head = parts[0]
    summary["kind"] = head

    if head == "sort_op":
        if len(parts) > 1:
            summary["operation"] = parts[1]
        if len(parts) > 2:
            summary["token"] = parts[2]
        return summary

    if head == "overflow":
        summary["overflow"] = True
        if len(parts) > 1:
            summary["mode"] = parts[1]
        if len(parts) > 2:
            summary["key"] = parts[2]
        if len(parts) > 3:
            summary["token"] = parts[3]
        return summary

    if head == "divert":
        summary["divert"] = True
        if len(parts) > 1:
            summary["reason"] = parts[1]
        return summary

    summary["mode"] = head
    if len(parts) > 1:
        summary["key"] = parts[1]
    if len(parts) > 2:
        summary["token"] = parts[2]
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grid_cells_from_config() -> List[Dict[str, Any]]:
    cells: List[Dict[str, Any]] = []
    grid_cfg = raw_cfg.get("grid", {}) if isinstance(raw_cfg, dict) else {}
    positions = grid_cfg.get("positions") if isinstance(grid_cfg, dict) else None
    if isinstance(positions, dict) and positions:
        for cell_id, pos in positions.items():
            if len(cell_id) == 2 and cell_id[0].isalpha() and cell_id[1].isdigit():
                if isinstance(pos, (list, tuple)) and len(pos) >= 2:
                    x, y = pos[:2]
                else:
                    x = y = 0.0
                cells.append({"id": cell_id, "x": float(x), "y": float(y), "z": 0.0})
        cells.sort(key=lambda item: item["id"])
        return cells

    spacing_x = float(grid_cfg.get("column_spacing", 84.0)) if isinstance(grid_cfg, dict) else 84.0
    spacing_y = float(grid_cfg.get("row_spacing", 104.0)) if isinstance(grid_cfg, dict) else 104.0
    columns = [chr(ord("A") + i) for i in range(11)]
    for row in range(1, 4):
        for col_index, col in enumerate(columns):
            cell_id = f"{col}{row}"
            cells.append({
                "id": cell_id,
                "x": float(col_index * spacing_x),
                "y": float((row - 1) * spacing_y),
                "z": 0.0,
            })
    return cells


async def _motion_status_payload() -> Dict[str, Any]:
    ctrl = MOTION
    driver = ctrl.driver
    driver_name = type(driver).__name__
    port = getattr(driver, "port", "virtual")
    pos = ctrl.current
    try:
        pos = await driver.query_position()  # type: ignore[attr-defined]
        ctrl.current = pos
    except Exception:
        pass
    
    # In demo mode (fake hardware), the virtual driver is always "connected"
    # In real mode, check if the serial connection exists
    is_demo = is_demo_mode()
    connected = is_demo or hasattr(driver, "_serial")
    
    payload = {
        "driver": driver_name,
        "pos": (float(pos[0]), float(pos[1]), float(pos[2])),
        "homed": bool(ctrl.homed),
        "cells_configured": len(ctrl.cells),
        "port": port,
        "demo": is_demo,
        "virtual": is_demo,
        "connected": connected,
        "ok": True,
    }
    return payload


def _append_error(reason: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entry = {
        "id": f"K3-{len(ERROR_LOG) + 1}",
        "reason": reason,
        "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
        "meta": meta or {},
    }
    ERROR_LOG.append(entry)
    return entry


# ---------------------------------------------------------------------------
# Static + middleware
# ---------------------------------------------------------------------------


@app.get("/")
def read_index() -> Response:
    index_path = static_dir / "index.html"
    if index_path.exists():
        resp = FileResponse(str(index_path))
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp
    raise HTTPException(status_code=404, detail="Web UI not found. Ensure app/static/index.html exists.")


@app.get("/status")
async def get_status() -> Dict[str, Any]:
    """Combined status endpoint for motion and camera."""
    motion_status = await _motion_status_payload()
    camera_mgr = camera_svc.get_manager()
    
    return {
        "ok": True,
        "motion": {
            "status": "Connected" if motion_status.get("connected") else "Disconnected",
            "driver": motion_status.get("driver"),
            "pos": motion_status.get("pos"),
            "homed": motion_status.get("homed"),
            "demo": motion_status.get("demo"),
        },
        "camera": {
            "status": "Ready" if camera_mgr else "Not Available",
            "device": getattr(camera_mgr, "device_id", None) if camera_mgr else None,
        }
    }


@app.middleware("http")
async def dev_cache_control(request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/static/app.js"}:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# Grid + configuration
# ---------------------------------------------------------------------------


@app.get("/grid/cells")
def grid_cells() -> Dict[str, Any]:
    return {"cells": _grid_cells_from_config()}


# ---------------------------------------------------------------------------
# Sorting helpers
# ---------------------------------------------------------------------------


@app.get("/sorting/modes")
def sorting_modes() -> Dict[str, Any]:
    modes_payload: List[Dict[str, Any]] = []
    for mode_id, mode in CFG.sort_modes.items():
        modes_payload.append({
            "id": mode_id,
            "label": mode.label,
            "type": mode.type,
            "count": len(mode.mapping),
            "default_cell": mode.default_cell,
        })

    modes_payload.sort(key=lambda item: item["label"].lower())

    default_mode = CFG.default_sort_mode or DEFAULT_SORT_MODE
    if default_mode in CFG.sort_modes:
        modes_payload.sort(key=lambda item: 0 if item["id"] == default_mode else 1)

    active = STATE.active_sort_mode or default_mode
    if active not in CFG.sort_modes and default_mode in CFG.sort_modes:
        active = default_mode

    return {
        "modes": modes_payload,
        "active": active if active in CFG.sort_modes else None,
        "default": default_mode if default_mode in CFG.sort_modes else None,
        "has_modes": bool(modes_payload),
    }


@app.post("/sorting/mode")
def sorting_set_mode(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not CFG.sort_modes:
        STATE.active_sort_mode = CFG.default_sort_mode
        return {"ok": False, "active": STATE.active_sort_mode, "message": "No sort modes configured"}

    requested = (
        _normalize_mode_id((payload or {}).get("mode"))
        or _normalize_mode_id((payload or {}).get("id"))
        or _normalize_mode_id((payload or {}).get("sort_mode"))
    )

    if not requested:
        STATE.active_sort_mode = CFG.default_sort_mode
    else:
        if requested not in CFG.sort_modes:
            raise HTTPException(status_code=404, detail=f"Unknown sort mode '{requested}'")
        STATE.active_sort_mode = requested

    active_id = STATE.active_sort_mode or CFG.default_sort_mode or DEFAULT_SORT_MODE
    mode_cfg = CFG.sort_modes.get(active_id)
    label = mode_cfg.label if mode_cfg else active_id
    return {"ok": True, "active": active_id, "label": label}


@app.get("/sorting/operations")
def sorting_operations() -> Dict[str, Any]:
    operations_payload: List[Dict[str, Any]] = []
    for op_id, mapping in CFG.sort_operations.items():
        label = "Default" if op_id == DEFAULT_SORT_OPERATION else op_id.replace("_", " ").title()
        operations_payload.append({
            "id": op_id,
            "label": label,
            "count": len(mapping),
        })

    # Stable sort: alpha by label, but keep configured default first if present
    operations_payload.sort(key=lambda item: item["label"].lower())
    default_op = CFG.default_sort_operation
    if default_op:
        operations_payload.sort(key=lambda item: 0 if item["id"] == default_op else 1)

    active = STATE.active_sort_operation or default_op
    if active not in CFG.sort_operations:
        active = None

    return {
        "operations": operations_payload,
        "active": active,
        "default": default_op,
        "has_operations": bool(operations_payload),
    }


@app.post("/sorting/operation")
def sorting_set_operation(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not CFG.sort_operations:
        STATE.active_sort_operation = None
        return {"ok": False, "active": None, "message": "No sort operations configured"}

    operation = _normalize_operation_id((payload or {}).get("operation") if payload else None)
    if not operation:
        STATE.active_sort_operation = CFG.default_sort_operation
        return {"ok": True, "active": STATE.active_sort_operation}

    if operation not in CFG.sort_operations:
        raise HTTPException(status_code=404, detail=f"Unknown sort operation '{operation}'")

    STATE.active_sort_operation = operation
    return {"ok": True, "active": operation}


# ---------------------------------------------------------------------------
# Motion endpoints
# ---------------------------------------------------------------------------


@app.post("/motion/goto/{cell_id}")
async def motion_goto_cell(cell_id: str) -> Dict[str, Any]:
    """Move to a specific cell by ID (e.g., A1, B2, C3)."""
    cell = cell_id.strip().upper()
    if not cell:
        raise HTTPException(status_code=400, detail="Missing cell ID")

    ctrl = MOTION
    if cell == "A1":
        await ctrl.home_x()
        await ctrl.home_y()
        return {"ok": True, "cell": cell, "pos": ctrl.current, "action": "homed_to_A1"}

    try:
        await ctrl.move_to_cell_xy(cell)
        return {"ok": True, "cell": cell, "pos": ctrl.current}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown cell: {cell}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/motion/move")
async def motion_move(payload: Dict[str, Any]) -> Dict[str, Any]:
    cell = str(payload.get("cell", "")).strip().upper()
    if not cell:
        raise HTTPException(status_code=400, detail="Missing 'cell'")

    ctrl = MOTION
    if cell == "A1":
        await ctrl.home_x()
        await ctrl.home_y()
        return {"ok": True, "cell": cell, "pos": ctrl.current, "action": "homed_to_A1"}

    try:
        await ctrl.move_to_cell_xy(cell)
        return {"ok": True, "cell": cell, "pos": ctrl.current}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - motion errors
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/motion/home_all")
async def motion_home_all() -> Dict[str, Any]:
    await MOTION.home_all()
    return {"ok": True, "pos": MOTION.current}


@app.post("/motion/home")
async def motion_home() -> Dict[str, Any]:
    """Home all axes (alias for home_all)."""
    await MOTION.home_all()
    return {"ok": True, "pos": MOTION.current}


@app.post("/motion/home_x")
async def motion_home_x() -> Dict[str, Any]:
    await MOTION.home_x()
    return {"ok": True, "pos": MOTION.current}


@app.post("/motion/home_y")
async def motion_home_y() -> Dict[str, Any]:
    await MOTION.home_y()
    return {"ok": True, "pos": MOTION.current}


@app.post("/motion/home_z")
async def motion_home_z() -> Dict[str, Any]:
    await MOTION.home_z()
    return {"ok": True, "pos": MOTION.current}


# New endpoint: home Z then extrude
@app.post("/motion/home_z_and_extrude")
async def motion_home_z_and_extrude() -> Dict[str, Any]:
    """
    Homes the Z-axis, extrudes 0.2mm, raises to 145mm, moves to assigned cell,
    lowers Z, retracts 0.2mm, raises Z, and returns to start position.
    """
    try:
        # Save starting position
        start_x = float(MOTION.current[0])
        start_y = float(MOTION.current[1])
        
        # 1. Home the Z-axis to Z=0
        LOG.info("Auto-sort: Homing Z-axis")
        await MOTION.home_z()

        # 2. Extrude 0.2mm at 50 mm/min to pick up card
        LOG.info("Auto-sort: Extruding 0.2mm to pick up card")
        await MOTION.driver.extrude(0.2, 50.0)

        # 3. Raise Z-axis to 145mm safe height with jerking motion to shake off extra cards
        # Pattern: +40mm, -20mm, +40mm, -20mm, +105mm = 145mm total
        LOG.info("Auto-sort: Raising to 145mm with shake pattern")
        await MOTION.driver.send_gcode('G0 Z40.0 F1000')   # Up 40mm to Z=40
        try:
            await _await_motion_completion(MOTION, (MOTION.current[0], MOTION.current[1], 40.0), tolerance=1.0, timeout=5.0)
        except Exception:
            await asyncio.sleep(1.0)  # Fallback wait
        
        await MOTION.driver.send_gcode('G0 Z20.0 F1000')   # Down 20mm to Z=20
        try:
            await _await_motion_completion(MOTION, (MOTION.current[0], MOTION.current[1], 20.0), tolerance=1.0, timeout=5.0)
        except Exception:
            await asyncio.sleep(1.0)
        
        await MOTION.driver.send_gcode('G0 Z60.0 F1000')   # Up 40mm to Z=60
        try:
            await _await_motion_completion(MOTION, (MOTION.current[0], MOTION.current[1], 60.0), tolerance=1.0, timeout=5.0)
        except Exception:
            await asyncio.sleep(1.0)
        
        await MOTION.driver.send_gcode('G0 Z40.0 F1000')   # Down 20mm to Z=40
        try:
            await _await_motion_completion(MOTION, (MOTION.current[0], MOTION.current[1], 40.0), tolerance=1.0, timeout=5.0)
        except Exception:
            await asyncio.sleep(1.0)
        
        await MOTION.driver.send_gcode('G0 Z145.0 F1000')  # Up 105mm to Z=145 (safe height)
        LOG.info("Auto-sort: Reached safe height Z=145mm")

        # Ensure the motion controller's cached position reflects the raised Z.
        try:
            await _await_motion_completion(MOTION, (MOTION.current[0], MOTION.current[1], 145.0), tolerance=1.0, timeout=6.0)
        except Exception:
            # Fallback: try a direct query; if that fails, conservatively set the Z to 145
            try:
                pos = await MOTION.driver.query_position()
                MOTION.current = (float(pos[0]), float(pos[1]), float(pos[2]))
            except Exception:
                MOTION.current = (MOTION.current[0], MOTION.current[1], 145.0)

        # 4. Read the assigned cell from scanned_cards.csv and move there
        moved_cell = None
        move_error = None
        try:
            csv_path = Path("data") / "scanned_cards.csv"
            if csv_path.exists():
                import csv as _csv

                with csv_path.open("r", encoding="utf8", newline="") as fh:
                    reader = _csv.DictReader(fh)
                    last_row = None
                    for row in reader:
                        last_row = row
                if last_row:
                    target = (last_row.get("assigned_cell") or "").strip().upper()
                    if target:
                        if target in CFG.cells:
                            try:
                                LOG.info("Auto-sort: Moving to cell %s", target)
                                
                                # Get cell position
                                pos = MOTION.cells.get(target) if hasattr(MOTION, 'cells') else CFG.cells.get(target)
                                if not isinstance(pos, dict):
                                    raise KeyError(f"Cell position for {target} missing or invalid: {pos}")
                                raw_x = pos.get('x')
                                raw_y = pos.get('y')
                                if raw_x is None or raw_y is None:
                                    raise KeyError(f"Cell position for {target} missing X/Y")
                                tx = float(raw_x)
                                ty = float(raw_y)

                                driver = MOTION.driver
                                # Current safe Z to preserve during XY travel
                                safe_z = float(MOTION.current[2]) if MOTION.current is not None else 0.0

                                if hasattr(driver, 'send_gcode'):
                                    # Use absolute positioning and send a G1 with only X and Y.
                                    try:
                                        await driver.send_gcode('G90')
                                    except Exception:
                                        pass
                                    feed = int(getattr(MOTION, 'rapid_speed', getattr(MOTION, 'default_speed', 800)))
                                    await driver.send_gcode(f'G1 X{tx:.3f} Y{ty:.3f} F{feed}')
                                    # Update cached controller position to reflect the XY move
                                    MOTION.current = (tx, ty, safe_z)
                                    try:
                                        await _await_motion_completion(MOTION, (tx, ty, safe_z), tolerance=1.0, timeout=6.0)
                                    except Exception:
                                        # Best-effort; don't fail the whole operation if completion wait fails
                                        pass
                                    
                                    # 5. Lower Z by 100mm after reaching the target cell to deposit card
                                    LOG.info("Auto-sort: Lowering Z by 100mm to deposit card")
                                    new_z = safe_z - 100.0
                                    await driver.send_gcode(f'G1 Z{new_z:.3f} F1000')
                                    MOTION.current = (tx, ty, new_z)
                                    try:
                                        await _await_motion_completion(MOTION, (tx, ty, new_z), tolerance=1.0, timeout=6.0)
                                    except Exception:
                                        pass
                                    
                                    # 6. Retract extruder by 0.2mm at 50 mm/min to release card
                                    LOG.info("Auto-sort: Retracting extruder 0.2mm to release card")
                                    await driver.extrude(-0.2, 50.0)
                                    
                                    # Small delay to ensure retraction completes
                                    await asyncio.sleep(0.5)
                                    
                                    # 7. Raise Z by 100mm back to safe height (145mm)
                                    # CRITICAL: Must complete before XY move to avoid collision
                                    LOG.info("Auto-sort: Raising Z back to safe height")
                                    await driver.send_gcode(f'G0 Z{safe_z:.3f} F1000')
                                    
                                    # Wait for Z raise with extended timeout and mandatory completion
                                    z_raised = False
                                    for attempt in range(3):
                                        try:
                                            await _await_motion_completion(MOTION, (tx, ty, safe_z), tolerance=1.0, timeout=10.0)
                                            MOTION.current = (tx, ty, safe_z)
                                            z_raised = True
                                            break
                                        except Exception as e:
                                            LOG.warning("Z raise attempt %d failed: %s", attempt + 1, e)
                                            if attempt < 2:
                                                await asyncio.sleep(0.5)
                                    
                                    if not z_raised:
                                        # Emergency: Force position update and add safety delay
                                        LOG.error("Z raise did not complete after retries! Adding emergency delay")
                                        await asyncio.sleep(3.0)  # Extra time for Z to complete
                                        MOTION.current = (tx, ty, safe_z)
                                    
                                    # 8. Return to starting position
                                    LOG.info("Auto-sort: Returning to start position (X=%.1f, Y=%.1f)", start_x, start_y)
                                    feed = int(getattr(MOTION, 'rapid_speed', getattr(MOTION, 'default_speed', 800)))
                                    await driver.send_gcode(f'G1 X{start_x:.3f} Y{start_y:.3f} F{feed}')
                                    MOTION.current = (start_x, start_y, safe_z)
                                    try:
                                        await _await_motion_completion(MOTION, (start_x, start_y, safe_z), tolerance=1.0, timeout=6.0)
                                    except Exception as e:
                                        LOG.warning("Return to start completion wait failed: %s", e)
                                        pass
                                else:
                                    # Fallback to controller helper which may include Z;
                                    # the controller's move_to_cell_xy should preserve Z.
                                    await MOTION.move_to_cell_xy(target)

                                moved_cell = target
                                LOG.info("Auto-sort: Successfully completed motion sequence for cell %s", target)
                            except Exception as mex:
                                move_error = str(mex)
                                LOG.warning("Failed to move to assigned cell %s: %s", target, mex, exc_info=True)
                        else:
                            LOG.debug("Assigned cell from CSV not in config: %s", target)
        except Exception as exc:
            LOG.warning("Failed to read scanned_cards.csv or move to assigned cell: %s", exc, exc_info=True)

        result = {"ok": True, "message": "Card pickup and delivery sequence completed", "pos": MOTION.current}
        if moved_cell:
            result["moved_to"] = moved_cell
        elif move_error:
            result["move_error"] = move_error
        return result
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        LOG.error("Auto-sort motion sequence failed: %s", exc, exc_info=True)
        # Return an error if any command fails
        return {"ok": False, "error": str(exc), "traceback": tb}


@app.post("/motion/home_xy")
async def motion_home_xy() -> Dict[str, Any]:
    await MOTION.home_x()
    await MOTION.home_y()
    return {"ok": True, "pos": MOTION.current}


@app.post("/motion/home_a1")
async def motion_home_a1() -> Dict[str, Any]:
    await MOTION.home_x()
    await MOTION.home_y()
    if 'A1' in MOTION.cells:
        await MOTION.move_to_cell_xy('A1')
    return {"ok": True, "pos": MOTION.current}


@app.post("/motion/jog")
async def motion_jog(payload: Dict[str, Any]) -> Dict[str, Any]:
    axis = str(payload.get('axis', '')).strip().upper()
    distance = float(payload.get('distance', 0.0))
    if axis not in {"X", "Y", "Z"}:
        raise HTTPException(status_code=400, detail="axis must be X, Y or Z")

    # Try jog, detect paused state, and unpause if needed
    try:
        await MOTION.jog_axis(axis, distance)
    except Exception as exc:
        # Check for paused for user in last response
        last_lines = getattr(MOTION.driver, 'last_response', None)
        if last_lines and any('paused for user' in ln.lower() for ln in last_lines):
            # Send unpause command (M108 is common, but may need to be changed for your hardware)
            try:
                await MOTION.driver.send_gcode('M108', wait_ok=True, timeout=2.0)
                await MOTION.jog_axis(axis, distance)
            except Exception as exc2:
                raise HTTPException(status_code=503, detail=f"Jog failed after unpause: {exc2}")
        else:
            raise HTTPException(status_code=503, detail=f"Jog failed: {exc}")
    return {"ok": True, "pos": MOTION.current}


@app.post("/motion/estop")
async def motion_estop() -> Dict[str, Any]:
    await MOTION.emergency_stop()
    return {"ok": True, "message": "Emergency stop activated"}


@app.post("/motion/reset_position")
async def motion_reset_position(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    x = float(payload.get('x', 0.0))
    y = float(payload.get('y', 0.0))
    z = float(payload.get('z', 0.0))
    await MOTION.reset_position(x, y, z)
    return {"ok": True, "position": [x, y, z]}


@app.post("/motion/set_current")
async def motion_set_current(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not payload:
        raise HTTPException(status_code=400, detail="Missing payload")

    cell = payload.get('cell')
    if cell:
        cell = str(cell).strip().upper()
        if cell not in CFG.cells:
            raise HTTPException(status_code=404, detail=f"Unknown cell {cell}")
        pos = CFG.cells[cell]
        x = float(getattr(pos, 'x', 0.0) or 0.0)
        y = float(getattr(pos, 'y', 0.0) or 0.0)
        z = float(getattr(pos, 'z', 0.0) or 0.0)
        MOTION.current = (x, y, z)
        return {"ok": True, "pos": MOTION.current}

    x = float(payload.get('x', MOTION.current[0]))
    y = float(payload.get('y', MOTION.current[1]))
    z = float(payload.get('z', MOTION.current[2]))
    MOTION.current = (x, y, z)
    return {"ok": True, "pos": MOTION.current}


    mode_request = (
        _normalize_mode_id(payload.get("sorting"))
        or _normalize_mode_id(payload.get("sort_mode"))
        or _normalize_mode_id(payload.get("mode"))
    )
    previous_mode = STATE.active_sort_mode
    activated_mode_override = False
    if mode_request:
        if mode_request not in CFG.sort_modes:
            raise HTTPException(status_code=404, detail=f"Unknown sort mode '{mode_request}'")
        STATE.active_sort_mode = mode_request
        activated_mode_override = True


# motion test/vacuum endpoints removed


@app.get("/motion/status")
async def motion_status() -> Dict[str, Any]:
    return await _motion_status_payload()


@app.post("/extruder/extrude")
async def extruder_extrude(request: Request) -> Dict[str, Any]:
    """Extrude a small amount to actuate an extruder/plunger.
    Expects JSON body optional {amount: float, feed: float}. Defaults to 0.2mm @ 50 mm/min.
    """
    payload = await request.json() if request is not None else {}
    amount = float(payload.get('amount', 0.2) if isinstance(payload, dict) else 0.2)
    feed = float(payload.get('feed', 50.0) if isinstance(payload, dict) else 50.0)
    try:
        await MOTION.driver.extrude(amount, feed)
        return {"ok": True, "amount": amount, "feed": feed}
    except Exception as exc:  # pragma: no cover - hardware specific
        LOG.exception("extruder_extrude failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/extruder/retract")
async def extruder_retract(request: Request) -> Dict[str, Any]:
    payload = await request.json() if request is not None else {}
    amount = float(payload.get('amount', 0.2) if isinstance(payload, dict) else 0.2)
    feed = float(payload.get('feed', 50.0) if isinstance(payload, dict) else 50.0)
    try:
        await MOTION.driver.extrude(-abs(amount), feed)
        return {"ok": True, "amount": -abs(amount), "feed": feed}
    except Exception as exc:  # pragma: no cover - hardware specific
        LOG.exception("extruder_retract failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/motion/z_drop_and_extrude")
async def motion_z_drop_and_extrude() -> Dict[str, Any]:
    try:
        await MOTION.jog('z', -10.0)
        await MOTION.driver.extrude(0.2, 50.0)
        return {"ok": True, "message": "Z dropped and extruded"}
    except Exception as exc:  # pragma: no cover - hardware specific
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/motion/detect")
def motion_detect() -> Dict[str, Any]:
    try:
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except Exception:
        return {"ports": [], "connected": not is_demo_mode(), "error": "pyserial not available"}

    ports = [{"device": p.device, "description": str(p.description)} for p in list_ports.comports()]
    port = getattr(MOTION.driver, 'port', 'virtual')
    matched = next((p for p in ports if p['device'] == port), None)
    return {"ports": ports, "connected": bool(matched) or is_demo_mode(), "matched": matched}


@app.post("/motion/calibrate")
async def motion_calibrate() -> Dict[str, Any]:
    await MOTION.home_all()
    return {"ok": True, "message": "Calibration complete", "pos": MOTION.current}


@app.post("/motion/save_a1_reference")
async def motion_save_a1_reference() -> Dict[str, Any]:
    result = await MOTION.save_a1_reference()
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Plunger / vacuum
# ---------------------------------------------------------------------------


@app.post("/plunger/down")
async def plunger_down() -> Dict[str, Any]:
    await MOTION.driver.plunger_down()
    return {"ok": True}


@app.post("/plunger/up")
async def plunger_up() -> Dict[str, Any]:
    await MOTION.driver.plunger_up()
    return {"ok": True}




# /vacuum endpoints removed


# ---------------------------------------------------------------------------
# G-code helpers
# ---------------------------------------------------------------------------


@app.post("/gcode/send")
async def gcode_send(payload: Dict[str, Any]) -> Dict[str, Any]:
    cmd = payload.get('cmd') if payload else None
    if not cmd or not isinstance(cmd, str):
        raise HTTPException(status_code=400, detail="Missing or invalid 'cmd'")
    if not hasattr(MOTION.driver, 'send_gcode'):
        raise HTTPException(status_code=400, detail="Current driver does not support raw G-code send")
    try:
        lines = await MOTION.driver.send_gcode(cmd)
        return {"ok": True, "cmd": cmd, "lines": lines}
    except Exception as exc:  # pragma: no cover - hardware/serial errors
        LOG.exception("gcode_send failed for cmd=%s: %s", cmd, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/gcode/mcodes")
def gcode_mcodes_get() -> Dict[str, Any]:
    mc = getattr(MOTION.driver, 'mcodes', None)
    return {"mcodes": mc}


@app.post("/gcode/mcodes")
def gcode_mcodes_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    mc = payload.get('mcodes') if payload else None
    if not isinstance(mc, dict):
        raise HTTPException(status_code=400, detail="Missing or invalid 'mcodes'")
    driver = MOTION.driver
    if not hasattr(driver, 'mcodes'):
        raise HTTPException(status_code=400, detail="Current driver does not support mcodes mapping")
    current = getattr(driver, 'mcodes', None)
    if not isinstance(current, dict):
        raise HTTPException(status_code=400, detail="Driver mcodes not mutable")
    current.update(mc)
    return {"ok": True, "mcodes": current}


# ---------------------------------------------------------------------------
# Run/session management
# ---------------------------------------------------------------------------


@app.post("/run/start")
def run_start(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = dict(payload or {})

    requested_mode = (
        _normalize_mode_id(meta.get("sorting"))
        or _normalize_mode_id(meta.get("sort_mode"))
        or _normalize_mode_id(meta.get("mode"))
    )
    if requested_mode:
        if requested_mode not in CFG.sort_modes:
            raise HTTPException(status_code=404, detail=f"Unknown sort mode '{requested_mode}'")
        STATE.active_sort_mode = requested_mode
    else:
        STATE.active_sort_mode = CFG.default_sort_mode

    requested_op = (
        _normalize_operation_id(meta.get("sort_operation"))
        or _normalize_operation_id(meta.get("operation"))
    )
    if requested_op:
        if requested_op not in CFG.sort_operations:
            raise HTTPException(status_code=404, detail=f"Unknown sort operation '{requested_op}'")
        STATE.active_sort_operation = requested_op
    else:
        STATE.active_sort_operation = CFG.default_sort_operation

    active_mode = STATE.active_sort_mode or CFG.default_sort_mode or DEFAULT_SORT_MODE
    mode_cfg = CFG.sort_modes.get(active_mode)
    meta["sorting"] = active_mode
    if mode_cfg:
        meta.setdefault("sort_mode_label", mode_cfg.label)
        meta.setdefault("sort_mode_type", mode_cfg.type)

    active_operation = STATE.active_sort_operation or CFG.default_sort_operation or DEFAULT_SORT_OPERATION
    if CFG.sort_operations:
        if active_operation not in CFG.sort_operations:
            active_operation = CFG.default_sort_operation or DEFAULT_SORT_OPERATION
    else:
        active_operation = None
    STATE.active_sort_operation = active_operation
    if active_operation:
        meta.setdefault("sort_operation", active_operation)

    try:
        session = SESSION.start_session(meta)
        return {"ok": True, "session": session}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/run/pause")
def run_pause() -> Dict[str, Any]:
    try:
        session = SESSION.update_state("Paused")
        return {"ok": True, "session": session}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/run/resume")
def run_resume() -> Dict[str, Any]:
    try:
        session = SESSION.update_state("Running")
        return {"ok": True, "session": session}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/run/end")
def run_end(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    notes = (payload or {}).get("notes") if payload else None
    try:
        session = SESSION.end_session(notes=notes)
        return {"ok": True, "session": session}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/run/status")
def run_status() -> Dict[str, Any]:
    status = SESSION.status()
    status.setdefault("state", "Idle")
    status.setdefault("throughput_cpm", 0.0)
    return status


@app.post("/run/divert_current")
def run_divert_current() -> Dict[str, Any]:
    entry = _append_error("manual_divert")
    return {"ok": True, "entry": entry}


# ---------------------------------------------------------------------------
# Error + log utilities
# ---------------------------------------------------------------------------


@app.get("/errors/export")
def errors_export() -> Any:
    if not ERROR_LOG:
        return {"ok": False, "message": "No errors recorded"}
    import csv

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=["id", "reason", "timestamp"])
    writer.writeheader()
    for item in ERROR_LOG:
        writer.writerow({
            "id": item.get("id"),
            "reason": item.get("reason"),
            "timestamp": item.get("timestamp"),
        })
    csv_bytes = buf.getvalue().encode("utf8")
    data = base64.b64encode(csv_bytes).decode("ascii")
    return f"data:text/csv;base64,{data}"


@app.post("/errors/clear")
def errors_clear() -> Dict[str, Any]:
    ERROR_LOG.clear()
    return {"ok": True}


@app.get("/logs/tail")
def logs_tail() -> Dict[str, Any]:
    lines = [f"[error] {item['timestamp']} {item['reason']}" for item in ERROR_LOG[-20:]]
    text = "\n".join(lines) if lines else "(no recent log entries)"
    return {"text": text}


# ---------------------------------------------------------------------------
# Camera endpoints
# ---------------------------------------------------------------------------


@app.get("/camera/devices")
def camera_devices(max_index: int = 10) -> Dict[str, Any]:
    """List all available camera devices with their capabilities."""
    devices = camera_svc.list_devices(max_index=max_index)
    LOG.info(f"Found {devices.get('count', 0)} available cameras: {devices.get('candidates', [])}")
    return devices


@app.post("/camera/select")
def camera_select(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Select and configure a camera device."""
    device = payload.get("device")
    if device is None:
        raise HTTPException(status_code=400, detail="Missing device")
    
    LOG.info(f"Attempting to configure camera device: {device}")
    try:
        camera_svc.configure({"device": device})
        info = camera_svc.get_manager().info()
        LOG.info(f"Successfully configured camera {device}: {info}")
        return {"ok": True, "device": device, "info": info}
    except Exception as e:
        LOG.error(f"Failed to configure camera {device}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to configure camera: {str(e)}")


@app.get("/camera/test/{device_id}")
def camera_test_device(device_id: str) -> Dict[str, Any]:
    """Test capture a frame from a specific device without changing configuration.
    
    Args:
        device_id: Can be numeric index like "0" or device path like "/dev/video0"
    
    Returns:
        Dictionary with frame info and base64 encoded preview image
    """
    # Parse device_id - convert to int if numeric, otherwise use as string
    try:
        device = int(device_id)
    except ValueError:
        device = device_id
    
    LOG.info(f"Testing camera device: {device}")
    
    try:
        # Try to open the device temporarily
        cap = cv2.VideoCapture(device)
        if not cap or not cap.isOpened():
            LOG.warning(f"Failed to open camera device {device}")
            return {
                "ok": False,
                "device": device,
                "error": "Failed to open device"
            }
        
        # Try to capture a frame
        ret, frame = cap.read()
        cap.release()
        
        if not ret or frame is None:
            LOG.warning(f"Failed to capture frame from device {device}")
            return {
                "ok": False,
                "device": device,
                "error": "Failed to capture frame"
            }
        
        # Get frame info
        height, width = frame.shape[:2]
        
        # Create a small preview (max 400px width)
        scale = min(1.0, 400.0 / width)
        preview_width = int(width * scale)
        preview_height = int(height * scale)
        preview = cv2.resize(frame, (preview_width, preview_height))
        
        # Encode as JPEG
        _, buffer = cv2.imencode('.jpg', preview, [cv2.IMWRITE_JPEG_QUALITY, 85])
        preview_b64 = base64.b64encode(buffer).decode('utf-8')
        
        LOG.info(f"Successfully captured test frame from {device}: {width}x{height}")
        
        return {
            "ok": True,
            "device": device,
            "resolution": f"{width}x{height}",
            "preview": f"data:image/jpeg;base64,{preview_b64}"
        }
        
    except Exception as e:
        LOG.error(f"Error testing camera device {device}: {e}")
        return {
            "ok": False,
            "device": device,
            "error": str(e)
        }


@app.get("/camera/status")
def camera_status() -> Dict[str, Any]:
    info = camera_svc.get_manager().info(ensure_capture=True)
    device = info.get("device")
    path = str(device) if device is not None else "unknown"
    if isinstance(device, int):
        path = f"/dev/video{device}"
    return {
        "device": device,
        "path": path,
        "online": info.get("online"),
        "resolution": info.get("resolution"),
        "fps": info.get("fps"),
        "last_frame_ts": info.get("last_frame_ts"),
        "error": info.get("error"),
    }


def _simple_ocr_pipeline(frame: np.ndarray) -> Dict[str, Any]:
    """Simple OCR pipeline for test photos: rotate, align, and OCR."""
    
    # Step 1: Rotate the image to portrait orientation if needed
    height, width = frame.shape[:2]
    rotated = frame
    rotation_applied = "none"
    
    if width > height:
        # Image is landscape, rotate 90 degrees counter-clockwise
        rotated = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        rotation_applied = "90_ccw"
        LOG.info("Rotated image 90° counter-clockwise: %dx%d -> %dx%d", width, height, rotated.shape[1], rotated.shape[0])
    
    # Step 2: Detect and align the card border
    # Create a dummy mask for single frame processing
    dummy_mask = np.ones((rotated.shape[0], rotated.shape[1]), dtype=np.uint8) * 255
    
    # First, visualize the border detection
    processed = ocr_pipeline._enhance_for_border_detection(rotated)
    edges = cv2.Canny(processed, 40, 160)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, kernel, iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    # Draw the detected border on the rotated image
    rotated_with_border = rotated.copy()
    if contours:
        contour = max(contours, key=cv2.contourArea)
        rect = cv2.minAreaRect(contour)
        box = cv2.boxPoints(rect)
        box = box.astype(np.int32)
        cv2.drawContours(rotated_with_border, [box], 0, (0, 255, 0), 3)
        LOG.info("Detected border with %d contours, largest has area %.1f", len(contours), cv2.contourArea(contour))
    else:
        LOG.warning("No contours found for border detection")
    
    # Use border_margin_px=0 to crop tightly to the card edge
    card_aligned, card_mask, border_meta = ocr_pipeline._warp_card_to_bounds(
        rotated, dummy_mask, border_margin_px=0
    )
    
    LOG.info("Card alignment: found=%s, area=%.1f", border_meta.get("found"), border_meta.get("area", 0))
    
    # Step 3: Define card regions on aligned card
    # Assuming card_aligned is now in portrait orientation and properly cropped
    h, w = card_aligned.shape[:2]
    
    # Card layout regions:
    # - Title: top 15% of card (0-15%)
    # - Type line: 10% section above rules (53-63%)
    # - Rules text: 30% section above collector (63-93%)
    # - Collector/Artist: bottom 7% (93-100%)
    
    regions = {
        "title": (0, int(h * 0.15)),                    # Top 15%
        "type_line": (int(h * 0.53), int(h * 0.63)),    # 53-63% (10% section)
        "rules": (int(h * 0.63), int(h * 0.93)),        # 63-93% (30% section)
        "collector": (int(h * 0.93), h),                # Bottom 7% (93-100%)
    }
    
    LOG.info("Card regions (h=%d): title=%s, type_line=%s, rules=%s, collector=%s", 
             h, regions["title"], regions["type_line"], regions["rules"], regions["collector"])
    
    # Step 4: Prepare aligned card for OCR (enhance, etc.)
    ocr_ready, ocr_meta = ocr_pipeline._prepare_for_ocr(card_aligned)
    
    # Step 5: Perform OCR on each region
    ocr_text_map = {}
    ocr_results_by_region = {}
    
    for region_name, (start_y, end_y) in regions.items():
        # Extract region from OCR-ready image
        region_img = ocr_ready[start_y:end_y, :]
        
        if region_img.size == 0:
            continue
        
        # Perform OCR on this region
        region_ocr_map, region_ocr_meta = ocr_pipeline._perform_ocr(region_img, region_hints=None)
        
        # Store the full text from this region
        ocr_text_map[region_name] = region_ocr_map.get("full", "")
        ocr_results_by_region[region_name] = {
            "text": region_ocr_map.get("full", ""),
            "meta": region_ocr_meta,
            "bounds": (start_y, end_y),
        }
    
    # Step 6: Build combined OCR meta
    combined_ocr_meta = {
        "engine": ocr_results_by_region.get("title", {}).get("meta", {}).get("engine", "unknown"),
        "regions": regions,
        "by_region": ocr_results_by_region,
        "border": border_meta,
    }
    
    return {
        "original": frame,
        "rotated": rotated,
        "rotated_with_border": rotated_with_border,
        "edges": edges,
        "aligned": card_aligned,
        "ocr_ready": ocr_ready,
        "ocr_text": ocr_text_map,
        "ocr_meta": combined_ocr_meta,
        "rotation_applied": rotation_applied,
        "regions": regions,
    }


@app.get("/camera/test_photo")
async def camera_test_photo() -> Dict[str, Any]:
    """Capture a single test photo with simple OCR pipeline."""
    cam = camera_svc.get_manager()
    loop = asyncio.get_running_loop()
    
    # Discard a few buffered frames and capture a fresh one
    for _ in range(5):
        await loop.run_in_executor(None, cam.grab_frame_sync, 0.0)
        await asyncio.sleep(0.05)
    
    frame = await loop.run_in_executor(None, cam.grab_frame_sync, 0.0)
    
    if frame is None:
        raise HTTPException(status_code=503, detail="Failed to capture frame")
    
    # Run simple OCR pipeline
    result = _simple_ocr_pipeline(frame)
    
    # Encode original frame as JPEG
    _, buffer = cv2.imencode('.jpg', result["original"], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    img_bytes = buffer.tobytes()
    encoded_image = base64.b64encode(img_bytes).decode('ascii')
    
    # Encode rotated frame as JPEG
    _, rot_buffer = cv2.imencode('.jpg', result["rotated"], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    rot_bytes = rot_buffer.tobytes()
    encoded_rotated = base64.b64encode(rot_bytes).decode('ascii')
    
    # Encode rotated frame with border detection as JPEG
    _, border_buffer = cv2.imencode('.jpg', result["rotated_with_border"], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    border_bytes = border_buffer.tobytes()
    encoded_border = base64.b64encode(border_bytes).decode('ascii')
    
    # Encode edge detection as PNG
    _, edges_buffer = cv2.imencode('.png', result["edges"])
    edges_bytes = edges_buffer.tobytes()
    encoded_edges = base64.b64encode(edges_bytes).decode('ascii')
    
    # Encode aligned card as JPEG
    _, aligned_buffer = cv2.imencode('.jpg', result["aligned"], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    aligned_bytes = aligned_buffer.tobytes()
    encoded_aligned = base64.b64encode(aligned_bytes).decode('ascii')
    
    # Encode OCR-prepared image as PNG
    _, ocr_buffer = cv2.imencode('.png', result["ocr_ready"])
    ocr_bytes = ocr_buffer.tobytes()
    encoded_ocr = base64.b64encode(ocr_bytes).decode('ascii')
    
    return {
        "success": True,
        "shape": list(frame.shape),
        "size_bytes": len(img_bytes),
        "image": encoded_image,
        "mime": "image/jpeg",
        "rotated_image": encoded_rotated,
        "rotated_shape": list(result["rotated"].shape),
        "border_detection_image": encoded_border,
        "edges_image": encoded_edges,
        "aligned_image": encoded_aligned,
        "aligned_shape": list(result["aligned"].shape),
        "rotation_applied": result["rotation_applied"],
        "ocr_image": encoded_ocr,
        "ocr_mime": "image/png",
        "ocr_text": result["ocr_text"],
        "ocr_meta": result["ocr_meta"],
        "regions": result["regions"],
    }


@app.get("/camera/snapshot")
async def camera_snapshot(
    quality: int = 80,
    max_age: float = 0.0,
) -> Dict[str, Any]:
    """Capture a single snapshot for card identification (no dual-frame compositing)."""
    cam = camera_svc.get_manager()
    
    # Check camera status first
    camera_info = cam.info(ensure_capture=True)
    if not camera_info.get("online"):
        error_detail = camera_info.get("error", "Camera not available")
        LOG.error(f"Camera snapshot failed: {error_detail}")
        raise HTTPException(
            status_code=503,
            detail=f"Camera not available: {error_detail}"
        )
    
    ctrl = motion.get_controller()
    loop = asyncio.get_running_loop()

    async def _capture_fresh_frame(
        *,
        max_age_override: float,
        discard: int = 2,
        settle: float = 0.05,
    ) -> np.ndarray:
        # Discard buffered frames to ensure we get a fresh capture
        for _ in range(max(0, discard)):
            await loop.run_in_executor(None, cam.grab_frame_sync, 0.0)
            if settle > 0:
                await asyncio.sleep(settle)
        # Final capture - always use max_age=0.0 to force a fresh read
        frame = cast(np.ndarray, await loop.run_in_executor(None, cam.grab_frame_sync, 0.0))
        return frame

    async with ctrl.lock:
        commanded_start: Tuple[float, float, float] = (
            float(ctrl.current[0]),
            float(ctrl.current[1]),
            float(ctrl.current[2]),
        )

        try:
            measured_start = await _await_motion_completion(ctrl, None, tolerance=0.3, timeout=6.0)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        ctrl.current = measured_start
        
        # CRITICAL SAFETY CHECK: Ensure axes are homed before any motion
        if not ctrl.homed:
            LOG.error("Snapshot request rejected: System not homed")
            raise HTTPException(
                status_code=400,
                detail="Cannot take snapshot: System not homed. Home all axes first for safety (use Home All button)."
            )
        
        # SAFETY CHECK: Ensure Z-axis is at a safe height before moving Y
        # Prevent collisions by requiring Z >= 100mm
        if measured_start[2] < 100.0:
            LOG.warning(f"Z-axis too low for snapshot ({measured_start[2]:.1f}mm). Raising to safe height...")
            try:
                # Raise Z to safe height (145mm) before any XY movement
                await ctrl.driver.send_gcode('G0 Z145.0 F1000')
                await _await_motion_completion(ctrl, (measured_start[0], measured_start[1], 145.0), tolerance=1.0, timeout=8.0)
                measured_start = (measured_start[0], measured_start[1], 145.0)
                ctrl.current = measured_start
                LOG.info("Z-axis raised to safe height: 145mm")
            except Exception as z_raise_exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Failed to raise Z to safe height: {z_raise_exc}"
                )
        
        # SAFETY CHECK: Ensure Y position is reasonable
        # If Y > 100, something is wrong - probably position tracking issue
        if measured_start[1] > 100.0:
            LOG.error(f"Unsafe Y position detected: {measured_start[1]:.1f}mm - resetting to 0")
            # Try to recover by moving back to Y=0
            try:
                await ctrl.driver.move_absolute(measured_start[0], 0.0, measured_start[2], ctrl.default_speed)
                await _await_motion_completion(ctrl, (measured_start[0], 0.0, measured_start[2]), tolerance=0.5, timeout=6.0)
                measured_start = (measured_start[0], 0.0, measured_start[2])
                ctrl.current = measured_start
            except Exception as recovery_exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Unsafe Y position ({measured_start[1]:.1f}mm) and recovery failed: {recovery_exc}"
                )

        try:
            await ctrl.driver.set_speed(ctrl.default_speed)
            
            # Move Y-axis by 55mm for photo capture
            photo_y = measured_start[1] + 55.0
            LOG.info(f"SNAPSHOT MOVEMENT: Starting position: X={measured_start[0]:.3f}, Y={measured_start[1]:.3f}, Z={measured_start[2]:.3f}")
            LOG.info(f"SNAPSHOT MOVEMENT: Moving to: X={measured_start[0]:.3f}, Y={photo_y:.3f}, Z={measured_start[2]:.3f} (Y+55mm)")
            await ctrl.driver.move_absolute(measured_start[0], photo_y, measured_start[2], ctrl.default_speed)
            ctrl.current = (measured_start[0], photo_y, measured_start[2])
            
            # Wait for motion to complete
            await _await_motion_completion(ctrl, (measured_start[0], photo_y, measured_start[2]), tolerance=0.5, timeout=6.0)
            
            # Wait for camera to stabilize after movement (autofocus, auto-exposure, vibration)
            await asyncio.sleep(3.0)
            
            # Capture single frame at offset position
            frame = await _capture_fresh_frame(max_age_override=0.0, discard=5, settle=0.05)
            
            # Return to original position
            await ctrl.driver.move_absolute(measured_start[0], measured_start[1], measured_start[2], ctrl.default_speed)
            ctrl.current = measured_start
        except Exception as exc:
            # Ensure we return to original position even on error
            try:
                await ctrl.driver.move_absolute(measured_start[0], measured_start[1], measured_start[2], ctrl.default_speed)
                ctrl.current = measured_start
            except Exception:
                pass
            raise HTTPException(status_code=503, detail=f"Camera snapshot failed: {exc}")

    if frame is None:
        raise HTTPException(status_code=503, detail="Snapshot frame unavailable")

    timestamp_dt = datetime.utcnow()
    timestamp_slug = timestamp_dt.strftime("%Y%m%d-%H%M%S-%f")
    timestamp_iso = timestamp_dt.isoformat(timespec="milliseconds") + "Z"
    snapshot_dir = Path("data") / "snapshots"

    # Clear existing snapshots to keep only the latest capture
    try:
        if snapshot_dir.exists():
            for child in snapshot_dir.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except Exception:
                    LOG.debug("Failed to remove snapshot item %s", child, exc_info=True)
    except Exception:
        LOG.warning("Could not clear snapshots directory: %s", snapshot_dir, exc_info=True)

    # Process single frame (no compositing)
    artifacts = ocr_pipeline.prepare_single_snapshot_artifacts(
        frame,
        timestamp_slug=timestamp_slug,
        save_dir=snapshot_dir,
        jpeg_quality=quality,
        persist=True,
        include_bytes=True,
    )

    labels = [
        "rotated",
        "aligned",
        "flipped",
        "ocr_prepared",
        "ocr_text",
        "original",
    ]
    frames: List[Dict[str, Any]] = []
    for label in labels:
        asset = artifacts.get(label)
        if not asset:
            continue
        frame_info: Dict[str, Any] = {
            "label": label,
            "mime": asset.get("mime", "image/jpeg"),
            "path": asset.get("path", ""),
            "shape": asset.get("shape"),
        }
        if "bytes" in asset:
            encoded_image = base64.b64encode(asset["bytes"]).decode("ascii")
            frame_info["image"] = encoded_image
            frame_info["size"] = len(asset["bytes"])
        if "text" in asset:
            frame_info["text"] = asset.get("text")
        if "meta" in asset:
            frame_info["meta"] = asset["meta"]
        frames.append(frame_info)

    frame_map = {entry["label"]: entry for entry in frames}

    for label in labels:
        if label in artifacts:
            artifacts[label].pop("bytes", None)

    saved_paths = {
        "original_path": artifacts.get("original", {}).get("path", ""),
        "ocr_path": artifacts.get("ocr_prepared", {}).get("path", ""),
        "meta_path": artifacts.get("meta", {}).get("path", ""),
    }

    meta_block = dict(artifacts.get("meta", {}))
    processing_meta = meta_block

    ocr_summary = None
    ocr_frame = frame_map.get("ocr_text") if isinstance(frame_map, dict) else None
    if isinstance(ocr_frame, dict):
        ocr_payload = ocr_frame.get("text")
        if isinstance(ocr_payload, dict):
            for key in ("full_text", "full", "oracle", "name"):
                text_val = ocr_payload.get(key)
                if isinstance(text_val, str) and text_val.strip():
                    ocr_summary = text_val.strip()
                    break
    if ocr_summary and len(ocr_summary) > 160:
        ocr_summary = ocr_summary[:157] + "…"

    meta_path = saved_paths.get("meta_path")
    if meta_path:
        LOG.info("Snapshot %s OCR saved to %s%s", timestamp_iso, meta_path, f" — {ocr_summary}" if ocr_summary else "")
    elif ocr_summary:
        LOG.info("Snapshot %s OCR: %s", timestamp_iso, ocr_summary)

    # Persist a small file with the best identification match's scryfall id
    try:
        # Use lightweight identification instead of embeddings
        identification_info = artifacts.get("meta", {}).get("identification") if isinstance(artifacts.get("meta", {}), dict) else None
        zone_ocr_info = artifacts.get("meta", {}).get("zone_ocr") if isinstance(artifacts.get("meta", {}), dict) else None
        scry_id = None
        assigned_cell = None
        assignment_reason = None
        identification_warning = None
        
        # Check for rejected identifications
        if zone_ocr_info:
            selected_orientation = zone_ocr_info.get("selected_orientation")
            if selected_orientation in ("low_confidence", "orientation_ambiguous", "no_valid_ocr"):
                identification_warning = {
                    "low_confidence": "Identification rejected: confidence too low",
                    "orientation_ambiguous": "Identification rejected: orientation unclear",
                    "no_valid_ocr": "Identification rejected: no readable text",
                }.get(selected_orientation, "Identification failed")
                LOG.warning(identification_warning)
        
        if isinstance(identification_info, dict):
            best = identification_info.get("best")
            score = identification_info.get("score", 0.0)
            
            # Warn if score is marginal
            if score < 60.0 and score > 0:
                identification_warning = f"Low identification confidence: {score:.1f}%"
                LOG.warning(identification_warning)
            
            if isinstance(best, dict):
                # FTS identification stores id or scryfall_id in best
                scry_id = best.get("id") or best.get("scryfall_id")
                
                # Create a Card object and get cell assignment
                try:
                    card = Card(
                        game="magic",
                        scryfall_id=scry_id or "",
                        name=best.get("name", ""),
                        set_code=best.get("set", ""),
                        collector_number=best.get("collector_number", ""),
                        confidence=identification_info.get("score", 0.0) / 100.0,  # Convert score to 0-1 confidence
                    )
                    LOG.info("Attempting to assign card: %s (scryfall_id=%s, confidence=%.2f)", 
                             card.name, scry_id, card.confidence)
                    assigned_cell, assignment_reason = assign_card(card, CFG, STATE)
                    LOG.info("Card %s assigned to cell %s (%s)", card.name, assigned_cell, assignment_reason)
                except Exception as exc:
                    LOG.error("Failed to assign card to cell: %s", exc, exc_info=True)
                    # Set assignment warning so frontend knows there was an issue
                    if not identification_warning:
                        identification_warning = f"Assignment failed: {str(exc)}"
        
        # Log final assignment state for debugging
        LOG.info("Final assignment state: cell=%s, reason=%s, warning=%s", 
                 assigned_cell, assignment_reason, identification_warning)
                    
        last_path = snapshot_dir / "last_snapshot.json"
        # Write just the scryfall id as a JSON string (or null)
        last_path.write_text(json.dumps(scry_id), encoding="utf8")
    except Exception:
        LOG.warning("Failed to write last_snapshot.json", exc_info=True)

    # Always include assignment in response (even if None) for frontend clarity
    assignment_data = {
        "cell": assigned_cell,
        "reason": assignment_reason,
        "warning": identification_warning,
    } if (assigned_cell or identification_warning) else None
    
    LOG.info("Creating assignment_data: %s (assigned_cell=%s, warning=%s)", 
             assignment_data, assigned_cell, identification_warning)

    response_data = {
        "timestamp": timestamp_iso,
        "frames": frames,
        "images": frame_map,
        "saved": saved_paths,
        "processing": processing_meta,
        "assignment": assignment_data,
    }
    
    LOG.info("Snapshot response includes assignment: %s", bool(response_data.get("assignment")))
    if response_data.get("assignment"):
        LOG.info("Assignment details: cell=%s", response_data["assignment"].get("cell"))
    
    return response_data


@app.post("/camera/dual_snapshot")
async def camera_dual_snapshot(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    offset = 44.0
    if payload and "offset_mm" in payload:
        try:
            offset = float(payload["offset_mm"])
        except Exception:
            raise HTTPException(status_code=400, detail="offset_mm must be numeric")
    return await ocr_pipeline.capture_dual_snapshot(offset_mm=offset)


@app.get("/debug/alpha_map")
def alpha_map() -> Dict[str, Any]:
    return {"letter_to_cell": CFG.letter_to_cell}


@app.get("/debug/preview")
async def debug_preview():
    """Debug preview page showing image processing stages."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Card Processing Debug Preview</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #1a1a1a;
            color: #fff;
        }
        h1 { color: #4a9eff; }
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        .controls {
            background: #2a2a2a;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        button {
            background: #4a9eff;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
        }
        button:hover { background: #3a8eef; }
        button:disabled {
            background: #555;
            cursor: not-allowed;
        }
        .status {
            margin: 10px 0;
            padding: 10px;
            background: #333;
            border-radius: 4px;
        }
        .frames-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .frame-card {
            background: #2a2a2a;
            border-radius: 8px;
            padding: 15px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .frame-card h3 {
            margin-top: 0;
            color: #4a9eff;
            border-bottom: 2px solid #4a9eff;
            padding-bottom: 10px;
        }
        .frame-image {
            width: 100%;
            height: auto;
            border: 2px solid #444;
            border-radius: 4px;
            background: #000;
        }
        .frame-info {
            margin-top: 10px;
            font-size: 14px;
            color: #aaa;
        }
        .frame-info div {
            margin: 5px 0;
        }
        .ocr-text {
            background: #1a1a1a;
            padding: 10px;
            border-radius: 4px;
            margin-top: 10px;
            font-family: monospace;
            font-size: 12px;
            max-height: 200px;
            overflow-y: auto;
        }
        .identification {
            background: #1a3a1a;
            padding: 10px;
            border-radius: 4px;
            margin-top: 10px;
        }
        .identification h4 {
            margin: 0 0 10px 0;
            color: #6fff6f;
        }
        .no-match {
            background: #3a1a1a;
            color: #ff6f6f;
        }
        .metadata {
            font-size: 12px;
            color: #888;
            margin-top: 10px;
            padding: 10px;
            background: #1e1e1e;
            border-radius: 4px;
        }
        .card-match-banner {
            background: linear-gradient(135deg, #1a3a1a 0%, #2a4a2a 100%);
            border: 3px solid #4aff4a;
            border-radius: 12px;
            padding: 25px;
            margin-bottom: 30px;
            box-shadow: 0 6px 12px rgba(0,0,0,0.5);
        }
        .card-match-banner h2 {
            margin: 0 0 15px 0;
            color: #4aff4a;
            font-size: 28px;
            text-align: center;
        }
        .card-match-content {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        .card-match-info {
            flex: 1;
        }
        .card-name {
            font-size: 24px;
            font-weight: bold;
            color: #fff;
            margin-bottom: 10px;
        }
        .card-details {
            font-size: 16px;
            color: #ccc;
            margin: 5px 0;
        }
        .match-score {
            font-size: 20px;
            color: #4aff4a;
            font-weight: bold;
            margin-top: 10px;
        }
        .cell-assignment {
            font-size: 20px;
            color: #ff9944;
            font-weight: bold;
            margin-top: 15px;
            padding: 10px;
            background: rgba(255, 153, 68, 0.1);
            border-left: 4px solid #ff9944;
            border-radius: 4px;
        }
        .orientation-badge {
            display: inline-block;
            background: #4a9eff;
            color: white;
            padding: 4px 12px;
            border-radius: 6px;
            font-size: 14px;
            margin-left: 10px;
        }
        .no-match-banner {
            background: linear-gradient(135deg, #3a1a1a 0%, #4a2a2a 100%);
            border: 3px solid #ff4a4a;
        }
        .no-match-banner h2 {
            color: #ff4a4a;
        }
            padding: 10px;
            background: #1a1a1a;
            border-radius: 4px;
        }
        .metadata pre {
            margin: 5px 0;
            overflow-x: auto;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Card Processing Debug Preview</h1>
        
        <div class="controls">
            <button onclick="captureSnapshot()" id="captureBtn">Capture Snapshot</button>
            <div class="status" id="status">Ready to capture</div>
        </div>

        <div id="results"></div>
    </div>

    <script>
        async function captureSnapshot() {
            const btn = document.getElementById('captureBtn');
            const status = document.getElementById('status');
            const results = document.getElementById('results');
            
            btn.disabled = true;
            status.textContent = 'Capturing snapshot...';
            results.innerHTML = '';
            
            try {
                const response = await fetch('/camera/snapshot');
                const data = await response.json();
                
                status.textContent = `Captured at ${data.timestamp}`;
                
                // Display card identification banner at the top
                const ocrTextFrame = data.frames.find(f => f.label === 'ocr_text');
                if (ocrTextFrame && ocrTextFrame.meta) {
                    const identification = ocrTextFrame.meta.identification;
                    const selectedOrientation = ocrTextFrame.meta.selected_orientation;
                    
                    if (identification && identification.best) {
                        const banner = document.createElement('div');
                        banner.className = 'card-match-banner';
                        
                        const title = document.createElement('h2');
                        title.innerHTML = '✓ Card Identified';
                        if (selectedOrientation) {
                            const badge = document.createElement('span');
                            badge.className = 'orientation-badge';
                            badge.textContent = selectedOrientation === 'normal' ? 'Normal Orientation' : '180° Rotated';
                            title.appendChild(badge);
                        }
                        banner.appendChild(title);
                        
                        const content = document.createElement('div');
                        content.className = 'card-match-content';
                        
                        const info = document.createElement('div');
                        info.className = 'card-match-info';
                        
                        const cardName = document.createElement('div');
                        cardName.className = 'card-name';
                        cardName.textContent = identification.best.name || 'Unknown Card';
                        info.appendChild(cardName);
                        
                        if (identification.best.set_name || identification.best.set) {
                            const setInfo = document.createElement('div');
                            setInfo.className = 'card-details';
                            setInfo.textContent = `Set: ${identification.best.set_name || identification.best.set || 'Unknown'}`;
                            if (identification.best.collector_number) {
                                setInfo.textContent += ` (#${identification.best.collector_number})`;
                            }
                            info.appendChild(setInfo);
                        }
                        
                        if (identification.best.type_line) {
                            const typeInfo = document.createElement('div');
                            typeInfo.className = 'card-details';
                            typeInfo.textContent = identification.best.type_line;
                            info.appendChild(typeInfo);
                        }
                        
                        const scoreDiv = document.createElement('div');
                        scoreDiv.className = 'match-score';
                        scoreDiv.textContent = `Match Score: ${identification.score.toFixed(1)}%`;
                        info.appendChild(scoreDiv);
                        
                        // Display cell assignment if available
                        if (data.assignment && data.assignment.cell) {
                            const cellDiv = document.createElement('div');
                            cellDiv.className = 'cell-assignment';
                            cellDiv.innerHTML = `<strong>📦 Destination:</strong> Cell ${data.assignment.cell}`;
                            if (data.assignment.reason) {
                                cellDiv.innerHTML += ` <span style="color: #888;">(${data.assignment.reason})</span>`;
                            }
                            info.appendChild(cellDiv);
                        }
                        
                        content.appendChild(info);
                        banner.appendChild(content);
                        results.appendChild(banner);
                    } else {
                        const banner = document.createElement('div');
                        banner.className = 'card-match-banner no-match-banner';
                        const title = document.createElement('h2');
                        title.textContent = '✗ No Card Match Found';
                        banner.appendChild(title);
                        results.appendChild(banner);
                    }
                }
                
                // Display frames in order
                const frameOrder = ['original', 'rotated', 'aligned', 'flipped', 'ocr_prepared', 'ocr_text'];
                const framesGrid = document.createElement('div');
                framesGrid.className = 'frames-grid';
                
                for (const label of frameOrder) {
                    const frame = data.frames.find(f => f.label === label);
                    if (!frame) continue;
                    
                    const card = document.createElement('div');
                    card.className = 'frame-card';
                    
                    const title = document.createElement('h3');
                    title.textContent = `${label.toUpperCase().replace('_', ' ')}`;
                    card.appendChild(title);
                    
                    if (frame.image) {
                        const img = document.createElement('img');
                        img.className = 'frame-image';
                        img.src = `data:${frame.mime};base64,${frame.image}`;
                        card.appendChild(img);
                    }
                    
                    const info = document.createElement('div');
                    info.className = 'frame-info';
                    
                    if (frame.shape) {
                        const shapeDiv = document.createElement('div');
                        shapeDiv.innerHTML = `<strong>Shape:</strong> ${frame.shape[1]}×${frame.shape[0]} (W×H)`;
                        info.appendChild(shapeDiv);
                        
                        const orientDiv = document.createElement('div');
                        const orientation = frame.shape[1] > frame.shape[0] ? 'LANDSCAPE' : 'PORTRAIT';
                        orientDiv.innerHTML = `<strong>Orientation:</strong> ${orientation}`;
                        info.appendChild(orientDiv);
                    }
                    
                    if (frame.size) {
                        const sizeDiv = document.createElement('div');
                        sizeDiv.innerHTML = `<strong>Size:</strong> ${(frame.size / 1024).toFixed(1)} KB`;
                        info.appendChild(sizeDiv);
                    }
                    
                    if (frame.meta) {
                        const metaDiv = document.createElement('div');
                        metaDiv.className = 'metadata';
                        metaDiv.innerHTML = `<strong>Metadata:</strong><pre>${JSON.stringify(frame.meta, null, 2)}</pre>`;
                        info.appendChild(metaDiv);
                    }
                    
                    if (frame.text) {
                        const textDiv = document.createElement('div');
                        textDiv.className = 'ocr-text';
                        textDiv.innerHTML = `<strong>OCR Text:</strong><pre>${JSON.stringify(frame.text, null, 2)}</pre>`;
                        info.appendChild(textDiv);
                    }
                    
                    card.appendChild(info);
                    framesGrid.appendChild(card);
                }
                
                results.appendChild(framesGrid);
                
                // Display identification results
                if (data.processing && data.processing.identification) {
                    const ident = data.processing.identification;
                    const identCard = document.createElement('div');
                    identCard.className = 'frame-card';
                    
                    const identTitle = document.createElement('h3');
                    identTitle.textContent = 'CARD IDENTIFICATION';
                    identCard.appendChild(identTitle);
                    
                    if (ident.best) {
                        const identDiv = document.createElement('div');
                        identDiv.className = 'identification';
                        identDiv.innerHTML = `
                            <h4>✓ Match Found</h4>
                            <div><strong>Name:</strong> ${ident.best.name}</div>
                            <div><strong>Score:</strong> ${(ident.score || 0).toFixed(2)}</div>
                            <div><strong>Scryfall ID:</strong> ${ident.best.scryfall_id || 'N/A'}</div>
                            <div><strong>Set:</strong> ${ident.best.set || 'N/A'}</div>
                            <div><strong>Collector:</strong> ${ident.best.collector_number || 'N/A'}</div>
                        `;
                        identCard.appendChild(identDiv);
                    } else {
                        const noMatch = document.createElement('div');
                        noMatch.className = 'identification no-match';
                        noMatch.innerHTML = '<h4>✗ No Match Found</h4>';
                        identCard.appendChild(noMatch);
                    }
                    
                    results.appendChild(identCard);
                }
                
                // Display OCR regions metadata
                if (data.processing && data.processing.ocr_meta && data.processing.ocr_meta.region_rows) {
                    const regionsCard = document.createElement('div');
                    regionsCard.className = 'frame-card';
                    
                    const regionsTitle = document.createElement('h3');
                    regionsTitle.textContent = 'OCR REGION ROWS';
                    regionsCard.appendChild(regionsTitle);
                    
                    const regionsDiv = document.createElement('div');
                    regionsDiv.className = 'metadata';
                    
                    const regions = data.processing.ocr_meta.region_rows;
                    for (const [region, rows] of Object.entries(regions)) {
                        const height = rows[1] - rows[0];
                        regionsDiv.innerHTML += `<div><strong>${region}:</strong> rows ${rows[0]}-${rows[1]} (height: ${height}px)</div>`;
                    }
                    
                    regionsCard.appendChild(regionsDiv);
                    results.appendChild(regionsCard);
                }
                
            } catch (error) {
                status.textContent = `Error: ${error.message}`;
                console.error(error);
            } finally {
                btn.disabled = false;
            }
        }
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


@app.post("/debug/reset_counts")
def reset_counts() -> Dict[str, Any]:
    for k in STATE.counts_by_cell.keys():
        STATE.counts_by_cell[k] = 0
    return {"ok": True}


@app.post("/debug/assign")
def debug_assign(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    conf = float(payload.get("confidence", 1.0))
    card = Card(
        game=payload.get("game", "mtg"),
        name=name,
        confidence=conf,
        scryfall_id=payload.get("scryfall_id"),
        printed_name=payload.get("printed_name"),
        flavor_name=payload.get("flavor_name"),
    )
    cell, reason = assign_card(card, CFG, STATE)
    STATE.counts_by_cell[cell] = STATE.counts_by_cell.get(cell, 0) + 1
    return {"cell": cell, "reason": reason, "counts": STATE.counts_by_cell}


@app.post("/debug/assign_preview")
def debug_assign_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_name = str(payload.get("name", "")).strip()
    raw_id = str(payload.get("scryfall_id", "")).strip()
    name = raw_name or raw_id
    conf = float(payload.get("confidence", 1.0))
    card = Card(
        game=payload.get("game", "mtg"),
        name=name,
        confidence=conf,
        scryfall_id=raw_id or None,
        printed_name=payload.get("printed_name"),
        flavor_name=payload.get("flavor_name"),
    )

    override = _normalize_operation_id(payload.get("sort_operation") or payload.get("operation"))
    previous_operation = STATE.active_sort_operation
    activated_override = False
    if override:
        if not CFG.sort_operations:
            raise HTTPException(status_code=404, detail="No sort operations configured")
        if override not in CFG.sort_operations:
            raise HTTPException(status_code=404, detail=f"Unknown sort operation '{override}'")
        STATE.active_sort_operation = override
        activated_override = True

    mode_request = (
        _normalize_mode_id(payload.get("sorting"))
        or _normalize_mode_id(payload.get("sort_mode"))
        or _normalize_mode_id(payload.get("mode"))
    )
    previous_mode = STATE.active_sort_mode
    activated_mode_override = False
    if mode_request:
        if mode_request not in CFG.sort_modes:
            raise HTTPException(status_code=404, detail=f"Unknown sort mode '{mode_request}'")
        STATE.active_sort_mode = mode_request
        activated_mode_override = True

    card_details = _get_card_details(raw_id) if raw_id else {}
    card_details = dict(card_details or {})
    if not card_details and raw_name:
        card_details = {"name": raw_name}

    metadata_name = card_details.get("name") if card_details else None
    if card_details:
        card.printed_name = card_details.get("printed_name") or card.printed_name
        card.flavor_name = card_details.get("flavor_name") or card.flavor_name

    if metadata_name and (not card.name or card.name == raw_id):
        card.name = metadata_name

    if card.printed_name and "printed_name" not in card_details:
        card_details["printed_name"] = card.printed_name
    if card.flavor_name and "flavor_name" not in card_details:
        card_details["flavor_name"] = card.flavor_name
    if card.name and "name" not in card_details:
        card_details["name"] = card.name

    display_name = (card.display_name() or "").strip()
    first = (display_name[:1] or "#").upper()
    if first < "A" or first > "Z":
        first = "A"

    reason_summary: Dict[str, Any] = {}
    mode_used = STATE.active_sort_mode or CFG.default_sort_mode or DEFAULT_SORT_MODE
    mode_cfg = CFG.sort_modes.get(mode_used)
    operation_used = STATE.active_sort_operation or CFG.default_sort_operation

    try:
        cell, reason = assign_card(card, CFG, STATE)
        operation_used = STATE.active_sort_operation or CFG.default_sort_operation
        reason_summary = _summarize_reason(reason)
        detected_mode = reason_summary.get("mode") if isinstance(reason_summary, dict) else None
        if detected_mode:
            mode_used = detected_mode
            mode_cfg = CFG.sort_modes.get(mode_used) or mode_cfg
    finally:
        if activated_override:
            STATE.active_sort_operation = previous_operation
        if activated_mode_override:
            STATE.active_sort_mode = previous_mode

    mode_label = mode_cfg.label if mode_cfg else mode_used
    mode_type = mode_cfg.type if mode_cfg else None
    first_key = None
    if isinstance(reason_summary, dict):
        first_key = reason_summary.get("key") or reason_summary.get("token")
    first_display = first_key or first

    return {
        "cell": cell,
        "reason": reason,
        "first": first_display,
        "card": card_details,
        "operation": operation_used,
        "mode": mode_used,
        "mode_label": mode_label,
        "mode_type": mode_type,
        "mode_key": first_key,
        "reason_details": reason_summary,
        "first_display": first_display,
    }


@app.post("/physical_testing/assign_known_card")
def assign_known_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    card_name = str(payload.get("card_name", "")).strip()
    confidence = float(payload.get("confidence", 1.0))
    perform_motion = bool(payload.get("perform_motion", False))
    if not card_name:
        raise HTTPException(status_code=400, detail="card_name is required")

    card = Card(
        game="mtg",
        name=card_name,
        confidence=confidence,
        scryfall_id=payload.get("scryfall_id"),
        printed_name=payload.get("printed_name"),
        flavor_name=payload.get("flavor_name"),
    )
    cell, reason = assign_card(card, CFG, STATE)
    STATE.counts_by_cell[cell] = STATE.counts_by_cell.get(cell, 0) + 1

    result = {
        "card_name": card_name,
        "assigned_cell": cell,
        "reason": reason,
        "confidence": confidence,
        "counts": STATE.counts_by_cell,
        "motion_performed": False,
    }
    if perform_motion:
        result["motion_performed"] = True
        result["motion_note"] = "Motion execution not yet implemented"
    return result


# ---------------------------------------------------------------------------
# Upload endpoints (OCR experiments)
# ---------------------------------------------------------------------------


@app.post("/ocr/run")
async def ocr_run(file: UploadFile = File(...)) -> Dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    try:
        result = ocr_pipeline.run_ocr_from_bytes(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    ocr_map = result.get("ocr_map") or {}
    embedding_info = card_id.embedding_matches_from_ocr(ocr_map, str(EMBEDDINGS_DIR))
    meta_block = dict(result.get("ocr_meta") or {})
    meta_block["embedding"] = embedding_info
    result["ocr_meta"] = meta_block
    return {
        "ok": True,
        **result,
        "embedding": embedding_info,
    }


@app.post("/ocr/upload")
async def ocr_upload(file: UploadFile = File(...)) -> Dict[str, Any]:  # pragma: no cover - tooling endpoint
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    encoded = base64.b64encode(data[:80]).decode("ascii")
    return {"ok": True, "preview": encoded}


# ---------------------------------------------------------------------------
# Auto-sort loop (capture -> OCR -> identify -> move -> repeat)
# ---------------------------------------------------------------------------

async def _auto_sort_loop():
    """
    Continuously captures snapshots, identifies cards, and moves them to assigned cells.
    Runs until AUTO_SORT_RUNNING is set to False.
    """
    global AUTO_SORT_RUNNING, AUTO_SORT_STATS
    
    LOG.info("Auto-sort loop started")
    AUTO_SORT_STATS["started_at"] = datetime.utcnow().isoformat(timespec="seconds")
    AUTO_SORT_STATS["cards_processed"] = 0
    AUTO_SORT_STATS["errors"] = 0
    
    # Small delay to ensure camera is ready
    await asyncio.sleep(1.0)
    
    while AUTO_SORT_RUNNING:
        try:
            # Step 1: Capture snapshot with OCR and card identification
            LOG.info("Auto-sort: Capturing snapshot...")
            try:
                snapshot_data = await camera_snapshot(quality=80, max_age=0.0)
            except Exception as snap_exc:
                LOG.error("Auto-sort: Camera snapshot failed: %s", snap_exc, exc_info=True)
                AUTO_SORT_STATS["errors"] += 1
                await asyncio.sleep(3.0)
                continue
            
            # Extract card information from snapshot
            frames = snapshot_data.get("frames", [])
            ocr_text_frame = None
            for frame in frames:
                if frame.get("label") == "ocr_text":
                    ocr_text_frame = frame
                    break
            
            if not ocr_text_frame:
                LOG.warning("Auto-sort: No OCR text frame found in snapshot")
                AUTO_SORT_STATS["errors"] += 1
                await asyncio.sleep(2.0)
                continue
            
            # Get card identification from OCR frame meta (same path as frontend)
            meta = ocr_text_frame.get("meta", {})
            identification = meta.get("identification", {}) if isinstance(meta, dict) else {}
            card_name = None
            scryfall_id = None
            confidence_score = 0.0
            
            # Extract identification data from the correct path
            if isinstance(identification, dict):
                best_match = identification.get("best")
                confidence_score = identification.get("score", 0.0)
                
                if isinstance(best_match, dict):
                    card_name = best_match.get("name")
                    scryfall_id = best_match.get("scryfall_id")
            
            # Require minimum confidence threshold (same as frontend: 70%)
            min_confidence = 70.0
            if confidence_score < min_confidence:
                LOG.warning(
                    "Auto-sort: Identification confidence too low: %.1f%% < %.1f%% for card '%s'",
                    confidence_score, min_confidence, card_name or "unknown"
                )
                AUTO_SORT_STATS["errors"] += 1
                await asyncio.sleep(2.0)
                continue
            
            if not card_name and not scryfall_id:
                LOG.warning("Auto-sort: Could not identify card from snapshot")
                AUTO_SORT_STATS["errors"] += 1
                await asyncio.sleep(2.0)
                continue
            
            LOG.info(
                "Auto-sort: Identified card: %s (ID: %s) with %.1f%% confidence", 
                card_name or "unknown", 
                scryfall_id or "none",
                confidence_score
            )
            
            # Step 2: Assign cell for the card
            card = Card(
                game="mtg",
                name=card_name or scryfall_id or "Unknown",
                confidence=confidence_score / 100.0,  # Convert percentage to 0-1 scale
                scryfall_id=scryfall_id,
            )
            cell, reason = assign_card(card, CFG, STATE)
            STATE.counts_by_cell[cell] = STATE.counts_by_cell.get(cell, 0) + 1
            
            LOG.info("Auto-sort: Assigned to cell %s (reason: %s)", cell, reason)
            
            # Write to scanned_cards.csv for the motion endpoint to pick up
            csv_path = Path("data") / "scanned_cards.csv"
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write header if file doesn't exist
            write_header = not csv_path.exists()
            
            import csv as _csv
            with csv_path.open("a", encoding="utf8", newline="") as fh:
                writer = _csv.DictWriter(fh, fieldnames=["timestamp", "card_name", "scryfall_id", "assigned_cell", "reason"])
                if write_header:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
                    "card_name": card_name or "",
                    "scryfall_id": scryfall_id or "",
                    "assigned_cell": cell,
                    "reason": reason or "",
                })
            
            # Step 3: Execute motion sequence (home Z, extrude, move to cell, drop, return)
            LOG.debug("Auto-sort: Executing motion sequence...")
            motion_result = await motion_home_z_and_extrude()
            
            if not motion_result.get("ok"):
                LOG.error("Auto-sort: Motion sequence failed: %s", motion_result.get("error", "unknown"))
                AUTO_SORT_STATS["errors"] += 1
                await asyncio.sleep(2.0)
                continue
            
            moved_to = motion_result.get("moved_to")
            if moved_to:
                LOG.info("Auto-sort: Successfully moved card to %s", moved_to)
            else:
                LOG.warning("Auto-sort: Motion completed but did not move to target cell")
            
            AUTO_SORT_STATS["cards_processed"] += 1
            LOG.info("Auto-sort: Card processed successfully (total: %d)", AUTO_SORT_STATS["cards_processed"])
            
            # Small delay before next card
            await asyncio.sleep(1.0)
            
        except Exception as exc:
            LOG.error("Auto-sort loop error: %s", exc, exc_info=True)
            AUTO_SORT_STATS["errors"] += 1
            await asyncio.sleep(3.0)  # Longer delay on error
    
    LOG.info("Auto-sort loop stopped (processed: %d, errors: %d)", 
             AUTO_SORT_STATS["cards_processed"], 
             AUTO_SORT_STATS["errors"])


@app.post("/auto_sort/start")
async def auto_sort_start() -> Dict[str, Any]:
    """Start the automatic sorting loop."""
    global AUTO_SORT_RUNNING, AUTO_SORT_TASK
    
    if AUTO_SORT_RUNNING:
        return {"ok": False, "message": "Auto-sort is already running", "running": True}
    
    # Check if camera is available before starting
    try:
        cam = camera_svc.get_manager()
        info = cam.info(ensure_capture=False)
        if not info.get("online"):
            return {
                "ok": False,
                "message": f"Camera is not online: {info.get('error', 'unknown error')}",
                "running": False
            }
    except Exception as exc:
        LOG.error("Camera check failed before starting auto-sort: %s", exc)
        return {
            "ok": False,
            "message": f"Camera check failed: {str(exc)}",
            "running": False
        }
    
    AUTO_SORT_RUNNING = True
    AUTO_SORT_TASK = asyncio.create_task(_auto_sort_loop())
    
    return {
        "ok": True,
        "message": "Auto-sort loop started",
        "running": True,
        "stats": AUTO_SORT_STATS,
    }


@app.post("/auto_sort/stop")
async def auto_sort_stop() -> Dict[str, Any]:
    """Stop the automatic sorting loop."""
    global AUTO_SORT_RUNNING, AUTO_SORT_TASK
    
    if not AUTO_SORT_RUNNING:
        return {"ok": False, "message": "Auto-sort is not running", "running": False}
    
    AUTO_SORT_RUNNING = False
    
    # Wait for the task to complete (with timeout)
    if AUTO_SORT_TASK:
        try:
            await asyncio.wait_for(AUTO_SORT_TASK, timeout=10.0)
        except asyncio.TimeoutError:
            LOG.warning("Auto-sort task did not stop within timeout, cancelling...")
            AUTO_SORT_TASK.cancel()
        except Exception as exc:
            LOG.warning("Error waiting for auto-sort task: %s", exc)
    
    return {
        "ok": True,
        "message": "Auto-sort loop stopped",
        "running": False,
        "stats": AUTO_SORT_STATS,
    }


@app.get("/auto_sort/status")
async def auto_sort_status() -> Dict[str, Any]:
    """Get the status of the automatic sorting loop."""
    return {
        "running": AUTO_SORT_RUNNING,
        "stats": AUTO_SORT_STATS,
    }


if __name__ == "__main__":  # pragma: no cover - manual launch helper
    import uvicorn  # type: ignore[import-not-found]

    uvicorn.run(app, host="0.0.0.0", port=8000)
