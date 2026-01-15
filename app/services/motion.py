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
import logging
import math
import os
import time

LOG = logging.getLogger("sort.motion")
logging.basicConfig(level=logging.DEBUG)

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

    async def extrude(self, amount_mm: float, feed: float = 50.0) -> None:
        """Extrude or retract filament/plunger by amount_mm (positive extrude, negative retract).
        feed is in mm/min. Drivers that support an E axis should implement this.
        """
        raise NotImplementedError()

    async def query_position(self) -> Tuple[float, float, float]:
        """Query the driver/firmware for its current position (X,Y,Z) and return
        a tuple of floats. Typical implementations use M114 (Marlin) or ?/status
        (GRBL) and parse the response."""
        raise NotImplementedError()



class VirtualMotionDriver(MotionDriver):
    """In-memory driver used when no hardware is available."""

    LIMITS: Dict[str, Tuple[float, float]] = {
        "x": (0.0, 920.0),
        "y": (0.0, 320.0),
        "z": (0.0, 250.0),
    }

    def __init__(self) -> None:
        self.port = "virtual"
        self.mcodes: Dict[str, str] = {
            # vacuum mcodes removed
            "plunger_down": "M110",
            "plunger_up": "M111",
        }
        self.feedrates: Dict[str, float] = {}
        self._position: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._speed: float = 800.0
        self._vacuum: bool = False
        self._extruded_total: float = 0.0
        self._plunger: str = "up"
        LOG.info("VirtualMotionDriver initialised")

    async def move_absolute(self, x: float, y: float, z: float, speed: float) -> None:
        target = (float(x), float(y), float(z))
        self._speed = float(speed)
        dist = math.sqrt(sum((target[i] - self._position[i]) ** 2 for i in range(3)))
        await asyncio.sleep(min(0.01 + dist / max(self._speed, 1.0), 0.1))
        self._position = target
        LOG.debug("Virtual driver move -> %s", self._position)

    async def set_speed(self, speed: float) -> None:
        self._speed = float(speed)

    async def vacuum_on(self) -> None:
        self._vacuum = True

    async def vacuum_off(self) -> None:
        self._vacuum = False

    async def plunger_down(self) -> None:
        self._plunger = "down"

    async def plunger_up(self) -> None:
        self._plunger = "up"

    async def stop(self) -> None:
        LOG.debug("Virtual driver stop")

    async def home_all(self) -> None:
        await asyncio.sleep(0.05)
        self._position = (0.0, 0.0, 0.0)

    async def move_until_limit(self, axis: str, direction: int, speed: float) -> float:
        axis = axis.lower()
        low, high = self.LIMITS.get(axis, (0.0, 0.0))
        idx = {"x": 0, "y": 1, "z": 2}.get(axis, 0)
        target = low if direction < 0 else high
        pos = list(self._position)
        pos[idx] = target
        await asyncio.sleep(0.05)
        self._position = (pos[0], pos[1], pos[2])
        return target

    async def send_gcode(self, cmd: str, wait_ok: bool = True, timeout: float = 2.0) -> List[str]:
        LOG.debug("Virtual driver gcode -> %s", cmd.strip())
        return ["ok"]

    async def extrude(self, amount_mm: float, feed: float = 50.0) -> None:
        # simulate extruder/plunger movement in demo
        try:
            self._extruded_total += float(amount_mm)
            LOG.info("Virtual extrude: %.4f mm @ F%.1f (total %.4f)", amount_mm, float(feed), self._extruded_total)
        except Exception:
            LOG.debug("Invalid extrude arguments")
        return

    async def query_position(self) -> Tuple[float, float, float]:
        return self._position



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
        # Cache last known position when firmware does not report positions
        self._last_position = (0.0, 0.0, 0.0)

    def _ensure_serial(self):
        if self._serial:
            return
        try:
            import serial  # type: ignore[import-not-found]
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

    async def extrude(self, amount_mm: float, feed: float = 50.0) -> None:
        """Send a relative extruder move: G91, G1 E{amount} F{feed}, G90."""
        try:
            amt = float(amount_mm)
            fd = int(feed)
            cmd = f'G91\nG1 E{amt:.4f} F{fd}\nG90'
            LOG.info("GCodeDriver extrude -> %s", cmd.replace('\n', ' | '))
            await self.send_gcode(cmd, wait_ok=True, timeout=2.0)
        except Exception:
            LOG.exception("extrude failed for amount=%s feed=%s", amount_mm, feed)
            raise

    async def query_position(self) -> Tuple[float, float, float]:
        # send M114 and parse 'X:.. Y:.. Z:..' but ignore step counters after "Count"
        lines = await self.send_gcode('M114', wait_ok=True, timeout=1.0)
        x = y = z = 0.0
        found = False
        for ln in lines:
            # example Marlin response: 'X:1.23 Y:4.56 Z:7.89 E:0.00 Count X:...'
            # Split at "Count" to ignore step counter values that can corrupt position
            actual_position_part = ln.split('Count')[0] if 'Count' in ln else ln
            
            # DEBUG: Add detailed logging of position parsing
            LOG.debug("Position parsing - Raw line: %r", ln)
            LOG.debug("Position parsing - After Count split: %r", actual_position_part)
            
            try:
                parts = actual_position_part.replace(',', ' ').split()
                LOG.debug("Position parsing - Parts: %s", parts)
                for p in parts:
                    if p.startswith('X:') and x == 0.0:  # Only parse first occurrence
                        x = float(p.split(':',1)[1])
                        found = True
                        LOG.debug("Position parsing - Found X: %.3f", x)
                    elif p.startswith('Y:') and y == 0.0:  # Only parse first occurrence
                        y = float(p.split(':',1)[1])
                        found = True
                        LOG.debug("Position parsing - Found Y: %.3f", y)
                    elif p.startswith('Z:') and z == 0.0:  # Only parse first occurrence
                        z = float(p.split(':',1)[1])
                        found = True
                        LOG.debug("Position parsing - Found Z: %.3f", z)
            except Exception as e:
                LOG.debug("Position parsing - Parse error on line %r: %s", ln, e)
                continue
        
        LOG.debug("Position parsing - Final result: (%.3f, %.3f, %.3f) found=%s", x, y, z, found)
        if not found:
            # Firmware did not include X/Y/Z in the response; return last
            # commanded position so UI and callers see movement when firmware
            # suppresses position reporting.
            LOG.warning("query_position: no X/Y/Z tokens in driver response; returning cached position %s", self._last_position)
            return self._last_position
        return (x, y, z)

    async def move_absolute(self, x: float, y: float, z: float, speed: float) -> None:
        # ensure absolute mode and send G1 with feedrate in mm/min
        feed = int(speed)
        cmd = f'G90\nG1 X{float(x):.3f} Y{float(y):.3f} Z{float(z):.3f} F{feed}'
        await self.send_gcode(cmd, wait_ok=True, timeout=5.0)
        # Cache the last commanded position so callers can fall back when
        # position reporting via M114 is unavailable or unparseable.
        try:
            self._last_position = (float(x), float(y), float(z))
        except Exception:
            pass

    async def set_speed(self, speed: float) -> None:
        # store as feedrate; not all firmwares support a global feedrate set command
        self.feedrates['current'] = float(speed)

    async def vacuum_on(self) -> None:
        # Vacuum/extruder test removed: do not send any commands here.
        LOG.info("vacuum_on called but vacuum features removed by configuration")
        return

    async def vacuum_off(self) -> None:
        # Vacuum/extruder test removed: do not send any commands here.
        LOG.info("vacuum_off called but vacuum features removed by configuration")
        return

    async def plunger_down(self) -> None:
        cmd = str((self.mcodes or {}).get('plunger_down', 'M110')).strip()
        await self.send_gcode(cmd)

    async def plunger_up(self) -> None:
        cmd = str((self.mcodes or {}).get('plunger_up', 'M111')).strip()
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
        """
        Move along an axis until a limit switch is triggered.
        Uses small incremental moves with both position feedback and limit switch checking.
        """
        axis_upper = axis.upper()
        step = 0.5 * (1 if direction > 0 else -1)  # Smaller steps for better detection
        
        LOG.info("Moving until limit: axis=%s, direction=%d, speed=%.1f", axis_upper, direction, speed)
        
        # Switch to relative positioning mode
        await self.send_gcode('G91')
        
        try:
            last_pos = None
            stuck_count = 0
            max_steps = 4000  # More steps with smaller step size
            
            for step_num in range(max_steps):
                # Check limit switches before each move
                try:
                    switches = await self.get_limit_switch_status()  # type: ignore[attr-defined]
                    axis_lower = axis.lower()
                    
                    # Check if we've hit the target limit switch
                    if direction < 0:  # Moving toward min limit
                        if switches.get(f'{axis_lower}_min', False):
                            LOG.info("Limit switch %s_min triggered at step %d", axis_lower, step_num)
                            pos = await self.query_position()
                            return pos[{'x': 0, 'y': 1, 'z': 2}[axis_lower]]
                    else:  # Moving toward max limit
                        if switches.get(f'{axis_lower}_max', False):
                            LOG.info("Limit switch %s_max triggered at step %d", axis_lower, step_num)
                            pos = await self.query_position()
                            return pos[{'x': 0, 'y': 1, 'z': 2}[axis_lower]]
                except Exception as e:
                    LOG.debug("Limit switch check failed: %s", e)
                
                # Get current position before move
                try:
                    current_pos = await self.query_position()
                    axis_index = {'X': 0, 'Y': 1, 'Z': 2}.get(axis_upper, 0)
                    current_axis_pos = current_pos[axis_index] if current_pos else 0.0
                    
                    # Check if we're stuck (no movement from last position)
                    if last_pos is not None:
                        movement = abs(current_axis_pos - last_pos)
                        if movement < 0.05:  # Less than 0.05mm movement indicates stuck/limit
                            stuck_count += 1
                            if stuck_count >= 5:  # Require 5 consecutive stuck moves
                                LOG.info("Movement blocked at position %.3f (axis %s) - likely hit limit", 
                                        current_axis_pos, axis_upper)
                                return current_axis_pos
                        else:
                            stuck_count = 0
                    
                    last_pos = current_axis_pos
                except Exception:
                    # If position query fails, continue with the move
                    pass
                
                # Make incremental move
                await self.send_gcode(f'G1 {axis_upper}{step:.3f} F{int(speed)}')
                await asyncio.sleep(0.02)  # Shorter delay for faster detection
                
            # If we reach here, we've moved the maximum distance without hitting a limit
            LOG.warning("Reached maximum steps (%d) without detecting limit switch", max_steps)
            try:
                final_pos = await self.query_position()
                axis_index = {'X': 0, 'Y': 1, 'Z': 2}.get(axis_upper, 0)
                return final_pos[axis_index] if final_pos else 0.0
            except Exception:
                return 0.0
            
        finally:
            # Always return to absolute positioning mode
            await self.send_gcode('G90')




class MotionController:
    """
    High level motion controller. Use configure() to provide cell positions:
      { 'A1': {'x':..., 'y':..., 'z':...}, ... }

    Working area: 920mm x 320mm x 250mm (X x Y x Z)
    All coordinates are assumed to be in the same units as the real driver expects.
    """
    def __init__(self, driver: Optional[MotionDriver] = None):
        if driver is None:
            raise ValueError("MotionController requires a driver - no default simulation available")
        self.driver = driver
        self.cells: Dict[str, Dict[str, float]] = {}
        self.current: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.homed = False
        self.default_speed = 800.0  # Default speed in mm/min (configurable)
        self.rapid_speed = 1200.0   # Fast speed for positioning moves
        self.homing_speed = 400.0   # Speed for homing operations
        self.lock = asyncio.Lock()
        self.pick_clearance = 12.0
        self.pick_retract_height = 15.0
        self.pick_depth_offset = -5.0
        self.place_clearance = 12.0
        self.place_drop_offset = -5.0
        self.return_clearance = 18.0

    def configure_cells(self, cells: Dict[str, Dict[str, float]]) -> None:
        """
        cells: mapping cell_id -> {'x':float,'y':float}
        Z coordinates are handled separately by the motion system.
        """
        self.cells = {k: {'x': float(v['x']), 'y': float(v['y'])} for k, v in cells.items()}
        LOG.info("Configured %d cells (XY coordinates only)", len(self.cells))

    async def home_all(self) -> None:
        """Home all axes sequentially using G28 commands: X, then Y, then Z."""
        async with self.lock:
            LOG.info("Starting sequential homing: X -> Y -> Z")
            
            # Home X axis first
            LOG.info("Homing X axis...")
            await self.driver.send_gcode('G28 X')
            LOG.info("X axis homed")
            
            # Home Y axis second  
            LOG.info("Homing Y axis...")
            await self.driver.send_gcode('G28 Y')
            LOG.info("Y axis homed")
            
            # Home Z axis last
            LOG.info("Homing Z axis...")
            await self.driver.send_gcode('G28 Z')
            LOG.info("Z axis homed")
            
            # Mark as fully homed and reset position to (0,0,0)
            self.current = (0.0, 0.0, 0.0)
            self.homed = True
            LOG.info("All axes homed. Position reset to (0.0, 0.0, 0.0)")

    async def move_to_cell(self, cell_id: str, speed: Optional[float] = None) -> None:
        """
        Move to a named cell's XY coordinates. Z position is preserved from current position.
        Raises KeyError if unknown.
        """
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell {cell_id}")
        pos = self.cells[cell_id]
        # Only move in XY plane - preserve current Z position
        target = (pos['x'], pos['y'], self.current[2])
        sp = speed or self.default_speed
        async with self.lock:
            await self.driver.set_speed(sp)
            await self.driver.move_absolute(*target, sp)
            self.current = target
            LOG.info("Moved to cell %s (XY only) -> %s", cell_id, target)

    async def move_to_cell_xy(self, cell_id: str, speed: Optional[float] = None) -> None:
        """
        Move to the XY coordinates of a named cell without changing the Z axis.
        This is safe for demos: it will NOT activate vacuum or plunger.
        Uses rapid_speed by default for faster positioning.
        """
        if cell_id not in self.cells:
            raise KeyError(f"Unknown cell {cell_id}")
        pos = self.cells[cell_id]
        # preserve current z
        x, y = float(pos['x']), float(pos['y'])
        z = float(self.current[2]) if self.current is not None else pos.get('z', 0.0)
        sp = speed or self.rapid_speed  # Use rapid speed for positioning moves
        async with self.lock:
            await self.driver.set_speed(sp)
            await self.driver.move_absolute(x, y, z, sp)
            # Wait for move to complete before releasing lock
            try:
                if hasattr(self.driver, "send_gcode"):
                    await self.driver.send_gcode("M400")  # Wait for moves to complete
                await asyncio.sleep(0.1)  # Small additional delay for stability
            except Exception as exc:
                LOG.debug("M400 wait failed in move_to_cell_xy: %s", exc)
            self.current = (x, y, z)
            LOG.info("Moved (XY) to cell %s -> %s", cell_id, self.current)

    async def jog(self, axis: str, delta: float, speed: Optional[float] = None) -> Tuple[float,float,float]:
        """
        Jog the machine along x/y/z by delta. Returns new position.
        axis: 'x'|'y'|'z'
        Includes limit switch protection to prevent crashes.
        """
        # Normalize axis to lowercase and validate
        axis = str(axis).lower().strip()
        if axis not in ('x','y','z'):
            raise ValueError(f"axis must be 'x','y' or 'z', got '{axis}'")
        
        # Safety limits to prevent runaway (working area: 920x320x250mm)
        MAX_JOG_DISTANCE = 1000.0  # 1000mm max single jog
        if abs(delta) > MAX_JOG_DISTANCE:
            raise ValueError(f"Jog distance {delta:.3f}mm exceeds safety limit {MAX_JOG_DISTANCE}mm")
        
        # CRITICAL: Validate that target position is within reasonable bounds
        # Working area: 920mm x 320mm x 250mm with some margin
        MAX_POSITION = {'x': 1000.0, 'y': 400.0, 'z': 300.0}
        MIN_POSITION = {'x': -50.0, 'y': -50.0, 'z': -50.0}
        
        LOG.info("Jog request: axis=%s, delta=%.3f", axis, delta)
        sp = speed or self.default_speed
        
        async with self.lock:
            # Always sync with hardware position before movement to avoid corruption
            try:
                actual_pos = await self.driver.query_position()
                LOG.debug("Jog sync - Hardware position: (%.3f, %.3f, %.3f)", 
                         actual_pos[0], actual_pos[1], actual_pos[2])
                LOG.debug("Jog sync - Previous stored position: (%.3f, %.3f, %.3f)", 
                         self.current[0], self.current[1], self.current[2])
                self.current = actual_pos
                LOG.info("Synced position with hardware: (%.3f, %.3f, %.3f)", 
                        actual_pos[0], actual_pos[1], actual_pos[2])
            except Exception as e:
                LOG.warning("Failed to query hardware position: %s", e)
            
            x, y, z = self.current
            
            # Calculate final target position for bounds checking (using actual position)
            if axis == 'x':
                final_target_coord = x + delta
            elif axis == 'y':
                final_target_coord = y + delta
            elif axis == 'z':
                final_target_coord = z + delta
                
            # Check bounds before starting movement
            if (final_target_coord > MAX_POSITION[axis] or 
                final_target_coord < MIN_POSITION[axis]):
                raise ValueError(f"Target position {final_target_coord:.3f}mm exceeds bounds "
                               f"[{MIN_POSITION[axis]:.1f}, {MAX_POSITION[axis]:.1f}] for {axis} axis")
            try:
                actual_pos = await self.driver.query_position()
                self.current = actual_pos
                LOG.info("Synced position with hardware: (%.3f, %.3f, %.3f)", 
                        actual_pos[0], actual_pos[1], actual_pos[2])
            except Exception as e:
                LOG.warning("Failed to query hardware position: %s", e)
            
            x, y, z = self.current
            
            # Calculate target position with explicit validation
            if axis == 'x':
                target_x = x + float(delta)
                target_y, target_z = y, z
                LOG.info("X jog: current=%.3f, delta=%.3f, target=%.3f", x, delta, target_x)
            elif axis == 'y':
                target_y = y + float(delta)
                target_x, target_z = x, z
                LOG.info("Y jog: current=%.3f, delta=%.3f, target=%.3f", y, delta, target_y)
            elif axis == 'z':
                target_z = z + float(delta)
                target_x, target_y = x, y
                LOG.info("Z jog: current=%.3f, delta=%.3f, target=%.3f", z, delta, target_z)
            else:
                # This should never happen due to validation above, but extra safety
                raise ValueError(f"Invalid axis in jog calculation: '{axis}'")
            
            # Check limit switches before moving
            try:
                switches = await self.get_limit_switch_status()
                
                # Prevent moves into triggered limit switches
                if axis == 'x':
                    if delta < 0 and switches.get('x_min', False):
                        LOG.warning("Cannot jog X negative - X min limit switch triggered")
                        return self.current
                    if delta > 0 and switches.get('x_max', False):
                        LOG.warning("Cannot jog X positive - X max limit switch triggered")
                        return self.current
                elif axis == 'y':
                    if delta < 0 and switches.get('y_min', False):
                        LOG.warning("Cannot jog Y negative - Y min limit switch triggered")
                        return self.current
                    if delta > 0 and switches.get('y_max', False):
                        LOG.warning("Cannot jog Y positive - Y max limit switch triggered")
                        return self.current
                elif axis == 'z':
                    if delta < 0 and switches.get('z_min', False):
                        LOG.warning("Cannot jog Z negative - Z min limit switch triggered")
                        return self.current
                    if delta > 0 and switches.get('z_max', False):
                        LOG.warning("Cannot jog Z positive - Z max limit switch triggered")
                        return self.current
                        
            except Exception as e:
                LOG.warning("Could not check limit switches before jog: %s", e)
            
            # Perform the move with limit switch monitoring
            await self.driver.set_speed(sp)
            
            # SIMPLIFIED APPROACH: Use single absolute move instead of incremental steps
            # This prevents accumulation errors and integer overflow
            if axis == 'x':
                final_target = (self.current[0] + delta, self.current[1], self.current[2])
            elif axis == 'y':
                final_target = (self.current[0], self.current[1] + delta, self.current[2])
            elif axis == 'z':
                final_target = (self.current[0], self.current[1], self.current[2] + delta)
            else:
                raise ValueError(f"Invalid axis '{axis}' in jog")
            
            LOG.info("Single jog move: %s -> %s", self.current, final_target)
            
            # Make single absolute move to target
            await self.driver.move_absolute(*final_target, sp)
            
            # Update position - query from hardware for accuracy
            try:
                actual_pos = await self.driver.query_position()
                self.current = actual_pos
                LOG.info("Hardware reports position: %s", actual_pos)
            except Exception as e:
                # If position query fails, use calculated target
                self.current = final_target
                LOG.warning("Position query failed, using calculated: %s", final_target)
            
            LOG.info("Jogged %s by %.3f -> pos=%s", axis, delta, self.current)
            return self.current

    async def emergency_stop(self) -> None:
        """Immediately stop all motion and disable motors."""
        try:
            LOG.warning("EMERGENCY STOP activated!")
            await self.driver.stop()
            # Send M112 for emergency stop (Marlin firmware)
            await self.driver.send_gcode('M112', wait_ok=False, timeout=1.0)
        except Exception as e:
            LOG.error("Emergency stop failed: %s", e)

    async def reset_position(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        """Reset the firmware position coordinates to specified values (default 0,0,0)."""
        try:
            LOG.warning("Resetting position to X=%.3f Y=%.3f Z=%.3f", x, y, z)
            # Use G92 to set current position without moving
            await self.driver.send_gcode(f'G92 X{x:.3f} Y{y:.3f} Z{z:.3f}', wait_ok=True, timeout=2.0)
            self.current = (x, y, z)
            LOG.info("Position reset complete")
        except Exception as e:
            LOG.error("Position reset failed: %s", e)
            raise

    # Backwards-compatible adapter methods expected by main.py endpoints
    async def jog_axis(self, axis: str, distance: float, speed: Optional[float] = None) -> Tuple[float,float,float]:
        """Compatibility wrapper for jog_axis calls from main.py"""
        return await self.jog(axis.lower(), distance, speed)

    async def home_x(self) -> None:
        """Home X axis using G28 X command"""
        async with self.lock:
            LOG.info("Homing X axis with G28 X")
            await self.driver.send_gcode('G28 X')
            # Update position - assume it went to X=0 after homing
            self.current = (0.0, self.current[1], self.current[2])
            LOG.info("X axis homed to position: 0.0")

    async def home_y(self) -> None:
        """Home Y axis using G28 Y command"""
        async with self.lock:
            LOG.info("Homing Y axis with G28 Y")
            await self.driver.send_gcode('G28 Y')
            # Update position - assume it went to Y=0 after homing
            self.current = (self.current[0], 0.0, self.current[2])
            LOG.info("Y axis homed to position: 0.0")

    async def home_z(self) -> None:
        """Home Z axis using G28 Z command"""
        async with self.lock:
            LOG.info("Homing Z axis with G28 Z")
            await self.driver.send_gcode('G28 Z')
            # Update position - assume it went to Z=0 after homing
            self.current = (self.current[0], self.current[1], 0.0)
            LOG.info("Z axis homed to position: 0.0")

    async def get_limit_switch_status(self) -> Dict[str, bool]:
        """Query limit switch status using M119"""
        status: Dict[str, bool] = {}
        try:
            lines = await self.driver.send_gcode('M119', wait_ok=True, timeout=1.0)
            for ln in lines:
                low = ln.strip().lower()
                if ':' in low:
                    parts = [p.strip() for p in low.split(':', 1)]
                    if len(parts) == 2:
                        key, val = parts[0], parts[1]
                        triggered = ('trigger' in val) or ('closed' in val)
                        status[key] = bool(triggered)
        except Exception as exc:
            LOG.debug("get_limit_switch_status failed: %s", exc)
        return status

    async def calibrate_routine(self, points: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Any]:
        """
        Run a comprehensive calibration routine:
          1. Home to limit switches (find true mechanical zero)
          2. Allow manual positioning to establish A1 reference
          3. Visit sample points and record actual positions
        Returns calibration results for user confirmation.
        """
        async with self.lock:
            # Step 1: Home to limit switches to find true mechanical zero
            LOG.info("Starting calibration: finding home position using limit switches")
            await self._calibrate_home_with_limits()
            
            # Step 2: A1 reference positioning will be handled by UI
            # The user can manually jog to position A1 and then call save_a1_reference()
            
            # Step 3: Visit sample points if provided
            observed = {}
            if points:
                sample_keys = list(points.keys())
                for k in sample_keys:
                    if k not in self.cells:
                        continue
                    pos = self.cells[k]
                    await self.driver.move_absolute(pos['x'], pos['y'], pos.get('z',0.0), self.default_speed)
                    # small settle
                    await asyncio.sleep(0.05)
                    # read back driver pos if driver exposes it via query_position
                    try:
                        observed[k] = await self.driver.query_position()
                    except Exception:
                        observed[k] = (pos['x'], pos['y'], pos.get('z',0.0))
                    LOG.info("Calib visit %s -> observed %s", k, observed[k])
            
            return {
                "home_position": self.current,
                "observed": observed, 
                "sampled": len(observed),
                "status": "homed_to_limits",
                "message": "Homed to limit switches. Use manual positioning to set A1 reference."
            }

    async def _calibrate_home_with_limits(self) -> None:
        """
        Use limit switches to find the true home position in the top-left corner.
        This moves the device until both X and Y limit switches are triggered.
        """
        LOG.info("Moving to top-left corner using limit switches")
        
        # Use configured homing speed for limit switch approach
        limit_speed = self.homing_speed
        await self.driver.set_speed(limit_speed)
        
        # First, move to X limit (negative direction, left side)
        LOG.info("Finding X limit switch (moving left)")
        x_limit = await self.driver.move_until_limit('x', -1, limit_speed)
        LOG.info("X limit found at position: %.3f", x_limit)
        
        # Then move to Y limit (negative direction, towards front/top)
        LOG.info("Finding Y limit switch (moving forward)")
        y_limit = await self.driver.move_until_limit('y', -1, limit_speed)
        LOG.info("Y limit found at position: %.3f", y_limit)
        
        # Move to Z home if available
        LOG.info("Homing Z axis")
        try:
            await self.driver.send_gcode('G28 Z')
            z_home = 0.0
        except Exception:
            # If Z homing fails, just move to a safe height
            await self.driver.move_absolute(x_limit, y_limit, 10.0, limit_speed)
            z_home = 10.0
        
        # Set our current position to the limit switch positions
        self.current = (x_limit, y_limit, z_home)
        self.homed = True
        
        LOG.info("Limit switch calibration complete. Home position: %.3f, %.3f, %.3f", 
                x_limit, y_limit, z_home)
        
        # Restore normal speed
        await self.driver.set_speed(self.default_speed)

    async def save_a1_reference(self) -> Dict[str, Any]:
        """
        Save the current position as the A1 reference point.
        This should be called after manually positioning the head over cell A1.
        """
        if not self.homed:
            raise ValueError("Must home to limit switches before setting A1 reference")
        
        # Store the current position as A1 (XY only)
        a1_x, a1_y, a1_z = self.current
        
        # Update the A1 cell position (only X and Y)
        self.cells['A1'] = {'x': a1_x, 'y': a1_y}
        
        # Recalculate all other cell positions based on the new A1 reference
        # Working area: 920mm x 320mm x 250mm
        spacing_x = 92.0  # 920mm / 10 intervals (A to K)
        spacing_y = 160.0  # 320mm / 2 intervals (1 to 3)
        
        updated_cells = {}
        for cid, pos in self.cells.items():
            if cid == 'A1':
                updated_cells[cid] = {'x': a1_x, 'y': a1_y}
                continue
                
            # Parse cell ID to determine offset from A1
            letters = ''.join([ch for ch in cid if ch.isalpha()]) or 'A'
            nums = ''.join([ch for ch in cid if ch.isdigit()]) or '1'
            
            # Calculate column and row offsets from A1
            col_index = 0
            for ch in letters.upper():
                col_index = col_index * 26 + (ord(ch) - ord('A'))
            
            try:
                row_index = max(0, int(nums) - 1)
            except Exception:
                row_index = 0
            
            # Calculate new position relative to A1 (XY only)
            new_x = a1_x + (col_index * spacing_x)
            new_y = a1_y + (row_index * spacing_y)
            
            updated_cells[cid] = {'x': new_x, 'y': new_y}
        
        self.cells = updated_cells
        
        LOG.info("A1 reference saved at (%.3f, %.3f, %.3f). Updated %d cell positions (XY only).", 
                a1_x, a1_y, a1_z, len(updated_cells))
        
        return {
            "a1_reference": (a1_x, a1_y, a1_z),
            "cells_updated": len(updated_cells),
            "status": "a1_reference_saved",
            "message": f"A1 reference set to ({a1_x:.3f}, {a1_y:.3f}, {a1_z:.3f}). All cell positions updated (XY only)."
        }

    async def capture_dual_card_photos_no_cell(self, offset_mm: float = 44.0) -> Tuple[float, float, float]:
        """
        Capture dual photos workflow: take photo at current position, move offset_mm in Y direction, take second photo.
        Does NOT move to any cell - just moves from current position.
        
        Args:
            offset_mm: Distance to move in positive Y direction for second photo (default 44mm)
            
        Returns:
            Final position tuple (x, y, z)
        """
        async with self.lock:
            # Get current position - don't move anywhere, just use current position as first photo spot
            current_x, current_y, current_z = self.current
            LOG.info("First photo position: current position (%.1f, %.1f, %.1f)", current_x, current_y, current_z)
            
            # Second position: move offset_mm in positive Y direction only
            new_y = current_y + offset_mm
            
            await self.driver.set_speed(self.default_speed)
            await self.driver.move_absolute(current_x, new_y, current_z, self.default_speed)
            self.current = (current_x, new_y, current_z)
            
            LOG.info("Second photo position reached: moved %.1fmm in Y direction to (%.1f, %.1f, %.1f)", 
                    offset_mm, current_x, new_y, current_z)
            
            return self.current

    async def capture_dual_card_photos(self, cell: str, offset_mm: float = 44.0) -> Tuple[float, float, float]:
        """
        Capture dual photos workflow: take photo at current position, move offset_mm in Y direction, take second photo.
        
        Args:
            cell: Target cell ID (used for reference, but we just move from current position)
            offset_mm: Distance to move in positive Y direction for second photo (default 44mm)
            
        Returns:
            Final position tuple (x, y, z)
        """
        async with self.lock:
            # Get current position - don't move to cell, just use current position as first photo spot
            current_x, current_y, current_z = self.current
            LOG.info("First photo position: current position (%.1f, %.1f, %.1f)", current_x, current_y, current_z)
            
            # Second position: move offset_mm in positive Y direction only
            new_y = current_y + offset_mm
            
            await self.driver.set_speed(self.default_speed)
            await self.driver.move_absolute(current_x, new_y, current_z, self.default_speed)
            self.current = (current_x, new_y, current_z)
            
            LOG.info("Second photo position reached: moved %.1fmm in Y direction to (%.1f, %.1f, %.1f)", 
                    offset_mm, current_x, new_y, current_z)
            
            return self.current

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
        # Cell positions only contain X,Y - Z is handled separately
        return (
            float(pos.get('x', 0.0)),
            float(pos.get('y', 0.0)),
            0.0,  # Default Z position - actual Z is managed separately
        )

    async def _run_home_sequence_locked(self, target: Optional[Tuple[float, float, float]]) -> None:
        LOG.info("Starting homing routine")
        await self.driver.home_all()
        self.homed = True
        self.current = (0.0, 0.0, 0.0)
        LOG.info("Driver reported home; current reset to %s", self.current)

        if target:
            LOG.info("Moving to reference cell at %s after homing", target)
            await self.driver.set_speed(self.rapid_speed)
            await self.driver.move_absolute(target[0], target[1], self.current[2], self.rapid_speed)
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
        # Cell positions only contain X and Y - Z is handled separately by the motion system
        return (float(pos.get('x', 0.0)), float(pos.get('y', 0.0)), 0.0)

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
        # vacuum activation removed
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
        safe_x, safe_y, _ = await self._move_to_cell_safe_locked(cell_id, self.place_clearance)
        drop_z = base_z + place_z_offset
        await self._move_head_locked(safe_x, safe_y, drop_z, self.default_speed / 4)
        try:
            await self.driver.plunger_down()
        except AttributeError:
            LOG.debug("Driver lacks plunger_down during place")
        # vacuum release removed
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
_driver_is_virtual: bool = False

def get_controller() -> MotionController:
    global _controller, _driver_is_virtual
    if _controller is not None:
        return _controller

    config: Dict[str, Any] = {}
    try:
        import yaml  # type: ignore[import-not-found]

        with open('config.yaml', 'r', encoding='utf8') as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        LOG.warning("Unable to read config.yaml (%s); continuing with defaults", exc)
        config = {}

    gcode_opts = config.get('gcode', {}) if isinstance(config, dict) else {}
    configured_port = gcode_opts.get('port') if isinstance(gcode_opts, dict) else None
    baud = int(gcode_opts.get('baud', 115200)) if isinstance(gcode_opts, dict) else 115200
    mcodes = gcode_opts.get('mcodes') if isinstance(gcode_opts, dict) else None
    feedrates = gcode_opts.get('feedrates') if isinstance(gcode_opts, dict) else None
    fallback_ports = gcode_opts.get('fallback_ports', []) if isinstance(gcode_opts, dict) else []

    force_virtual = str(os.environ.get('SORTME_FORCE_VIRTUAL_DRIVER', '')).lower() in {'1', 'true', 'yes'}
    use_fake_hardware = config.get('use_fake_hardware', False) if isinstance(config, dict) else False

    candidate_ports: List[str] = []
    for base in ['/dev/ttyACM0', '/dev/ttyACM1']:
        if base not in candidate_ports:
            candidate_ports.append(base)
    if configured_port and configured_port not in candidate_ports:
        candidate_ports.append(str(configured_port))
    for extra in fallback_ports:
        if extra not in candidate_ports:
            candidate_ports.append(str(extra))

    driver: Optional[MotionDriver] = None
    
    # Use virtual driver if fake hardware mode is enabled
    if use_fake_hardware:
        LOG.info("Using VirtualMotionDriver (fake hardware mode enabled)")
        driver = VirtualMotionDriver()
    elif not force_virtual:
        for port in candidate_ports:
            try:
                if not os.path.exists(port):
                    LOG.debug("Motion port %s not present", port)
                    continue
                test_driver = GCodeDriver(port=port, baud=baud, mcodes=mcodes, feedrates=feedrates)
                test_driver._ensure_serial()
                LOG.info("Connected to motion hardware on %s", port)
                driver = test_driver
                break
            except Exception as exc:
                LOG.warning("Failed to initialise driver on %s: %s", port, exc)
                continue

    if driver is None:
        searched = ", ".join(candidate_ports) or "(none)"
        raise RuntimeError(
            f"Unable to initialise motion hardware; no reachable controller on ports: {searched}"
        )

    _driver_is_virtual = isinstance(driver, VirtualMotionDriver)

    _controller = MotionController(driver)
    return _controller

# helper to wire cells from config YAML/dict
def configure_from_cfg(cfg: Any) -> None:
    """
    Configure the global MotionController from a configuration source.

    `cfg` should be a raw dict loaded from YAML with 'grid' and 'cells' sections.
    """
    ctrl = get_controller()
    cells: Dict[str, Dict[str, float]] = {}
    
    # First, try to use grid positions from config
    grid_positions = cfg.get("grid", {}).get("positions", {})
    
    if grid_positions:
        # Use explicit grid positions from config
        for cell_id, pos in grid_positions.items():
            # Only include actual grid cells (A1-K3), exclude ERR1 etc.
            if (cell_id.startswith(('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K')) and 
                len(cell_id) == 2 and cell_id[1] in '123'):
                x, y = pos if isinstance(pos, (list, tuple)) and len(pos) >= 2 else (0, 0)
                # Z coordinate is NOT included - motion system should handle Z separately
                cells[cell_id] = {'x': float(x), 'y': float(y)}
    else:
        # Fallback: generate positions using spacing from config or defaults
        spacing = cfg.get("grid", {})
        spacing_x = float(spacing.get("column_spacing", 84.0))
        spacing_y = float(spacing.get("row_spacing", 104.0))
        
        # Generate A1-K3 grid (Z coordinate is NOT included)
        cols = ['A','B','C','D','E','F','G','H','I','J','K']
        for r in range(1, 4):  # rows 1, 2, 3
            for col_idx, col in enumerate(cols):
                cell_id = f"{col}{r}"
                x = col_idx * spacing_x
                y = (r - 1) * spacing_y
                cells[cell_id] = {'x': float(x), 'y': float(y)}

    ctrl.configure_cells(cells)
    
    # Configure speeds from config
    motion_config = cfg.get("motion", {})
    if "default_speed" in motion_config:
        ctrl.default_speed = float(motion_config["default_speed"])
    if "rapid_speed" in motion_config:
        ctrl.rapid_speed = float(motion_config["rapid_speed"])
    if "homing_speed" in motion_config:
        ctrl.homing_speed = float(motion_config["homing_speed"])





def get_driver_name() -> str:
    return type(get_controller().driver).__name__


def is_demo_mode() -> bool:
    return isinstance(get_controller().driver, VirtualMotionDriver)


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
    z = 0.0  # Z position is handled separately from cell definitions

    # prefer feedrates from controller or provided overrides
    travel_feed = int(feed_travel or ctrl.default_speed)
    pick_feed = int(feed_pick or max(80, ctrl.default_speed // 2))

    # find M-code names from driver if available
    drv = ctrl.driver
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
        lines.append('G4 P0.03')
        lines.append(f'G1 Z{float(safe_z):.3f} F{travel_feed}')

    return lines