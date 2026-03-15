#!/usr/bin/env python3
"""Test QR code auto-advance functionality."""

import asyncio
import sys

# Add app directory to path
sys.path.insert(0, 'app')

from services import run_loop
from services import motion as motion_svc

async def test_auto_advance():
    """Test that detecting QR code on active feeder automatically advances to next feeder."""
    
    print("=" * 70)
    print("Testing QR Code Auto-Advance Functionality")
    print("=" * 70)
    
    # Get initial feeder state
    print(f"\nInitial state:")
    print(f"  Active feeder: {run_loop._active_feeder}")
    print(f"  Feeder sequence: {run_loop._FEEDER_SEQUENCE}")
    print(f"  Feeder counts: {run_loop.feeders_remaining()}")
    
    # Set A1 as the active feeder
    run_loop._active_feeder = "A1"
    run_loop.state.feeder_counts["A1"] = 50
    print(f"\n✓ Set A1 as active feeder with 50 cards")
    print(f"  Active feeder: {run_loop._active_feeder}")
    
    # Simulate QR code detection by calling _refresh_feeder_detections
    print(f"\n⏳ Calling _refresh_feeder_detections() to check for QR codes...")
    results = await run_loop._refresh_feeder_detections(["A1"])
    
    print(f"\n📊 Detection results:")
    for cell, data in results.items():
        qr_detected = data.get("qr_detected", False)
        qr_data = data.get("qr_data", "")
        print(f"  {cell}: QR detected={qr_detected}, data='{qr_data}'")
    
    # Check if active feeder changed
    print(f"\n📍 After QR detection:")
    print(f"  Active feeder: {run_loop._active_feeder}")
    print(f"  Feeder counts: {run_loop.feeders_remaining()}")
    
    if run_loop._active_feeder == "A2":
        print(f"\n✅ SUCCESS: Auto-advance worked! Moved from A1 to A2")
    elif run_loop._active_feeder == "A1":
        print(f"\n❌ FAIL: Active feeder still A1 (not advanced)")
    else:
        print(f"\n⚠️  WARNING: Active feeder is {run_loop._active_feeder}")
    
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(test_auto_advance())
