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
        """
        Move along an axis until a limit switch is triggered.
        Uses incremental moves with position feedback to detect when movement stops.
        """
        axis_upper = axis.upper()
        step = 1.0 * (1 if direction > 0 else -1)
        
        LOG.info("Moving until limit: axis=%s, direction=%d, speed=%.1f", axis_upper, direction, speed)
        
        # Switch to relative positioning mode
        await self.send_gcode('G91')
        
        try:
            last_pos = None
            stuck_count = 0
            max_steps = 2000
            
            for step_num in range(max_steps):
                # Get current position before move
                try:
                    current_pos = await self.query_position()
                    axis_index = {'X': 0, 'Y': 1, 'Z': 2}.get(axis_upper, 0)
                    current_axis_pos = current_pos[axis_index] if current_pos else 0.0
                    
                    # Check if we're stuck (limit switch triggered)
                    if last_pos is not None:
                        movement = abs(current_axis_pos - last_pos)
                        if movement < 0.1:  # Less than 0.1mm movement indicates limit hit
                            stuck_count += 1
                            if stuck_count >= 3:  # Require 3 consecutive stuck moves
                                LOG.info("Limit switch detected at position %.3f (axis %s)", current_axis_pos, axis_upper)
                                return current_axis_pos
                        else:
                            stuck_count = 0
                    
                    last_pos = current_axis_pos
                except Exception:
                    # If position query fails, continue with the move
                    pass
                
                # Make incremental move
                await self.send_gcode(f'G1 {axis_upper}{step:.3f} F{int(speed)}')
                await asyncio.sleep(0.05)  # Small delay for movement
                
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

    All coordinates are assumed to be in the same units as the real driver expects.
    """
    def __init__(self, driver: Optional[MotionDriver] = None):
        if driver is None:
            raise ValueError("MotionController requires a driver - no default simulation available")
        self.driver = driver
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

    # Backwards-compatible adapter methods expected by main.py endpoints
    async def jog_axis(self, axis: str, distance: float, speed: Optional[float] = None) -> Tuple[float,float,float]:
        """Compatibility wrapper for jog_axis calls from main.py"""
        return await self.jog(axis.lower(), distance, speed)

    async def home_x(self) -> None:
        """Home X axis using move_until_limit"""
        limit_speed = max(50.0, self.default_speed / 4)
        x_limit = await self.driver.move_until_limit('x', -1, limit_speed)
        self.current = (float(x_limit), self.current[1], self.current[2])
        self.homed = True

    async def home_y(self) -> None:
        """Home Y axis using move_until_limit"""
        limit_speed = max(50.0, self.default_speed / 4)
        y_limit = await self.driver.move_until_limit('y', -1, limit_speed)
        self.current = (self.current[0], float(y_limit), self.current[2])
        self.homed = True

    async def home_z(self) -> None:
        """Home Z axis using move_until_limit"""
        limit_speed = max(50.0, self.default_speed / 4)
        z_limit = await self.driver.move_until_limit('z', -1, limit_speed)
        self.current = (self.current[0], self.current[1], float(z_limit))
        self.homed = True

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
        
        # Set slow speed for limit switch approach
        limit_speed = max(50.0, self.default_speed / 4)
        await self.driver.set_speed(limit_speed)
        
        # First, move to X limit (negative direction, left side)
        LOG.info("Finding X limit switch (moving left)")
        x_limit = await self.driver.move_until_limit('X', -1, limit_speed)
        LOG.info("X limit found at position: %.3f", x_limit)
        
        # Then move to Y limit (negative direction, towards front/top)
        LOG.info("Finding Y limit switch (moving forward)")
        y_limit = await self.driver.move_until_limit('Y', -1, limit_speed)
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
        
        # Store the current position as A1
        a1_x, a1_y, a1_z = self.current
        
        # Update the A1 cell position
        self.cells['A1'] = {'x': a1_x, 'y': a1_y, 'z': a1_z}
        
        # Recalculate all other cell positions based on the new A1 reference
        spacing_x = 25.0  # Same as the default spacing
        spacing_y = 25.0
        
        updated_cells = {}
        for cid, pos in self.cells.items():
            if cid == 'A1':
                updated_cells[cid] = {'x': a1_x, 'y': a1_y, 'z': a1_z}
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
            
            # Calculate new position relative to A1
            new_x = a1_x + (col_index * spacing_x)
            new_y = a1_y + (row_index * spacing_y)
            new_z = a1_z  # Keep same Z as A1
            
            updated_cells[cid] = {'x': new_x, 'y': new_y, 'z': new_z}
        
        self.cells = updated_cells
        
        LOG.info("A1 reference saved at (%.3f, %.3f, %.3f). Updated %d cell positions.", 
                a1_x, a1_y, a1_z, len(updated_cells))
        
        return {
            "a1_reference": (a1_x, a1_y, a1_z),
            "cells_updated": len(updated_cells),
            "status": "a1_reference_saved",
            "message": f"A1 reference set to ({a1_x:.3f}, {a1_y:.3f}, {a1_z:.3f}). All cell positions updated."
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
        # Create GCode driver from config
        import yaml
        try:
            with open('config.yaml', 'r', encoding='utf8') as f:
                config = yaml.safe_load(f)
            gcode_opts = config.get('gcode', {})
            configured_port = gcode_opts.get('port', '/dev/ttyACM0')
            baud = int(gcode_opts.get('baud', 115200))
            mcodes = gcode_opts.get('mcodes')
            feedrates = gcode_opts.get('feedrates')
            
            # Try multiple common ports for V1CNC/Marlin hardware
            ports_to_try = [configured_port]
            if configured_port not in ['/dev/ttyACM0', '/dev/ttyACM1']:
                ports_to_try.extend(['/dev/ttyACM0', '/dev/ttyACM1'])
            
            driver = None
            for port in ports_to_try:
                try:
                    import os
                    if not os.path.exists(port):
                        LOG.debug("Port %s does not exist, skipping", port)
                        continue
                    
                    test_driver = GCodeDriver(port=port, baud=baud, mcodes=mcodes, feedrates=feedrates)
                    test_driver._ensure_serial()
                    LOG.info("Successfully connected to %s at %d baud", port, baud)
                    driver = test_driver
                    break
                except Exception as port_exc:
                    LOG.warning("Failed to connect to %s: %s", port, port_exc)
                    continue
            
            if driver is None:
                raise RuntimeError(f"Could not connect to any serial port from {ports_to_try}")
            
            _controller = MotionController(driver)
            LOG.info("Created MotionController with GCodeDriver port=%s baud=%s", driver.port, baud)
        except Exception as exc:
            LOG.exception("Failed to create GCodeDriver from config: %s", exc)
            raise RuntimeError(f"Cannot initialize motion controller: {exc}")
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
    spacing_x = 84.0
    spacing_y = 104.0
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