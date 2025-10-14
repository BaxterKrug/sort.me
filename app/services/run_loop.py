# services/run_loop.py (excerpt)
import yaml
from services.assign import load_config, Config, SystemState, Card, assign_card
import asyncio
import logging
from services import motion as motion_svc

LOG = logging.getLogger("sort.runloop")

CFG: Config = load_config(yaml.safe_load(open("config.yaml")))
state = SystemState(counts_by_cell={cid: 0 for cid in CFG.cells})

# configure motion controller with positions from CFG.cells (if available)
try:
    ctrl = motion_svc.get_controller()
    # build mapping cid -> {x,y,z} from CFG.cells (Cell objects or dicts)
    cells_map = {}
    for cid, c in CFG.cells.items():
        # support both dataclass-like objects and plain dicts
        try:
            x = float(getattr(c, "x", c.get("x")))
            y = float(getattr(c, "y", c.get("y")))
            z = float(getattr(c, "z", c.get("z", 0.0)))
        except Exception:
            # fallback: zeroed coords
            x, y, z = 0.0, 0.0, 0.0
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

        # determine source cell
        source_cell = meta.get("from_cell") or meta.get("source_cell") or meta.get("feeder")
        if not source_cell:
            # prefer feeders in column A, else any cell with non-zero count, else first cell
            feeders = [cid for cid in CFG.cells.keys() if str(cid).upper().startswith("A")]
            if feeders:
                source_cell = feeders[0]
            else:
                nonempty = [cid for cid, cnt in state.counts_by_cell.items() if cnt > 0]
                source_cell = nonempty[0] if nonempty else (list(CFG.cells.keys())[0] if CFG.cells else None)

        if source_cell is None:
            raise RuntimeError("No source cell available to pick from")

        # transfer using motion controller (async)
        controller = motion_svc.get_controller()
        LOG.info("Transferring card '%s' from %s -> %s (reason=%s)", card.name, source_cell, cell_id, reason)
        try:
            await controller.transfer_card(source_cell, cell_id)
        except Exception as e:
            LOG.error("Transfer failed: %s", e)
            # publish failure and return
            try:
                events.publish("placement_failed", {"card": card.name, "from": source_cell, "to": cell_id, "error": str(e)})
            except Exception:
                pass
            return

        # update state counts
        state.counts_by_cell[cell_id] = state.counts_by_cell.get(cell_id, 0) + 1

        # publish event for successful placement
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
