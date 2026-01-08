#!/usr/bin/env python3
"""
Test script to verify fake hardware mode works correctly.

This script tests:
1. Fake camera returns images from Photos directory
2. Virtual motion driver is used instead of real hardware
"""
import asyncio
import sys
import yaml
from app.services import camera as camera_svc
from app.services import motion


async def test_fake_hardware():
    """Test fake hardware functionality."""
    
    print("=" * 60)
    print("Testing Fake Hardware Mode")
    print("=" * 60)
    
    # Load config
    with open('config.yaml', 'r', encoding='utf8') as f:
        config = yaml.safe_load(f) or {}
    
    use_fake = config.get('use_fake_hardware', False)
    print(f"\nConfig use_fake_hardware: {use_fake}")
    
    if not use_fake:
        print("\n⚠️  WARNING: use_fake_hardware is set to false in config.yaml")
        print("Set it to true to test fake hardware mode.")
        return False
    
    # Test Camera
    print("\n" + "-" * 60)
    print("Testing Fake Camera")
    print("-" * 60)
    
    camera_cfg = config.get("camera", {})
    camera_cfg["use_fake"] = use_fake
    
    try:
        camera_svc.configure(camera_cfg)
        camera = camera_svc.get_manager()
        
        # Get camera info
        info = camera.info(ensure_capture=True)
        print(f"Camera online: {info['online']}")
        print(f"Camera device: {info['device']}")
        print(f"Camera error: {info.get('error', 'None')}")
        
        if not info['online']:
            print("❌ Camera failed to initialize")
            return False
        
        # Try to grab a frame
        print("\nGrabbing test frame...")
        frame = await camera.grab_frame()
        print(f"✓ Got frame with shape: {frame.shape}")
        print(f"✓ Frame dtype: {frame.dtype}")
        
        # Grab a few more to ensure we're getting images from Photos
        for i in range(3):
            frame = await camera.grab_frame()
            print(f"✓ Frame {i+1}: shape={frame.shape}")
        
        print("\n✅ Fake camera working correctly!")
        
    except Exception as e:
        print(f"❌ Camera test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test Motion
    print("\n" + "-" * 60)
    print("Testing Virtual Motion Driver")
    print("-" * 60)
    
    try:
        # Get motion controller
        motion_ctrl = motion.get_controller()
        driver_name = motion.get_driver_name()
        is_demo = motion.is_demo_mode()
        
        print(f"Driver name: {driver_name}")
        print(f"Demo mode: {is_demo}")
        
        if not is_demo:
            print("❌ Expected VirtualMotionDriver but got:", driver_name)
            return False
        
        # Test basic motion commands
        print("\nTesting motion commands...")
        await motion_ctrl.move_to_cell("B1")
        print(f"✓ Moved to B1, current position: {motion_ctrl.current}")
        
        await motion_ctrl.move_to_cell("D3")
        print(f"✓ Moved to D3, current position: {motion_ctrl.current}")
        
        await motion_ctrl.home_all()
        print(f"✓ Homed, current position: {motion_ctrl.current}")
        
        print("\n✅ Virtual motion driver working correctly!")
        
    except Exception as e:
        print(f"❌ Motion test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    print("\n" + "=" * 60)
    print("✅ All fake hardware tests passed!")
    print("=" * 60)
    print("\nYou can now run the main application safely without")
    print("real camera or motion hardware.")
    print("\nTo disable fake hardware mode, set:")
    print("  use_fake_hardware: false")
    print("in config.yaml")
    print("=" * 60)
    
    return True


if __name__ == "__main__":
    success = asyncio.run(test_fake_hardware())
    sys.exit(0 if success else 1)
