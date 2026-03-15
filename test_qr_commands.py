#!/usr/bin/env python3
"""Test QR code command parsing and execution."""

import asyncio
import sys

# Add app directory to path
sys.path.insert(0, 'app')

from services import run_loop


def test_parse_qr_command():
    """Test parsing various QR code formats."""
    print("=" * 70)
    print("Testing QR Command Parsing")
    print("=" * 70)
    
    test_cases = [
        ("A1 endstep", {"cell": "A1", "command": "endstep"}),
        ("A1", {"cell": "A1", "command": "endstep"}),
        ("endstep", {"cell": None, "command": "endstep"}),
        ("A2 refill", {"cell": "A2", "command": "refill"}),
        ("B1 pause", {"cell": "B1", "command": "pause"}),
        ("resume A3", {"cell": "A3", "command": "resume"}),
        ("  A1  ENDSTEP  ", {"cell": "A1", "command": "endstep"}),
        # Legacy "FEEDER_XX_END" format from QR code generator
        ("FEEDER_A1_END", {"cell": "A1", "command": "endstep"}),
        ("FEEDER_A2_END", {"cell": "A2", "command": "endstep"}),
        ("FEEDER_B1_REFILL", {"cell": "B1", "command": "refill"}),
    ]
    
    all_passed = True
    for qr_data, expected in test_cases:
        result = run_loop.parse_qr_command(qr_data)
        
        cell_ok = result.get("cell") == expected.get("cell")
        cmd_ok = result.get("command") == expected.get("command")
        
        status = "✓" if (cell_ok and cmd_ok) else "✗"
        if not (cell_ok and cmd_ok):
            all_passed = False
        
        print(f"{status} Input: '{qr_data}'")
        print(f"    Expected: cell={expected.get('cell')}, command={expected.get('command')}")
        print(f"    Got:      cell={result.get('cell')}, command={result.get('command')}")
    
    return all_passed


async def test_execute_qr_command():
    """Test executing QR commands."""
    print("\n" + "=" * 70)
    print("Testing QR Command Execution")
    print("=" * 70)
    
    # Reset state for clean test
    run_loop.reset_qr_command_state()
    run_loop._active_feeder = "A1"
    run_loop.state.feeder_counts["A1"] = 50
    run_loop.state.feeder_counts["A2"] = 50
    run_loop._FEEDER_DETECTION["A1"] = True
    run_loop._FEEDER_DETECTION["A2"] = True
    
    print(f"\n1. Initial state:")
    print(f"   Active feeder: {run_loop._active_feeder}")
    print(f"   A1 count: {run_loop.state.feeder_counts.get('A1')}")
    print(f"   A2 count: {run_loop.state.feeder_counts.get('A2')}")
    
    # Test endstep command
    print(f"\n2. Executing 'A1 endstep' command...")
    result = await run_loop.execute_qr_command("A1 endstep", "A1")
    print(f"   Result: {result}")
    print(f"   Active feeder: {run_loop._active_feeder}")
    print(f"   A1 count: {run_loop.state.feeder_counts.get('A1')}")
    
    endstep_ok = (
        result.get("executed") == True and
        run_loop.state.feeder_counts.get("A1") == 0 and
        run_loop._active_feeder == "A2"
    )
    print(f"   {'✓' if endstep_ok else '✗'} endstep command {'worked' if endstep_ok else 'FAILED'}")
    
    # Test that same command doesn't execute again
    print(f"\n3. Re-executing same 'A1 endstep' command (should skip)...")
    result2 = await run_loop.execute_qr_command("A1 endstep", "A1")
    print(f"   Result: {result2}")
    skip_ok = result2.get("executed") == False
    print(f"   {'✓' if skip_ok else '✗'} Command was {'skipped' if skip_ok else 'NOT skipped'} (as expected)")
    
    # Test refill command
    print(f"\n4. Executing 'A1 refill' command...")
    result3 = await run_loop.execute_qr_command("A1 refill", "A1")
    print(f"   Result: {result3}")
    print(f"   A1 count: {run_loop.state.feeder_counts.get('A1')}")
    refill_ok = (
        result3.get("executed") == True and
        run_loop.state.feeder_counts.get("A1") > 0
    )
    print(f"   {'✓' if refill_ok else '✗'} refill command {'worked' if refill_ok else 'FAILED'}")
    
    return endstep_ok and skip_ok and refill_ok


async def main():
    """Run all tests."""
    parse_ok = test_parse_qr_command()
    exec_ok = await test_execute_qr_command()
    
    print("\n" + "=" * 70)
    print("Test Summary")
    print("=" * 70)
    print(f"Parsing tests: {'✓ PASSED' if parse_ok else '✗ FAILED'}")
    print(f"Execution tests: {'✓ PASSED' if exec_ok else '✗ FAILED'}")
    
    if parse_ok and exec_ok:
        print("\n✅ All tests passed!")
        return 0
    else:
        print("\n❌ Some tests failed!")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
