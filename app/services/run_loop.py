# services/run_loop.py (excerpt)
import asyncio
import logging
from typing import Any, Dict, List, Optional

import yaml  # type: ignore[import-not-found]

from app.services.assign import Card, Config, SystemState, assign_card, load_config
from app.services import camera as camera_svc
from app.services import feeder_monitor as feeder_vision
from app.services import motion as motion_svc
from app.services import qr_scanner as qr_svc
from app.services import sort_session

try:  # optional event bus
    from app.services import events  # type: ignore
except Exception:  # pragma: no cover - events module is optional
    events = None  # type: ignore

LOG = logging.getLogger("sort.runloop")

with open("config.yaml", "r", encoding="utf8") as _cfg_fh:
    _RAW_CFG = yaml.safe_load(_cfg_fh)

CFG: Config = load_config(_RAW_CFG)
state = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})
state.active_sort_operation = CFG.default_sort_operation
state.active_sort_mode = CFG.default_sort_mode
CAMERA_CFG = _RAW_CFG.get("camera", {}) if isinstance(_RAW_CFG, dict) else {}

try:
    camera_svc.configure(CAMERA_CFG)
except Exception as exc:
    LOG.warning("Camera configuration failed: %s", exc)

try:
    feeder_vision.configure_from_cfg(CFG, CAMERA_CFG)
except Exception as exc:
    LOG.warning("Feeder monitor configuration failed: %s", exc)

try:
    qr_svc.configure_from_cfg(CFG, CAMERA_CFG)
except Exception as exc:
    LOG.warning("QR scanner configuration failed: %s", exc)

# Track feeder ordering and inventory so we can exhaust one feeder before
# advancing to the next when running on real hardware.
_FEEDER_SEQUENCE: List[str] = []
_active_feeder: Optional[str] = None
_feeder_index: int = 0
_FEEDER_DETECTION: Dict[str, bool] = {}

# Track processed QR codes to avoid re-executing commands
_processed_qr_codes: Dict[str, str] = {}  # cell -> last processed QR data


def parse_qr_command(qr_data: str) -> Dict[str, Any]:
    """Parse QR code data into a command structure.
    
    Expected formats:
        - "A1 endstep" - Cell A1 end of stack, advance to next feeder
        - "A1" - Simple cell identifier (defaults to endstep behavior)
        - "endstep" - Generic end of stack command
        - "FEEDER_A1_END" - Legacy format for end of stack
        - "FEEDER_A1_REFILL" - Legacy format for refill command
        
    Args:
        qr_data: Raw string data from QR code
        
    Returns:
        Dict with keys:
            - cell: Optional cell identifier (e.g., "A1")
            - command: Command to execute (e.g., "endstep")
            - raw: Original QR data
    """
    import re
    
    if not qr_data:
        return {"cell": None, "command": None, "raw": qr_data}
    
    data_upper = qr_data.strip().upper()
    result = {"cell": None, "command": "endstep", "raw": qr_data}  # Default to endstep
    
    # Handle legacy "FEEDER_XX_END" or "FEEDER_XX_REFILL" format
    feeder_pattern = re.compile(r'^FEEDER_([A-Z]+\d+)_(\w+)$')
    match = feeder_pattern.match(data_upper)
    if match:
        result["cell"] = match.group(1)
        cmd_word = match.group(2).lower()
        # Map legacy command names to new ones
        if cmd_word in ("end", "endstep", "empty"):
            result["command"] = "endstep"
        elif cmd_word in ("refill", "reload", "full"):
            result["command"] = "refill"
        else:
            result["command"] = cmd_word
        return result
    
    parts = data_upper.split()
    
    if len(parts) == 0:
        return result
    
    # Check if first part is a cell identifier (letter + number pattern)
    cell_pattern = re.compile(r'^[A-Z]+\d+$')
    
    if cell_pattern.match(parts[0]):
        result["cell"] = parts[0]
        if len(parts) > 1:
            result["command"] = parts[1].lower()
    else:
        # First part is probably a command
        result["command"] = parts[0].lower()
        if len(parts) > 1 and cell_pattern.match(parts[1]):
            result["cell"] = parts[1]
    
    return result


async def execute_qr_command(qr_data: str, detected_cell: str) -> Dict[str, Any]:
    """Execute a command parsed from QR code data.
    
    Args:
        qr_data: Raw QR code data string
        detected_cell: Cell where QR code was detected
        
    Returns:
        Dict with execution results
    """
    global _active_feeder, _processed_qr_codes, _feeder_index
    
    # Parse the command
    cmd = parse_qr_command(qr_data)
    cell = cmd.get("cell") or detected_cell
    command = cmd.get("command", "endstep")
    
    result = {
        "cell": cell,
        "command": command,
        "executed": False,
        "message": "",
    }
    
    # Check if we've already processed this exact QR code for this cell
    last_processed = _processed_qr_codes.get(cell)
    if last_processed == qr_data:
        result["message"] = f"QR code already processed for {cell}"
        return result
    
    LOG.info("Executing QR command: %s for cell %s (raw: %s)", command, cell, qr_data)
    
    if command == "endstep":
        # End of stack - mark feeder as empty and advance to next
        state.feeder_counts[cell] = 0
        _FEEDER_DETECTION[cell] = False
        
        # Find the next feeder after the detected cell
        next_feeder = None
        if cell in _FEEDER_SEQUENCE:
            # Set feeder index to point after the detected cell
            try:
                cell_idx = _FEEDER_SEQUENCE.index(cell)
                _feeder_index = cell_idx + 1  # Start search from next cell
            except ValueError:
                pass
        
        # Find next available feeder
        next_feeder = _advance_to_next_feeder()
        if next_feeder:
            _active_feeder = next_feeder
            state.active_feeder = next_feeder
            LOG.info("Advanced to next feeder: %s", next_feeder)
            result["message"] = f"Advanced from {cell} to {next_feeder}"
            result["next_feeder"] = next_feeder
            
            # Move to the next feeder position
            try:
                if not motion_svc.is_demo_mode():
                    LOG.info("Moving to next feeder position: %s", next_feeder)
                    ctrl = motion_svc.get_controller()
                    await ctrl.move_to_cell_xy(next_feeder)
                    result["moved"] = True
            except Exception as move_exc:
                LOG.warning("Failed to move to next feeder %s: %s", next_feeder, move_exc)
                result["move_error"] = str(move_exc)
        else:
            _active_feeder = None
            state.active_feeder = None
            LOG.info("Sort complete - all feeders processed after %s", cell)
            result["message"] = f"Sort complete - all feeders processed"
            result["sort_complete"] = True
        
        result["executed"] = True
        _processed_qr_codes[cell] = qr_data
        
        # Publish event if available
        if events:
            try:
                events.publish("qr_command", {"cell": cell, "command": command, "result": result})
            except Exception:
                pass
    
    elif command == "refill":
        # Mark feeder as refilled
        capacity = 120  # Default capacity
        cell_cfg = CFG.cells.get(cell)
        if cell_cfg:
            capacity = getattr(cell_cfg, 'capacity', 120) or 120
        state.feeder_counts[cell] = capacity
        _FEEDER_DETECTION[cell] = True
        result["executed"] = True
        result["message"] = f"Refilled {cell} with {capacity} cards"
        _processed_qr_codes[cell] = qr_data
        LOG.info("Feeder %s refilled via QR command with %d cards", cell, capacity)
    
    elif command == "pause":
        # Pause the sorting operation
        state.paused = True
        result["executed"] = True
        result["message"] = "Sorting paused"
        _processed_qr_codes[cell] = qr_data
        LOG.info("Sorting paused via QR command")
    
    elif command == "resume":
        # Resume the sorting operation
        state.paused = False
        result["executed"] = True
        result["message"] = "Sorting resumed"
        _processed_qr_codes[cell] = qr_data
        LOG.info("Sorting resumed via QR command")
    
    else:
        result["message"] = f"Unknown command: {command}"
        LOG.warning("Unknown QR command: %s", command)
    
    return result


def reset_qr_command_state(cell: Optional[str] = None) -> None:
    """Reset the processed QR code state to allow re-execution.
    
    Args:
        cell: Specific cell to reset, or None to reset all.
    """
    global _processed_qr_codes
    if cell:
        _processed_qr_codes.pop(cell, None)
    else:
        _processed_qr_codes.clear()


def _cell_sort_key(cell_id: str) -> tuple:
    letters = ''.join(ch for ch in cell_id if ch.isalpha()).upper() or 'A'
    nums = ''.join(ch for ch in cell_id if ch.isdigit()) or '1'
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord('A'))
    try:
        row = max(0, int(nums) - 1)
    except Exception:
        row = 0
    return (col, row, cell_id)


def _initial_feeder_stock(cell_id: str) -> int:
    cell = CFG.cells.get(cell_id)
    if not cell:
        return 0
    if cell_id in state.counts_by_cell and state.counts_by_cell[cell_id] > 0:
        return state.counts_by_cell[cell_id]
    capacity = getattr(cell, 'capacity', 0)
    return int(capacity) if capacity else 0


def _initialize_feeder_state() -> None:
    global _FEEDER_SEQUENCE, _feeder_index, _active_feeder
    feeders: List[str] = []
    
    # Priority 1: Use explicit feeder_sequence from config (includes tagged cells)
    if CFG.feeder_sequence:
        feeders = list(CFG.feeder_sequence)
        LOG.info("Using feeder sequence from config: %s", feeders)
    # Priority 2: Fall back to feeder regex pattern
    elif CFG.feeder_re:
        feeders = [cid for cid in CFG.cells.keys() if CFG.feeder_re.search(cid)]
        LOG.info("Using feeder regex pattern: %s", feeders)
    # Priority 3: Fall back to cells starting with 'A'
    if not feeders:
        feeders = [cid for cid in CFG.cells.keys() if str(cid).upper().startswith('A')]
        LOG.info("Falling back to 'A' prefix for feeders: %s", feeders)
    
    _FEEDER_SEQUENCE = sorted(dict.fromkeys(feeders), key=_cell_sort_key)
    _feeder_index = 0
    _active_feeder = None
    state.active_feeder = None
    for cid in _FEEDER_SEQUENCE:
        stock = _initial_feeder_stock(cid)
        if stock <= 0:
            # default to capacity if unknown so we at least service the feeder once
            stock = getattr(CFG.cells[cid], 'capacity', 0) or 0
        state.counts_by_cell[cid] = stock
        state.feeder_counts[cid] = stock
    if _FEEDER_SEQUENCE:
        LOG.info("Feeder sequence initialised: %s", _FEEDER_SEQUENCE)
        LOG.info("Initial feeder stock: %s", {cid: state.feeder_counts.get(cid, 0) for cid in _FEEDER_SEQUENCE})


def reinitialize_feeders_for_mode(mode_id: Optional[str] = None) -> None:
    """Reinitialize the feeder sequence based on the active sort mode.

    For price mode with column-based binary sort, row-1 cells of each
    configured column become the feeder sequence (A1, B1, ... L1).
    For all other modes, the default feeder sequence (from config tags /
    regex) is restored.
    """
    global _FEEDER_SEQUENCE, _feeder_index, _active_feeder

    mode_id = mode_id or state.active_sort_mode or CFG.default_sort_mode
    mode = CFG.sort_modes.get(mode_id) if mode_id else None

    if mode and mode.type == "price" and mode.price_columns:
        # Build feeder sequence from price mode columns (row 1 cells)
        feeder_row = mode.price_feeder_row
        feeders = [f"{col}{feeder_row}" for col in mode.price_columns
                   if f"{col}{feeder_row}" in CFG.cells]
        LOG.info("Price mode '%s': feeder sequence set to row-%d cells: %s",
                 mode_id, feeder_row, feeders)
    else:
        # Restore default feeder sequence
        feeders = list(CFG.feeder_sequence) if CFG.feeder_sequence else []
        if not feeders and CFG.feeder_re:
            feeders = [cid for cid in CFG.cells.keys() if CFG.feeder_re.search(cid)]
        if not feeders:
            feeders = [cid for cid in CFG.cells.keys() if str(cid).upper().startswith('A')]
        LOG.info("Restored default feeder sequence: %s", feeders)

    _FEEDER_SEQUENCE = sorted(dict.fromkeys(feeders), key=_cell_sort_key)
    _feeder_index = 0
    _active_feeder = None
    state.active_feeder = None

    for cid in _FEEDER_SEQUENCE:
        stock = _initial_feeder_stock(cid)
        if stock <= 0:
            cell_cfg = CFG.cells.get(cid)
            stock = getattr(cell_cfg, 'capacity', 0) or 0 if cell_cfg else 0
        state.counts_by_cell[cid] = stock
        state.feeder_counts[cid] = stock

    _processed_qr_codes.clear()

    if _FEEDER_SEQUENCE:
        LOG.info("Feeder sequence reinitialised for mode '%s': %s", mode_id, _FEEDER_SEQUENCE)


def set_feeder_inventory(updates: Dict[str, int]) -> None:
    """Allow external callers/tests to override remaining cards per feeder."""
    for cid, count in updates.items():
        count_int = max(0, int(count))
        state.feeder_counts[cid] = count_int
        state.counts_by_cell[cid] = count_int
        _FEEDER_DETECTION[cid] = bool(count_int)
    global _active_feeder
    if _active_feeder and state.feeder_remaining(_active_feeder) == 0:
        _active_feeder = None
        state.active_feeder = None


async def _refresh_feeder_detections(cells: Optional[List[str]] = None) -> Dict[str, Any]:
    """Check feeder status using visual monitoring and QR code detection.
    
    Args:
        cells: Optional list of cell IDs to check. If None, checks all feeders.
        
    Returns:
        Dictionary mapping cell IDs to detection results.
    """
    global _active_feeder
    monitor = feeder_vision.get_monitor()
    scanner = qr_svc.get_scanner()
    
    results = {}
    
    # Check visual fill detection
    if monitor and getattr(monitor, "enabled", False):
        try:
            results = await monitor.measure(target_cells=cells)
        except Exception as exc:
            LOG.warning("Feeder vision check failed: %s", exc)

        for cid, data in results.items():
            empty = bool(data.get("empty"))
            _FEEDER_DETECTION[cid] = not empty
            if empty:
                if state.feeder_counts.get(cid, 0) != 0:
                    LOG.info("Feeder %s detected empty via camera", cid)
                state.feeder_counts[cid] = 0
            else:
                if state.feeder_counts.get(cid, 0) == 0:
                    LOG.info("Feeder %s detected as refilled via camera", cid)
                    state.feeder_counts[cid] = 1
                    state.counts_by_cell[cid] = 1
    
    # Check QR code detection and execute commands (simplified full-frame scanner)
    if scanner and getattr(scanner, "enabled", False):
        try:
            qr_result = await scanner.scan()  # Scan full frame for single QR
        except Exception as exc:
            LOG.warning("QR scanner check failed: %s", exc)
            qr_result = {}
        
        # Reset processed QR codes when QR code fully disappears to allow re-detection
        # This happens when the scanner's stable tracking has been cleared (QR absent for N frames)
        if scanner.is_stable_cleared() and _processed_qr_codes:
            LOG.debug("QR code fully disappeared, clearing processed state for re-detection")
            _processed_qr_codes.clear()
        
        # If QR code is detected with stable reading, execute the command
        if qr_result.get("stable") and qr_result.get("is_new_stable"):
            qr_data = qr_result.get("data", "")
            cell_from_qr = qr_result.get("cell")  # Cell parsed from QR data
            command = qr_result.get("command", "endstep")
            
            if qr_data:
                # Use cell from QR code, or active feeder as fallback
                target_cell = cell_from_qr or _active_feeder
                
                LOG.info("QR command detected: '%s' (cell=%s, cmd=%s)", qr_data, target_cell, command)
                
                # Execute the command
                cmd_result = await execute_qr_command(qr_data, target_cell or "A1")
                
                # Add to results
                if target_cell:
                    if target_cell not in results:
                        results[target_cell] = {}
                    results[target_cell]["qr_detected"] = True
                    results[target_cell]["qr_data"] = qr_data
                    results[target_cell]["qr_command"] = cmd_result
    
    return results


def _advance_to_next_feeder() -> Optional[str]:
    """Find and return the next feeder cell with remaining cards."""
    global _feeder_index
    if not _FEEDER_SEQUENCE:
        return None
    for _ in range(len(_FEEDER_SEQUENCE)):
        cid = _FEEDER_SEQUENCE[_feeder_index % len(_FEEDER_SEQUENCE)]
        _feeder_index += 1
        remaining = state.feeder_remaining(cid)
        detected_clear = _FEEDER_DETECTION.get(cid)
        if detected_clear is False:
            state.feeder_counts[cid] = 0
            continue
        if remaining is None or remaining > 0:
            return cid
    return None


def feeders_remaining() -> Dict[str, int]:
    """Return a snapshot of remaining cards per tracked feeder."""
    return {cid: state.feeder_remaining(cid) or 0 for cid in _FEEDER_SEQUENCE}


def _ensure_active_feeder() -> Optional[str]:
    global _active_feeder
    if motion_svc.is_demo_mode():
        return None
    remaining = state.feeder_remaining(_active_feeder) if _active_feeder else None
    detected = _FEEDER_DETECTION.get(_active_feeder) if _active_feeder else None
    if detected is False:
        _active_feeder = None
        state.active_feeder = None
        remaining = None
    if _active_feeder and (remaining is None or remaining > 0):
        state.active_feeder = _active_feeder
        return _active_feeder
    _active_feeder = _advance_to_next_feeder()
    state.active_feeder = _active_feeder
    return _active_feeder


def _mark_feeder_decrement(cell_id: str) -> None:
    remaining = state.decrement_feeder(cell_id)
    if remaining is None:
        return
    state.counts_by_cell[cell_id] = remaining
    if remaining == 0:
        LOG.info("Feeder %s exhausted; advancing to next feeder", cell_id)
        _FEEDER_DETECTION[cell_id] = False
        global _active_feeder
        if _active_feeder == cell_id:
            _active_feeder = None
            state.active_feeder = None


_initialize_feeder_state()

# configure motion controller with positions from grid.positions in raw config
try:
    ctrl = motion_svc.get_controller()
    cells_map = {}
    
    # Load grid positions from raw config
    grid_cfg = _RAW_CFG.get("grid", {}) if isinstance(_RAW_CFG, dict) else {}
    grid_positions = grid_cfg.get("positions", {}) if isinstance(grid_cfg, dict) else {}
    
    # Build cells_map from grid positions
    for cid in CFG.cells.keys():
        if cid in grid_positions:
            pos = grid_positions[cid]
            if isinstance(pos, (list, tuple)) and len(pos) >= 2:
                x, y = float(pos[0]), float(pos[1])
                z = float(pos[2]) if len(pos) >= 3 else 0.0
                cells_map[cid] = {"x": x, "y": y, "z": z}
            else:
                LOG.warning("Cell %s has invalid position format in grid.positions: %s", cid, pos)
                cells_map[cid] = {"x": 0.0, "y": 0.0, "z": 0.0}
        else:
            LOG.warning("Cell %s not found in grid.positions, defaulting to (0,0,0)", cid)
            cells_map[cid] = {"x": 0.0, "y": 0.0, "z": 0.0}
    
    ctrl.configure_cells(cells_map)
    LOG.info("Motion controller configured from grid.positions (%d cells)", len(cells_map))
except Exception as e:
    LOG.warning("Failed to configure motion controller: %s", e)

# make an async handler so callers can schedule it safely
async def _handle_card_identified_async(meta: dict):
    """
    Async handler: identify assignment, perform transfer (pick/place) via motion controller,
    update state and publish events.
    Expects meta to possibly include a source cell (meta['from_cell'] or meta['source_cell']).
    If not provided, will attempt to select a reasonable feeder (cells starting with 'A').
    """
    global _active_feeder
    try:
        card = Card(
            game=meta.get("game", "mtg"),
            name=meta["name"],
            set_code=meta.get("set_code"),
            collector_number=meta.get("collector_number"),
            scryfall_id=meta.get("scryfall_id") or meta.get("id"),
            confidence=float(meta.get("confidence", 1.0)),
            printed_name=meta.get("printed_name"),
            flavor_name=meta.get("flavor_name"),
        )

        if not motion_svc.is_demo_mode():
            await _refresh_feeder_detections()

        # determine source cell BEFORE assignment so state.active_feeder is set
        # for price-mode column routing
        source_cell = meta.get("from_cell") or meta.get("source_cell") or meta.get("feeder")
        if not source_cell:
            if motion_svc.is_demo_mode():
                # In demo mode, pick the first feeder in the current sequence
                if _FEEDER_SEQUENCE:
                    source_cell = _active_feeder or _FEEDER_SEQUENCE[0]
                else:
                    feeders = [cid for cid in CFG.cells.keys() if str(cid).upper().startswith("A")]
                    if feeders:
                        source_cell = feeders[0]
                    else:
                        nonempty = [cid for cid, cnt in state.counts_by_cell.items() if cnt > 0]
                        source_cell = nonempty[0] if nonempty else (list(CFG.cells.keys())[0] if CFG.cells else None)
            else:
                source_cell = _ensure_active_feeder()

        if source_cell is None:
            if motion_svc.is_demo_mode():
                raise RuntimeError("No source cell available to pick from")
            LOG.info("No feeder cells available with remaining stock; halting pick sequence")
            return

        # Set active feeder on state so assign_card can route by column
        if source_cell in _FEEDER_SEQUENCE:
            _active_feeder = source_cell
            state.active_feeder = source_cell

        cell_id, reason = assign_card(card, CFG, state)

        # transfer using motion controller (async)
        controller = motion_svc.get_controller()
        LOG.info("Transferring card '%s' from %s -> %s (reason=%s)", card.name, source_cell, cell_id, reason)
        try:
            await controller.transfer_card(source_cell, cell_id)
        except Exception as e:
            LOG.error("Transfer failed: %s", e)
            # publish failure and return
            if events:
                try:
                    events.publish("placement_failed", {"card": card.name, "from": source_cell, "to": cell_id, "error": str(e)})
                except Exception:
                    pass
            return

        # update state counts
        state.counts_by_cell[cell_id] = state.counts_by_cell.get(cell_id, 0) + 1
        if not motion_svc.is_demo_mode():
            _mark_feeder_decrement(source_cell)
            await _refresh_feeder_detections([source_cell])

        # publish event for successful placement
        if events:
            try:
                events.publish("placement", {"card": card.name, "cell": cell_id, "reason": reason})
            except Exception:
                LOG.debug("events.publish unavailable or failed")

        # persist operation details for spreadsheet export
        try:
            orientation = meta.get("orientation") if isinstance(meta, dict) else None
            image_paths = meta.get("image_paths") if isinstance(meta, dict) else None
            ocr_map = None
            if isinstance(meta, dict):
                ocr_map = meta.get("ocr") or meta.get("ocr_map")

            entry = {
                "card_name": card.name,
                "set_code": card.set_code,
                "collector_number": card.collector_number,
                "scryfall_id": card.scryfall_id,
                "confidence": card.confidence,
                "assigned_cell": cell_id,
                "source_cell": source_cell,
                "reason": reason,
                "ocr_name": (ocr_map or {}).get("name") if isinstance(ocr_map, dict) else meta.get("name") if isinstance(meta, dict) else None,
                "orientation": orientation,
                "image_paths": image_paths,
            }
            await sort_session.log_operation_async(entry)
        except Exception as log_exc:  # pragma: no cover - logging should not break sort flow
            LOG.warning("Failed to record sort operation: %s", log_exc)

    except Exception as exc:
        LOG.exception("on_card_identified failed: %s", exc)

# sync wrapper for older callers: schedules the async handler
def on_card_identified(meta: dict):
    """
    Backwards-compatible entrypoint: schedule the async handler on the event loop.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_handle_card_identified_async(meta))
    except RuntimeError:
        # no running loop; start a new one briefly
        asyncio.run(_handle_card_identified_async(meta))
