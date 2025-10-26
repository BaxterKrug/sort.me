# services/run_loop.py (excerpt)
import asyncio
import logging
from typing import Any, Dict, List, Optional

import yaml

from services.assign import Card, Config, SystemState, assign_card, load_config
from services import camera as camera_svc
from services import feeder_monitor as feeder_vision
from services import motion as motion_svc

try:  # optional event bus
    from services import events  # type: ignore
except Exception:  # pragma: no cover - events module is optional
    events = None  # type: ignore

LOG = logging.getLogger("sort.runloop")

with open("config.yaml", "r", encoding="utf8") as _cfg_fh:
    _RAW_CFG = yaml.safe_load(_cfg_fh)

CFG: Config = load_config(_RAW_CFG)
state = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})
CAMERA_CFG = _RAW_CFG.get("camera", {}) if isinstance(_RAW_CFG, dict) else {}

try:
    camera_svc.configure(CAMERA_CFG)
except Exception as exc:
    LOG.warning("Camera configuration failed: %s", exc)

try:
    feeder_vision.configure_from_cfg(CFG, CAMERA_CFG)
except Exception as exc:
    LOG.warning("Feeder monitor configuration failed: %s", exc)

# Track feeder ordering and inventory so we can exhaust one feeder before
# advancing to the next when running on real hardware.
_FEEDER_SEQUENCE: List[str] = []
_active_feeder: Optional[str] = None
_feeder_index: int = 0
_FEEDER_DETECTION: Dict[str, bool] = {}


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
    if CFG.feeder_re:
        feeders = [cid for cid in CFG.cells.keys() if CFG.feeder_re.search(cid)]
    if not feeders:
        feeders = [cid for cid in CFG.cells.keys() if str(cid).upper().startswith('A')]
    _FEEDER_SEQUENCE = sorted(dict.fromkeys(feeders), key=_cell_sort_key)
    _feeder_index = 0
    _active_feeder = None
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


def feeders_remaining() -> Dict[str, int]:
    """Return a snapshot of remaining cards per tracked feeder."""
    return {cid: state.feeder_remaining(cid) or 0 for cid in _FEEDER_SEQUENCE}


async def _refresh_feeder_detections(cells: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    monitor = feeder_vision.get_monitor()
    if not monitor or not getattr(monitor, "enabled", False):
        return {}
    try:
        results = await monitor.measure(target_cells=cells)
    except Exception as exc:
        LOG.warning("Feeder vision check failed: %s", exc)
        return {}

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
    return results


def _advance_to_next_feeder() -> Optional[str]:
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


def _ensure_active_feeder() -> Optional[str]:
    global _active_feeder
    if motion_svc.is_demo_mode():
        return None
    remaining = state.feeder_remaining(_active_feeder) if _active_feeder else None
    detected = _FEEDER_DETECTION.get(_active_feeder) if _active_feeder else None
    if detected is False:
        _active_feeder = None
        remaining = None
    if _active_feeder and (remaining is None or remaining > 0):
        return _active_feeder
    _active_feeder = _advance_to_next_feeder()
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


_initialize_feeder_state()

# configure motion controller with positions from CFG.cells (if available)
try:
    ctrl = motion_svc.get_controller()
    # build mapping cid -> {x,y,z} from CFG.cells (Cell objects or dicts)
    cells_map = {}
    for cid, c in CFG.cells.items():
        # support both dataclass-like objects and plain dicts
        try:
            x_val = getattr(c, "x", None)
            y_val = getattr(c, "y", None)
            z_val = getattr(c, "z", None)
        except Exception:
            x_val = y_val = z_val = None
        if x_val is None or y_val is None or z_val is None:
            if isinstance(c, dict):
                x_val = c.get("x", x_val)
                y_val = c.get("y", y_val)
                z_val = c.get("z", z_val if z_val is not None else 0.0)
        try:
            x = float(x_val) if x_val is not None else 0.0
        except Exception:
            x = 0.0
        try:
            y = float(y_val) if y_val is not None else 0.0
        except Exception:
            y = 0.0
        try:
            z = float(z_val) if z_val is not None else 0.0
        except Exception:
            z = 0.0
        cells_map[cid] = {"x": x, "y": y, "z": z}
    ctrl.configure_cells(cells_map)
    LOG.info("Motion controller configured from CFG")
except Exception as e:
    LOG.warning("Failed to configure motion controller from CFG: %s", e)

# make an async handler so callers can schedule it safely
async def _handle_card_identified_async(meta: dict):
    """
    Async handler: identify assignment, perform transfer (pick/place) via motion controller,
    update state and publish events.
    Expects meta to possibly include a source cell (meta['from_cell'] or meta['source_cell']).
    If not provided, will attempt to select a reasonable feeder (cells starting with 'A').
    """
    try:
        card = Card(
            game=meta.get("game", "mtg"),
            name=meta["name"],
            set_code=meta.get("set_code"),
            collector_number=meta.get("collector_number"),
            confidence=float(meta.get("confidence", 1.0)),
        )

        cell_id, reason = assign_card(card, CFG, state)

        if not motion_svc.is_demo_mode():
            await _refresh_feeder_detections()

        # determine source cell
        source_cell = meta.get("from_cell") or meta.get("source_cell") or meta.get("feeder")
        if not source_cell:
            if motion_svc.is_demo_mode():
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

        if not motion_svc.is_demo_mode() and source_cell in _FEEDER_SEQUENCE:
            global _active_feeder
            _active_feeder = source_cell

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
