#!/usr/bin/env python3
"""Quick test to verify serial connection to motion controller."""
import serial
import time

PORT = "/dev/ttyACM0"
BAUD = 115200

print(f"Testing serial connection to {PORT} at {BAUD} baud...")

try:
    ser = serial.Serial(PORT, BAUD, timeout=2)
    print("✓ Serial port opened successfully")
    
    # Wait for controller to initialize
    time.sleep(2)
    
    # Clear any startup messages
    while ser.in_waiting:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line:
            print(f"  Startup: {line}")
    
    # Send a simple status query
    print("\nSending status query (M114)...")
    ser.write(b"M114\n")
    time.sleep(0.5)
    
    # Read response
    responses = []
    while ser.in_waiting:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line:
            responses.append(line)
            print(f"  Response: {line}")
    
    if responses:
        print("\n✓ Motion controller is responding!")
    else:
        print("\n⚠ No response from controller. Check:")
        print("  - Controller is powered on")
        print("  - Correct port selected")
        print("  - Firmware is loaded")
    
    ser.close()
    
except serial.SerialException as e:
    print(f"\n✗ Serial connection failed: {e}")
    print("\nTroubleshooting:")
    print("  - Check if device is connected: ls -la /dev/ttyACM*")
    print("  - Check permissions: sudo usermod -a -G dialout $USER")
    print("  - Try unplugging and replugging the USB cable")
    
except Exception as e:
    print(f"\n✗ Unexpected error: {e}")

print("\nTest complete.")
