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
from typing import Dict, Any, Optional, Tuple, List
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
    async def move_until_limit(self, axis: str, direction: int, speed: float) -> float:
        """
        Move continuously along `axis` ('x'|'y'|'z') in `direction` (-1 or 1)
        until a limit switch is hit. Returns the final coordinate along that axis.
        Drivers for real hardware should implement this using their limit switch
        inputs. The simulated driver will clamp to 0.0 for negative travel.
        """
        raise NotImplementedError()
    async def send_gcode(self, cmd: str, wait_ok: bool = True, timeout: float = 2.0) -> List[str]:
        """Send a raw G-code command to the driver and return response lines.
        Drivers that communicate over serial should implement this and block until
        the firmware acknowledges (or timeout)."""
        raise NotImplementedError()

    async def query_position(self) -> Tuple[float, float, float]:
        """Query the driver/firmware for its current position (X,Y,Z) and return
        a tuple of floats. Typical implementations use M114 (Marlin) or ?/status
        (GRBL) and parse the response."""
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

    async def move_until_limit(self, axis: str, direction: int, speed: float) -> float:
        """Simulate moving until a limit switch is hit.
        For negative direction we clamp the coordinate to 0.0 (home).
        For positive direction we simulate a large travel (e.g., +1000.0).
        """
        assert axis in ('x', 'y', 'z')
        assert direction in (-1, 1)
        # current coordinates
        cur_x, cur_y, cur_z = self.pos
        coord = {'x': cur_x, 'y': cur_y, 'z': cur_z}[axis]
        if direction < 0:
            target = 0.0
        else:
            # simulate an upper travel limit far away
            target = coord + 1000.0
        dist = abs(coord - target)
        duration = max(0.02, dist / max(1.0, speed/100.0))
        LOG.info("Simulated move_until_limit axis=%s dir=%s -> target=%.2f (t=%.2fs)", axis, direction, target, duration)
        await asyncio.sleep(duration)
        # update position
        if axis == 'x':
            self.pos = (target, cur_y, cur_z)
        elif axis == 'y':
            self.pos = (cur_x, target, cur_z)
        else:
            self.pos = (cur_x, cur_y, target)
        return getattr(self, 'pos')[('x','y','z').index(axis)]

    async def send_gcode(self, cmd: str, wait_ok: bool = True, timeout: float = 2.0) -> List[str]:
        LOG.info("Simulated send_gcode: %s", cmd.strip())
        # simple simulation: return ok
        await asyncio.sleep(0.01)
        return ["ok"]

    async def query_position(self) -> Tuple[float, float, float]:
        return self.pos


class GCodeDriver(MotionDriver):
    """Driver that communicates with firmware over a serial port by sending G-code.

    Basic behavior:
      - send_gcode writes a command and reads lines until 'ok' or timeout
      - query_position sends M114 and parses X/Y/Z when available
      - high-level methods map to G-code sequences (move_absolute uses G1)

    This implementation is a minimal, configurable template and may need tuning
    for specific firmware (GRBL/Marlin/Smoothie). It performs blocking serial IO
    inside a ThreadPoolExecutor to avoid blocking the asyncio loop.
    """
    def __init__(self, port: str = '/dev/ttyUSB0', baud: int = 115200, mcodes: Optional[Dict[str, str]] = None, feedrates: Optional[Dict[str, float]] = None):
        self.port = port
        self.baud = int(baud)
        self.mcodes = mcodes or {}
        self.feedrates = feedrates or {}
        self._serial = None
        # Don't capture an event loop at construction time; async methods will
        # obtain the running loop dynamically. Capturing a loop here can fail
        # when called from non-async threads.
        self._loop = None

    def _ensure_serial(self):
        if self._serial:
            return
        try:
            import serial
        except Exception as exc:
            raise RuntimeError("pyserial is required for GCodeDriver: install pyserial") from exc
        self._serial = serial.Serial(self.port, self.baud, timeout=0.1)

    def _read_lines_blocking(self, timeout: float) -> List[str]:
        lines = []
        if not self._serial:
            return lines
        deadline = time.time() + timeout
        buff = b""
        while time.time() < deadline:
            data = self._serial.read(1024)
            if data:
                buff += data
                while b"\n" in buff:
                    line, buff = buff.split(b"\n", 1)
                    text = line.decode('utf8', errors='ignore').strip()
                    if text:
                        lines.append(text)
                        if text.lower().startswith('ok') or text.lower().startswith('error') or text.lower().startswith('alarm'):
                            return lines
            else:
                time.sleep(0.01)
        return lines

    def _write_blocking(self, cmd: str) -> None:
        if not self._serial:
            return
        data = (cmd.rstrip() + '\n').encode('utf8')
        self._serial.write(data)
        self._serial.flush()

    async def send_gcode(self, cmd: str, wait_ok: bool = True, timeout: float = 2.0) -> List[str]:
        self._ensure_serial()
        loop = asyncio.get_running_loop()
        try:
            LOG.info("GCodeDriver -> %s", cmd.strip())
            # run blocking write/read in threadpool
            await loop.run_in_executor(None, self._write_blocking, cmd)
            if not wait_ok:
                return []
            lines = await loop.run_in_executor(None, self._read_lines_blocking, timeout)
            if lines:
                LOG.info("GCodeDriver <- %s", " | ".join(lines))
            else:
                LOG.info("GCodeDriver <- (no response)")
            return lines
        except Exception as exc:
            LOG.exception("GCodeDriver send_gcode failed for cmd=%s: %s", cmd.strip(), exc)
            raise

    async def query_position(self) -> Tuple[float, float, float]:
        # send M114 and parse 'X:.. Y:.. Z:..'
        lines = await self.send_gcode('M114', wait_ok=True, timeout=1.0)
        x = y = z = 0.0
        for ln in lines:
            # example Marlin response: 'X:1.23 Y:4.56 Z:7.89 E:0.00 Count X:...'
            try:
                parts = ln.replace(',', ' ').split()
                for p in parts:
                    if p.startswith('X:'):
                        x = float(p.split(':',1)[1])
                    elif p.startswith('Y:'):
                        y = float(p.split(':',1)[1])
                    elif p.startswith('Z:'):
                        z = float(p.split(':',1)[1])
            except Exception:
                continue
        return (x, y, z)

    async def move_absolute(self, x: float, y: float, z: float, speed: float) -> None:
        # ensure absolute mode and send G1 with feedrate in mm/min
        feed = int(speed)
        cmd = f'G90\nG1 X{float(x):.3f} Y{float(y):.3f} Z{float(z):.3f} F{feed}'
        await self.send_gcode(cmd, wait_ok=True, timeout=5.0)

    async def set_speed(self, speed: float) -> None:
        # store as feedrate; not all firmwares support a global feedrate set command
        self.feedrates['current'] = float(speed)

    async def vacuum_on(self) -> None:
        cmd = self.mcodes.get('vacuum_on', 'M100')
        await self.send_gcode(cmd)

    async def vacuum_off(self) -> None:
        cmd = self.mcodes.get('vacuum_off', 'M101')
        await self.send_gcode(cmd)

    async def plunger_down(self) -> None:
        cmd = self.mcodes.get('plunger_down', 'M110')
        await self.send_gcode(cmd)

    async def plunger_up(self) -> None:
        cmd = self.mcodes.get('plunger_up', 'M111')
        await self.send_gcode(cmd)

    async def stop(self) -> None:
        # send feedhold (soft stop) if supported; GRBL: '!' for feed hold, or use ctrl-x
        try:
            await self.send_gcode('!')
        except Exception:
            pass

    async def home_all(self) -> None:
        # G28 is common; some firmwares accept $H
        try:
            await self.send_gcode('G28')
        except Exception:
            await self.send_gcode('$H')

    async def move_until_limit(self, axis: str, direction: int, speed: float) -> float:
        # Move via small incremental relative steps while querying limit status via M119 or firmware-specific
        # This is a best-effort: many firmwares do not expose live limit status over G-code; prefer homing.
        # We'll perform a loop of small relative moves and query position.
        step = 1.0 * (1 if direction > 0 else -1)
        await self.send_gcode('G91')
        try:
            for _ in range(2000):
                await self.send_gcode(f'G1 {axis.upper()}{step:.3f} F{int(speed)}')
                pos = await self.query_position()
                # naive limit detection: if position did not change in the intended direction, assume limit
                coord = pos[('x','y','z').index(axis)]
                # no robust detection available here; return current pos
                return coord
        finally:
            await self.send_gcode('G90')
        return 0.0


class LoggingDriver(SimulatedDriver):
    """Driver used in demo mode: logs the G-code that would be sent and simulates responses.

    This is a friendly, safe simulation that prints the commands to stdout (so they
    appear in the server terminal) and otherwise behaves like the SimulatedDriver.
    """
    def __init__(self):
        super().__init__()

    async def send_gcode(self, cmd: str, wait_ok: bool = True, timeout: float = 2.0) -> List[str]:
        # Print to stdout for easy terminal visibility and also log
        s = cmd.strip()
        print(f"[DEMO GCODE] {s}")
        LOG.info("Demo GCODE: %s", s)
        # keep simulation small delay
        await asyncio.sleep(0.01)
        return ["ok"]

    async def move_absolute(self, x: float, y: float, z: float, speed: float) -> None:
        # Log the G-code that would be used for a move
        cmd = f'G90\nG1 X{float(x):.3f} Y{float(y):.3f} Z{float(z):.3f} F{int(speed)}'
        await self.send_gcode(cmd)
        # update simulated position
        self.pos = (x, y, z)

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
        self.pick_clearance = 12.0
        self.pick_retract_height = 15.0
        self.pick_depth_offset = -5.0
        self.place_clearance = 12.0
        self.place_drop_offset = -5.0
        self.return_clearance = 18.0

    def configure_cells(self, cells: Dict[str, Dict[str, float]]) -> None:
        """
        cells: mapping cell_id -> {'x':float,'y':float,'z':float}
        """
        self.cells = {k: {'x': float(v['x']), 'y': float(v['y']), 'z': float(v.get('z', 0.0))} for k, v in cells.items()}
        LOG.info("Configured %d cells", len(self.cells))

    async def home_all(self) -> None:
        """Home all axes and finish at the configured A1 reference if available."""
        await self.home_to_a1()

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

    async def move_to_cell_xy(self, cell_id: str, speed: Optional[float] = None) -> None:
        """
        Move to the XY coordinates of a named cell without changing the Z axis.
        This is safe for demos: it will NOT activate vacuum or plunger.
        """
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell {cell_id}")
        pos = self.cells[cell_id]
        # preserve current z
        x, y = float(pos['x']), float(pos['y'])
        z = float(self.current[2]) if self.current is not None else pos.get('z', 0.0)
        sp = speed or self.default_speed
        async with self.lock:
            await self.driver.set_speed(sp)
            await self.driver.move_absolute(x, y, z, sp)
            self.current = (x, y, z)
            LOG.info("Moved (XY) to cell %s -> %s", cell_id, self.current)

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

    async def home_to_a1(self, a1_pos: Optional[Tuple[float,float,float]] = None) -> None:
        """Home all axes and park at cell A1 before resuming operations."""
        target = a1_pos or self._lookup_cell_position('A1')
        if self.lock.locked():
            await self._run_home_sequence_locked(target)
        else:
            async with self.lock:
                await self._run_home_sequence_locked(target)

    def _lookup_cell_position(self, cell_id: str) -> Optional[Tuple[float, float, float]]:
        pos = self.cells.get(cell_id)
        if not pos:
            return None
        return (
            float(pos.get('x', 0.0)),
            float(pos.get('y', 0.0)),
            float(pos.get('z', 0.0)),
        )

    async def _run_home_sequence_locked(self, target: Optional[Tuple[float, float, float]]) -> None:
        LOG.info("Starting homing routine")
        await self.driver.home_all()
        self.homed = True
        self.current = (0.0, 0.0, 0.0)
        LOG.info("Driver reported home; current reset to %s", self.current)

        if target:
            LOG.info("Moving to reference cell at %s after homing", target)
            await self.driver.set_speed(self.default_speed)
            await self.driver.move_absolute(target[0], target[1], self.current[2], self.default_speed)
            self.current = (target[0], target[1], self.current[2])

        z_zero = await self._calibrate_z_with_plunger_locked()

        if target:
            final_z = target[2]
        elif z_zero is not None:
            final_z = z_zero
        else:
            final_z = 0.0

        if z_zero is not None:
            LOG.info("Z calibration complete; zero captured at %.4f", z_zero)
        else:
            LOG.info("Z calibration skipped; using configured height %.4f", final_z)

        if abs(self.current[2] - final_z) > 1e-6:
            await self.driver.move_absolute(self.current[0], self.current[1], final_z, self.default_speed)

        self.current = (self.current[0], self.current[1], final_z)
        LOG.info("Homing routine finished; current position %s", self.current)

    async def _calibrate_z_with_plunger_locked(self) -> Optional[float]:
        if not hasattr(self.driver, 'plunger_down') or not hasattr(self.driver, 'plunger_up'):
            LOG.info("Driver lacks plunger control; skipping Z calibration")
            return None

        LOG.info("Calibrating Z using plunger stroke")
        z_limit: Optional[float] = None
        plunger_engaged = False

        try:
            await self.driver.plunger_down()
            plunger_engaged = True
            await asyncio.sleep(0.1)

            if hasattr(self.driver, 'move_until_limit'):
                try:
                    z_limit = await self.driver.move_until_limit('z', -1, self.default_speed / 2)
                    LOG.info("Detected Z limit at %.4f via move_until_limit", z_limit)
                except Exception as exc:
                    LOG.warning("move_until_limit('z') failed: %s", exc)

            if z_limit is None and hasattr(self.driver, 'query_position'):
                try:
                    pos = await self.driver.query_position()
                    z_limit = float(pos[2])
                    LOG.info("Sampled Z position %.4f after plunger stroke", z_limit)
                except Exception as exc:
                    LOG.warning("query_position during Z calibration failed: %s", exc)
        finally:
            if plunger_engaged:
                try:
                    await self.driver.plunger_up()
                except Exception as exc:
                    LOG.warning("Failed to retract plunger after calibration: %s", exc)

        if z_limit is None:
            LOG.info("Unable to determine Z limit; skipping zeroing")
            return None

        if hasattr(self.driver, 'send_gcode'):
            try:
                await self.driver.send_gcode('G92 Z0')
                z_limit = 0.0
                LOG.info("Issued G92 Z0 to zero Z axis")
            except Exception as exc:
                LOG.warning("Failed to zero Z axis: %s", exc)

        return z_limit

    def _cell_coords(self, cell_id: str) -> Tuple[float, float, float]:
        pos = self.cells[cell_id]
        return (float(pos.get('x', 0.0)), float(pos.get('y', 0.0)), float(pos.get('z', 0.0)))

    async def _move_head_locked(self, x: float, y: float, z: float, speed: Optional[float] = None) -> None:
        sp = float(speed or self.default_speed)
        await self.driver.set_speed(sp)
        await self.driver.move_absolute(x, y, z, sp)
        self.current = (x, y, z)

    async def _move_to_cell_safe_locked(self, cell_id: str, clearance: float) -> Tuple[float, float, float]:
        x, y, base_z = self._cell_coords(cell_id)
        target_safe = base_z + clearance
        cur_x, cur_y, cur_z = self.current
        travel_z = max(cur_z, target_safe, self.return_clearance)
        if cur_z < travel_z - 1e-6:
            await self._move_head_locked(cur_x, cur_y, travel_z, self.default_speed)
        if abs(cur_x - x) > 1e-6 or abs(cur_y - y) > 1e-6:
            await self._move_head_locked(x, y, travel_z, self.default_speed)
        if abs(travel_z - target_safe) > 1e-6:
            await self._move_head_locked(x, y, target_safe, self.default_speed)
        return (x, y, target_safe)

    async def _lower_z_until_limit_locked(self, speed: float) -> Optional[float]:
        if not hasattr(self.driver, 'move_until_limit'):
            return None
        try:
            await self.driver.set_speed(speed)
            z_val = await self.driver.move_until_limit('z', -1, speed)
            try:
                z_float = float(z_val)
            except Exception:
                z_float = float(self.current[2])
            self.current = (self.current[0], self.current[1], z_float)
            return z_float
        except Exception as exc:
            LOG.warning("move_until_limit('z') failed: %s", exc)
            return None

    async def _pick_card_from_cell_locked(self, cell_id: str, pick_z_offset: float) -> None:
        x, y, base_z = self._cell_coords(cell_id)
        if is_demo_mode():
            safe_z = base_z + max(self.pick_clearance, 10.0)
            await self._move_head_locked(x, y, safe_z, self.default_speed)
            try:
                await self.driver.plunger_down()
            except Exception:
                pass
            pick_z = base_z + pick_z_offset
            await self._move_head_locked(x, y, pick_z, self.default_speed / 2)
            try:
                await self.driver.vacuum_on()
            except Exception:
                pass
            await asyncio.sleep(0.06)
            try:
                await self.driver.plunger_up()
            except Exception:
                pass
            await self._move_head_locked(x, y, safe_z, self.default_speed / 2)
            return

        safe_x, safe_y, _ = await self._move_to_cell_safe_locked(cell_id, self.pick_clearance)
        try:
            await self.driver.plunger_down()
        except AttributeError:
            LOG.debug("Driver lacks plunger_down during pick")
        await asyncio.sleep(0.05)
        z_limit = await self._lower_z_until_limit_locked(self.default_speed / 3)
        if z_limit is None:
            drop_z = base_z + pick_z_offset
            await self._move_head_locked(safe_x, safe_y, drop_z, self.default_speed / 4)
            z_limit = drop_z
        if hasattr(self.driver, 'vacuum_on'):
            try:
                await self.driver.vacuum_on()
            except Exception as exc:
                LOG.warning("vacuum_on failed during pick: %s", exc)
        await asyncio.sleep(0.05)
        lift_target = max(z_limit + self.pick_retract_height, self.return_clearance)
        await self._move_head_locked(safe_x, safe_y, lift_target, self.default_speed / 2)
        if hasattr(self.driver, 'plunger_up'):
            try:
                await self.driver.plunger_up()
            except Exception as exc:
                LOG.warning("plunger_up failed after pick: %s", exc)
        self.current = (safe_x, safe_y, lift_target)

    async def _place_card_to_cell_locked(self, cell_id: str, place_z_offset: float) -> None:
        x, y, base_z = self._cell_coords(cell_id)
        if is_demo_mode():
            safe_z = base_z + max(self.place_clearance, 10.0)
            await self._move_head_locked(x, y, safe_z, self.default_speed)
            drop_z = base_z + place_z_offset
            await self._move_head_locked(x, y, drop_z, self.default_speed / 2)
            try:
                await self.driver.vacuum_off()
            except Exception:
                pass
            await asyncio.sleep(0.03)
            try:
                await self.driver.plunger_down()
                await asyncio.sleep(0.03)
            except Exception:
                pass
            try:
                await self.driver.plunger_up()
            except Exception:
                pass
            await self._move_head_locked(x, y, safe_z, self.default_speed / 2)
            return

        safe_x, safe_y, _ = await self._move_to_cell_safe_locked(cell_id, self.place_clearance)
        drop_z = base_z + place_z_offset
        await self._move_head_locked(safe_x, safe_y, drop_z, self.default_speed / 4)
        try:
            await self.driver.plunger_down()
        except AttributeError:
            LOG.debug("Driver lacks plunger_down during place")
        if hasattr(self.driver, 'vacuum_off'):
            try:
                await self.driver.vacuum_off()
            except Exception as exc:
                LOG.warning("vacuum_off failed during place: %s", exc)
        await asyncio.sleep(0.05)
        if hasattr(self.driver, 'plunger_up'):
            try:
                await self.driver.plunger_up()
            except Exception as exc:
                LOG.warning("plunger_up failed after place: %s", exc)
        lift_target = max(base_z + self.place_clearance, self.return_clearance)
        await self._move_head_locked(safe_x, safe_y, lift_target, self.default_speed / 2)
        self.current = (safe_x, safe_y, lift_target)

    async def pick_card_from_cell(self, cell_id: str, pick_z_offset: Optional[float] = None) -> None:
        """Execute the pick routine for a feeder or storage cell."""
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell {cell_id}")
        offset = pick_z_offset if pick_z_offset is not None else self.pick_depth_offset
        async with self.lock:
            await self._pick_card_from_cell_locked(cell_id, offset)
        LOG.info("Picked card from %s", cell_id)

    async def place_card_to_cell(self, cell_id: str, place_z_offset: Optional[float] = None) -> None:
        """Execute the placement routine for the destination cell."""
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell %s" % cell_id)
        offset = place_z_offset if place_z_offset is not None else self.place_drop_offset
        async with self.lock:
            await self._place_card_to_cell_locked(cell_id, offset)
        LOG.info("Placed card to %s", cell_id)

    async def transfer_card(
        self,
        from_cell: str,
        to_cell: str,
        pick_z_offset: Optional[float] = None,
        place_z_offset: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Complete pick-and-place operation from from_cell -> to_cell.
        Returns dict with timings and final current position.
        """
        if from_cell not in self.cells or to_cell not in self.cells:
            raise KeyError("Unknown source or target cell")
        async with self.lock:
            start = time.time()
            pick_offset = pick_z_offset if pick_z_offset is not None else self.pick_depth_offset
            place_offset = place_z_offset if place_z_offset is not None else self.place_drop_offset
            await self._pick_card_from_cell_locked(from_cell, pick_offset)
            await self._move_to_cell_safe_locked(to_cell, self.place_clearance)
            await asyncio.sleep(0.02)
            await self._place_card_to_cell_locked(to_cell, place_offset)
            await self._move_to_cell_safe_locked(from_cell, self.pick_clearance)
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
def configure_from_cfg(cfg: Any) -> None:
    """
    Configure the global MotionController from a configuration source.

    `cfg` may be either:
      - a raw dict loaded from YAML (mapping with a 'cells' section), or
      - a typed Config dataclass returned by `app.services.assign.load_config`.

    The function extracts per-cell x/y/z coordinates and synthesizes a simple
    grid layout for any cells missing explicit coordinates.
    """
    ctrl = get_controller()
    cells: Dict[str, Dict[str, float]] = {}
    # Accept either a raw dict (loaded from YAML) or the Config dataclass returned by load_config
    raw_cells = None
    if isinstance(cfg, dict):
        raw_cells = cfg.get("cells")
    else:
        # dataclass from assign.load_config stores cells as a mapping id->Cell
        if hasattr(cfg, 'cells') and isinstance(getattr(cfg, 'cells'), dict):
            raw_cells = []
            for cid, cellobj in getattr(cfg, 'cells').items():
                # cellobj may be a dataclass with no x/y/z; default to 0
                raw_cells.append({
                    'id': cid,
                    'x': getattr(cellobj, 'x', 0.0) or 0.0,
                    'y': getattr(cellobj, 'y', 0.0) or 0.0,
                    'z': getattr(cellobj, 'z', 0.0) or 0.0,
                })
        else:
            # Fallback: try dict-like get
            try:
                raw_cells = cfg.get("cells", [])
            except Exception:
                raw_cells = []

    # raw_cells may be either a mapping or a list
    if isinstance(raw_cells, dict):
        for cid, v in raw_cells.items():
            cells[cid] = {'x': float(v.get('x', 0.0)), 'y': float(v.get('y', 0.0)), 'z': float(v.get('z', 0.0))}
    else:
        for item in (raw_cells or []):
            cid = item.get("id") or item.get("cell") or item.get("name")
            if not cid:
                continue
            cells[cid] = {'x': float(item.get("x", 0.0)), 'y': float(item.get("y", 0.0)), 'z': float(item.get("z", 0.0))}

    # If cells have no explicit x/y coordinates, fill a simple grid layout based on the cell id
    # e.g., A1 -> x=0, y=0; B1 -> x=spacing, y=0; A2 -> x=0, y=spacing
    spacing_x = 25.0
    spacing_y = 25.0
    filled: Dict[str, Dict[str, float]] = {}
    for cid, pos in cells.items():
        x = float(pos.get('x', 0.0))
        y = float(pos.get('y', 0.0))
        z = float(pos.get('z', 0.0))
        # detect if coordinates likely missing (both zero)
        if x == 0.0 and y == 0.0:
            # parse letter prefix and numeric suffix
            letters = ''.join([ch for ch in cid if ch.isalpha()]) or 'A'
            nums = ''.join([ch for ch in cid if ch.isdigit()]) or '1'
            # column index: A->0, B->1, ... support multi-letter like 'AA'
            col_index = 0
            for ch in letters.upper():
                col_index = col_index * 26 + (ord(ch) - ord('A'))
            try:
                row_index = max(0, int(nums) - 1)
            except Exception:
                row_index = 0
            x = col_index * spacing_x
            y = row_index * spacing_y
        filled[cid] = {'x': x, 'y': y, 'z': z}

    ctrl.configure_cells(filled)


# Demo mode helper: swap controller driver to a logging/simulated driver
_demo_mode = False

def set_demo_mode(enabled: bool, gcode_opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Toggle demo mode.

    Returns a dict with keys:
      - ok: bool
      - driver: name of the driver that was selected
      - error: optional error message if driver creation/validation failed

    Behavior: when enabled, controller.driver is set to LoggingDriver. When
    disabled, prefer a GCodeDriver if gcode_opts provided and it validates; on
    failure fall back to SimulatedDriver and return the diagnostic message.
    """
    global _demo_mode
    ctrl = get_controller()
    if enabled:
        LOG.info("Switching to demo LoggingDriver")
        ctrl.driver = LoggingDriver()
        _demo_mode = True
        return {"ok": True, "driver": type(ctrl.driver).__name__}

    # disable demo: prefer a real GCodeDriver if options provided, else SimulatedDriver
    if gcode_opts and isinstance(gcode_opts, dict):
        try:
            port = gcode_opts.get('port', '/dev/ttyUSB0')
            baud = int(gcode_opts.get('baud', 115200))
            mcodes = gcode_opts.get('mcodes')
            feedrates = gcode_opts.get('feedrates')
            # Instantiate the driver object
            gd = GCodeDriver(port=port, baud=baud, mcodes=mcodes, feedrates=feedrates)
            # Attempt to open the serial port and do a light validation handshake
            try:
                # try to open the serial port (may raise if pyserial missing or permission denied)
                gd._ensure_serial()
                # send a basic firmware query (M115) and read any immediate lines to validate presence
                try:
                    gd._write_blocking('M115')
                    lines = gd._read_lines_blocking(0.5)
                except Exception:
                    # If write/read fails, try a small pause and a read to capture any boot banner
                    time.sleep(0.05)
                    lines = gd._read_lines_blocking(0.2)
                LOG.info("GCodeDriver probe lines=%s", lines)
                # parse simple firmware banner heuristics
                fw = None
                for ln in lines:
                    low = ln.lower()
                    if 'firmware' in low or 'marlin' in low or 'grbl' in low or 'smoothie' in low:
                        fw = ln
                        break
                fw_summary = fw or (', '.join(lines) if lines else None)
            except Exception as e:
                # failed to open/validate serial device
                LOG.exception("GCodeDriver serial probe failed: %s", e)
                ctrl.driver = SimulatedDriver()
                _demo_mode = False
                return {"ok": False, "driver": type(ctrl.driver).__name__, "error": f"Serial probe failed: {str(e)}"}

            # If we got this far, consider the driver usable
            ctrl.driver = gd
            LOG.info("Switched to GCodeDriver port=%s baud=%s", port, baud)
            _demo_mode = False
            return {"ok": True, "driver": type(ctrl.driver).__name__, "firmware": fw_summary}
        except Exception as exc:
            LOG.exception("Failed to create GCodeDriver, falling back to SimulatedDriver: %s", exc)
            ctrl.driver = SimulatedDriver()
            _demo_mode = False
            return {"ok": False, "driver": type(ctrl.driver).__name__, "error": str(exc)}

    ctrl.driver = SimulatedDriver()
    _demo_mode = False
    return {"ok": True, "driver": type(ctrl.driver).__name__}


def is_demo_mode() -> bool:
    return bool(_demo_mode)


def get_driver_name() -> str:
    return type(get_controller().driver).__name__


def render_gcode_for_cell(cell_id: str, action: str = 'pick', pick_z_offset: float = -5.0, safe_z_offset: float = 10.0, feed_travel: Optional[float] = None, feed_pick: Optional[float] = None) -> List[str]:
    """Return a list of G-code lines that would perform a move and pick/place for a cell.

    action: 'pick' or 'place' (controls whether we generate pick or place sequence).
    This function does not send commands to hardware; it's purely for preview/testing.
    """
    ctrl = get_controller()
    if cell_id not in ctrl.cells:
        raise KeyError(f"Unknown cell {cell_id}")
    pos = ctrl.cells[cell_id]
    x = float(pos.get('x', 0.0))
    y = float(pos.get('y', 0.0))
    z = float(pos.get('z', 0.0))

    # prefer feedrates from controller or provided overrides
    travel_feed = int(feed_travel or ctrl.default_speed)
    pick_feed = int(feed_pick or max(80, ctrl.default_speed // 2))

    # find M-code names from driver if available
    drv = ctrl.driver
    mc_vac_on = getattr(drv, 'mcodes', {}).get('vacuum_on', 'M100') if hasattr(drv, 'mcodes') else 'M100'
    mc_vac_off = getattr(drv, 'mcodes', {}).get('vacuum_off', 'M101') if hasattr(drv, 'mcodes') else 'M101'
    mc_plunge = getattr(drv, 'mcodes', {}).get('plunger_down', 'M110') if hasattr(drv, 'mcodes') else 'M110'
    mc_plunge_up = getattr(drv, 'mcodes', {}).get('plunger_up', 'M111') if hasattr(drv, 'mcodes') else 'M111'

    lines: List[str] = []
    lines.append('; Simulated G-code for action=%s cell=%s' % (action, cell_id))
    lines.append('G21')
    lines.append('G90')
    safe_z = z + safe_z_offset
    if action == 'pick':
        # go above cell
        lines.append(f'G1 X{float(x):.3f} Y{float(y):.3f} Z{float(safe_z):.3f} F{travel_feed}')
        # plunger down
        lines.append(mc_plunge)
        # lower to pick depth
        pick_z = z + pick_z_offset
        lines.append(f'G1 Z{float(pick_z):.3f} F{pick_feed}')
        # vacuum on
        lines.append(mc_vac_on)
        # dwell briefly
        lines.append('G4 P0.05')
        # plunger up
        lines.append(mc_plunge_up)
        # retract
        lines.append(f'G1 Z{float(safe_z):.3f} F{travel_feed}')
    else:
        # place
        lines.append(f'G1 X{float(x):.3f} Y{float(y):.3f} Z{float(safe_z):.3f} F{travel_feed}')
        place_z = z + pick_z_offset
        lines.append(f'G1 Z{float(place_z):.3f} F{pick_feed}')
        lines.append(mc_vac_off)
        lines.append('G4 P0.03')
        lines.append(f'G1 Z{float(safe_z):.3f} F{travel_feed}')

    return lines