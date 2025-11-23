#!/usr/bin/env python3
"""
Test script to check and fix position synchronization issues.
"""
import asyncio
from app.services.motion import get_controller

async def run_position_sync_check():
    print("Testing position synchronization...")
    ctrl = get_controller()
    
    try:
        # Get current stored position
        stored_pos = ctrl.current
        print(f"Stored position: X={stored_pos[0]:.3f}, Y={stored_pos[1]:.3f}, Z={stored_pos[2]:.3f}")
        
        # Get actual hardware position
        actual_pos = await ctrl.driver.query_position()
        print(f"Hardware position: X={actual_pos[0]:.3f}, Y={actual_pos[1]:.3f}, Z={actual_pos[2]:.3f}")
        
        # Check for discrepancy
        x_diff = abs(stored_pos[0] - actual_pos[0])
        y_diff = abs(stored_pos[1] - actual_pos[1])
        z_diff = abs(stored_pos[2] - actual_pos[2])
        
        print(f"Position differences: X={x_diff:.3f}mm, Y={y_diff:.3f}mm, Z={z_diff:.3f}mm")
        
        if x_diff > 1.0 or y_diff > 1.0 or z_diff > 1.0:
            print("⚠️  Large position discrepancy detected!")
            print("Syncing stored position with hardware...")
            ctrl.current = actual_pos
            print(f"✅ Position synced: X={actual_pos[0]:.3f}, Y={actual_pos[1]:.3f}, Z={actual_pos[2]:.3f}")
        else:
            print("✅ Positions are synchronized")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_position_sync_check())