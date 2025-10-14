"""
Motion controller and simulated driver for the sorter.

Provides:
 - MotionController: high-level operations (move_to_cell, jog, home_all, calibrate_routine,
   pick_card_from_cell, place_card_to_cell, transfer_card)
 - MotionDriver interface + SimulatedDriver implementation
 - Config loader to wire cell positions (expects dict cell_id -> {x,y,z})
 - Async API so existing FastAPI endpoints can call these functions easily

Note: replace SimulatedDriver with a real hardware driver implementing the same
methods (move_absolute, set_speed, vacuum_on/off, plunger_up/down, stop).
"""
from typing import Dict, Any, Optional, Tuple
import asyncio
import time
import logging

LOG = logging.getLogger("sort.motion")
logging.basicConfig(level=logging.INFO)

# Driver interface (duck-typed)
class MotionDriver:
    async def move_absolute(self, x: float, y: float, z: float, speed: float) -> None:
        raise NotImplementedError()
    async def set_speed(self, speed: float) -> None:
        raise NotImplementedError()
    async def vacuum_on(self) -> None:
        raise NotImplementedError()
    async def vacuum_off(self) -> None:
        raise NotImplementedError()
    async def plunger_down(self) -> None:
        raise NotImplementedError()
    async def plunger_up(self) -> None:
        raise NotImplementedError()
    async def stop(self) -> None:
        raise NotImplementedError()
    async def home_all(self) -> None:
        raise NotImplementedError()

# Simple simulated driver for local testing
class SimulatedDriver(MotionDriver):
    def __init__(self):
        self.pos = (0.0, 0.0, 0.0)
        self.speed = 100.0
        self.vacuum = False
        self.plunger = "up"

    async def _fake_move(self, x, y, z, speed):
        dist = ((self.pos[0]-x)**2 + (self.pos[1]-y)**2 + (self.pos[2]-z)**2) ** 0.5
        # simple time model: dist / (speed/100) seconds (speed is arbitrary)
        duration = max(0.02, dist / max(1.0, speed/100.0))
        LOG.info("Simulated move -> (%.2f,%.2f,%.2f) speed=%.1f (t=%.2fs)", x, y, z, speed, duration)
        await asyncio.sleep(duration)
        self.pos = (x, y, z)

    async def move_absolute(self, x: float, y: float, z: float, speed: float) -> None:
        await self._fake_move(x, y, z, speed)

    async def set_speed(self, speed: float) -> None:
        LOG.info("Simulated set_speed=%s", speed)
        self.speed = speed

    async def vacuum_on(self) -> None:
        LOG.info("Simulated vacuum ON")
        self.vacuum = True
        await asyncio.sleep(0.05)

    async def vacuum_off(self) -> None:
        LOG.info("Simulated vacuum OFF")
        self.vacuum = False
        await asyncio.sleep(0.02)

    async def plunger_down(self) -> None:
        LOG.info("Simulated plunger DOWN")
        self.plunger = "down"
        await asyncio.sleep(0.07)

    async def plunger_up(self) -> None:
        LOG.info("Simulated plunger UP")
        self.plunger = "up"
        await asyncio.sleep(0.07)

    async def stop(self) -> None:
        LOG.info("Simulated stop")
        # no-op for simulation

    async def home_all(self) -> None:
        LOG.info("Simulated homing all axes")
        await asyncio.sleep(0.5)
        self.pos = (0.0, 0.0, 0.0)

class MotionController:
    """
    High level motion controller. Use configure() to provide cell positions:
      { 'A1': {'x':..., 'y':..., 'z':...}, ... }

    All coordinates are assumed to be in the same units as the real driver expects.
    """
    def __init__(self, driver: Optional[MotionDriver] = None):
        self.driver = driver or SimulatedDriver()
        self.cells: Dict[str, Dict[str, float]] = {}
        self.current: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.homed = False
        self.default_speed = 200.0  # arbitrary units
        self.lock = asyncio.Lock()

    def configure_cells(self, cells: Dict[str, Dict[str, float]]) -> None:
        """
        cells: mapping cell_id -> {'x':float,'y':float,'z':float}
        """
        self.cells = {k: {'x': float(v['x']), 'y': float(v['y']), 'z': float(v.get('z', 0.0))} for k, v in cells.items()}
        LOG.info("Configured %d cells", len(self.cells))

    async def home_all(self) -> None:
        async with self.lock:
            await self.driver.home_all()
            self.homed = True
            # trust driver to zero position; update current pos
            self.current = (0.0, 0.0, 0.0)
            LOG.info("Homed and set current position to %s", self.current)

    async def move_to_cell(self, cell_id: str, speed: Optional[float] = None) -> None:
        """
        Move to a named cell. Raises KeyError if unknown.
        """
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell {cell_id}")
        pos = self.cells[cell_id]
        target = (pos['x'], pos['y'], pos.get('z', 0.0))
        sp = speed or self.default_speed
        async with self.lock:
            await self.driver.set_speed(sp)
            await self.driver.move_absolute(*target, sp)
            self.current = target
            LOG.info("Moved to cell %s -> %s", cell_id, target)

    async def jog(self, axis: str, delta: float, speed: Optional[float] = None) -> Tuple[float,float,float]:
        """
        Jog the machine along x/y/z by delta. Returns new position.
        axis: 'x'|'y'|'z'
        """
        if axis not in ('x','y','z'):
            raise ValueError("axis must be 'x','y' or 'z'")
        sp = speed or self.default_speed
        async with self.lock:
            x,y,z = self.current
            if axis == 'x':
                x += float(delta)
            elif axis == 'y':
                y += float(delta)
            else:
                z += float(delta)
            await self.driver.set_speed(sp)
            await self.driver.move_absolute(x,y,z,sp)
            self.current = (x,y,z)
            LOG.info("Jogged %s by %s -> pos=%s", axis, delta, self.current)
            return self.current

    async def calibrate_routine(self, points: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Any]:
        """
        Run a simple calibration routine:
          - home all
          - visit each provided point (mapping name->pos) and record actual driver pos
        Returns mapping name -> observed_pos for user to confirm/save.
        If no points provided, will iterate configured cells but only a small sample to speed up.
        """
        async with self.lock:
            await self.home_all()
            observed = {}
            sample_keys = list(points.keys()) if points else list(self.cells.keys())[:12]
            for k in sample_keys:
                if k not in self.cells:
                    continue
                pos = self.cells[k]
                await self.driver.move_absolute(pos['x'], pos['y'], pos.get('z',0.0), self.default_speed)
                # small settle
                await asyncio.sleep(0.05)
                # read back driver pos if driver exposes it; SimulatedDriver stores it in .pos
                observed[k] = getattr(self.driver, "pos", (pos['x'], pos['y'], pos.get('z',0.0)))
                LOG.info("Calib visit %s -> observed %s", k, observed[k])
            return {"observed": observed, "sampled": len(observed)}

    async def pick_card_from_cell(self, cell_id: str, pick_z_offset: float = -5.0) -> None:
        """
        Pick (vacuum + plunger sequence) from a given cell. Sequence:
          - move above cell (safe Z)
          - plunger down (if present) then vacuum on then plunger up
        pick_z_offset: relative move in z to lower to reach card, driver expects absolute coords so z is used directly.
        """
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell {cell_id}")
        async with self.lock:
            pos = self.cells[cell_id]
            # move to safe above position (use z + 10 mm)
            safe_z = pos.get('z', 0.0) + 10.0
            await self.driver.move_absolute(pos['x'], pos['y'], safe_z, self.default_speed)
            # plunge
            await self.driver.plunger_down()
            # move to pick height
            pick_z = pos.get('z', 0.0) + pick_z_offset
            await self.driver.move_absolute(pos['x'], pos['y'], pick_z, self.default_speed/2)
            await self.driver.vacuum_on()
            await asyncio.sleep(0.06)
            await self.driver.plunger_up()
            LOG.info("Picked card from %s", cell_id)

    async def place_card_to_cell(self, cell_id: str, place_z_offset: float = -5.0) -> None:
        """
        Place card to a target cell:
          - move above target, lower, disable vacuum, retract
        """
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell %s" % cell_id)
        async with self.lock:
            pos = self.cells[cell_id]
            safe_z = pos.get('z', 0.0) + 10.0
            await self.driver.move_absolute(pos['x'], pos['y'], safe_z, self.default_speed)
            await self.driver.move_absolute(pos['x'], pos['y'], pos.get('z', 0.0) + place_z_offset, self.default_speed/2)
            # release
            await self.driver.vacuum_off()
            await asyncio.sleep(0.03)
            await self.driver.move_absolute(pos['x'], pos['y'], safe_z, self.default_speed/2)
            LOG.info("Placed card to %s", cell_id)

    async def transfer_card(self, from_cell: str, to_cell: str) -> Dict[str, Any]:
        """
        Complete pick-and-place operation from from_cell -> to_cell.
        Returns dict with timings and final current position.
        """
        if from_cell not in self.cells or to_cell not in self.cells:
            raise KeyError("Unknown source or target cell")
        async with self.lock:
            start = time.time()
            await self.pick_card_from_cell(from_cell)
            # small travel move to target safe height
            tpos = self.cells[to_cell]
            safe_z = tpos.get('z', 0.0) + 10.0
            await self.driver.move_absolute(tpos['x'], tpos['y'], safe_z, self.default_speed)
            await asyncio.sleep(0.02)
            await self.place_card_to_cell(to_cell)
            end = time.time()
            LOG.info("Transfer %s -> %s took %.3fs", from_cell, to_cell, end - start)
            return {"from": from_cell, "to": to_cell, "duration_s": end - start, "current_pos": self.current}

# convenience singleton used by endpoints
_controller: Optional[MotionController] = None

def get_controller() -> MotionController:
    global _controller
    if _controller is None:
        _controller = MotionController()
    return _controller

# helper to wire cells from config YAML/dict
def configure_from_cfg(cfg: Dict[str, Any]) -> None:
    """
    cfg expected to contain 'cells' mapping: cell_id -> {x,y,z}
    """
    ctrl = get_controller()
    cells = {}
    # support both list of cell dicts and mapping
    if isinstance(cfg.get("cells"), dict):
        for cid, v in cfg["cells"].items():
            cells[cid] = {'x': v.get('x', 0.0), 'y': v.get('y', 0.0), 'z': v.get('z', 0.0)}
    else:
        # list of {id,x,y,z}
        for item in cfg.get("cells", []):
            cid = item.get("id") or item.get("cell") or item.get("name")
            if not cid:
                continue
            cells[cid] = {'x': item.get("x", 0.0), 'y': item.get("y", 0.0), 'z': item.get("z", 0.0)}
    ctrl.configure_cells(cells)