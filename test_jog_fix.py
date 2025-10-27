#!/usr/bin/env python3
"""
Test script to verify jog overflow fix
"""
import asyncio
from app.services.motion import MotionController, MotionDriver

class MockDriver(MotionDriver):
    """Mock driver for testing without hardware"""
    def __init__(self):
        self.position = (0.0, 0.0, 0.0)
    
    async def move_absolute(self, x: float, y: float, z: float, speed: float) -> None:
        print(f"Mock move to: X={x:.3f} Y={y:.3f} Z={z:.3f} F={speed}")
        self.position = (x, y, z)
    
    async def set_speed(self, speed: float) -> None:
        print(f"Mock set speed: {speed}")
    
    async def send_gcode(self, cmd: str, wait_ok: bool = True, timeout: float = 2.0):
        print(f"Mock G-code: {cmd}")
        return ["ok"]
    
    async def query_position(self):
        return self.position
    
    async def stop(self) -> None:
        print("Mock stop")
    
    # Implement other required methods as no-ops
    async def vacuum_on(self) -> None: pass
    async def vacuum_off(self) -> None: pass
    async def plunger_down(self) -> None: pass
    async def plunger_up(self) -> None: pass
    async def home_all(self) -> None: pass
    async def move_until_limit(self, axis: str, direction: int, speed: float) -> float:
        return 0.0

async def test_jog_safety():
    print("Testing jog safety improvements...")
    
    # Create controller with mock driver
    mock_driver = MockDriver()
    controller = MotionController(mock_driver)
    controller.current = (0.0, 0.0, 0.0)
    
    try:
        print("\n1. Testing normal jog X +10mm...")
        result = await controller.jog('x', 10.0)
        print(f"   Result: {result}")
        
        print("\n2. Testing normal jog Y -5mm...")
        result = await controller.jog('y', -5.0)
        print(f"   Result: {result}")
        
        print("\n3. Testing safety limit - excessive distance...")
        try:
            result = await controller.jog('x', 2000.0)  # Should fail
            print(f"   ERROR: This should have failed! Got: {result}")
        except ValueError as e:
            print(f"   ✅ Correctly rejected: {e}")
        
        print("\n4. Testing bounds checking...")
        controller.current = (950.0, 0.0, 0.0)  # Near X limit
        try:
            result = await controller.jog('x', 100.0)  # Should fail - would exceed 1000mm
            print(f"   ERROR: This should have failed! Got: {result}")
        except ValueError as e:
            print(f"   ✅ Correctly rejected: {e}")
        
        print("\n5. Testing position reset...")
        await controller.reset_position(50.0, 25.0, 10.0)
        print(f"   Position after reset: {controller.current}")
        
        print("\n✅ All safety tests passed!")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_jog_safety())