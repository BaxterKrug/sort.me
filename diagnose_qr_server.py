#!/usr/bin/env python3
"""
Diagnostic script to check QR code detection using server's camera snapshot.
Tests both full frame and ROI regions to understand why detection might fail.
"""

import cv2
import numpy as np
import yaml
import requests
import json
from pathlib import Path

def load_config():
    """Load QR regions from config.yaml"""
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    qr_regions = config.get('camera', {}).get('qr_codes', [])
    return qr_regions

def get_frame_from_server(server_url="http://localhost:8000"):
    """Get a frame from the server's camera snapshot endpoint"""
    print(f"Requesting snapshot from {server_url}/camera/snapshot...")
    print("(This may take 10-20 seconds as it includes OCR processing...)")
    
    try:
        # Increase timeout since snapshot endpoint does OCR processing
        response = requests.get(f"{server_url}/camera/snapshot", timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        # Get the original image path from response
        saved_paths = data.get('saved', {})
        original_path = saved_paths.get('original_path', '')
        
        if not original_path:
            print("ERROR: No original_path in response")
            print("Response keys:", list(data.keys()))
            return None
        
        snapshot_path = Path(original_path)
        
        if not snapshot_path.exists():
            print(f"ERROR: Snapshot file not found: {snapshot_path}")
            return None
        
        # Load the image
        img = cv2.imread(str(snapshot_path))
        if img is None:
            print(f"ERROR: Could not load image from {snapshot_path}")
            return None
        
        print(f"✓ Loaded snapshot: {snapshot_path}")
        return img
        
    except requests.exceptions.Timeout:
        print(f"ERROR: Request timed out after 30 seconds")
        print("The snapshot endpoint might be taking too long to process")
        return None
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Could not connect to server: {e}")
        print("Make sure the server is running on http://localhost:8000")
        return None

def test_qr_detection(img, region_name="Full Frame", roi=None):
    """Test QR detection on an image or ROI"""
    
    if roi:
        x0, y0, x1, y1 = roi
        height, width = img.shape[:2]
        
        # Convert normalized to pixels
        px0 = int(x0 * width)
        py0 = int(y0 * height)
        px1 = int(x1 * width)
        py1 = int(y1 * height)
        
        # Extract ROI
        roi_img = img[py0:py1, px0:px1]
        test_img = roi_img
        print(f"\n{region_name}:")
        print(f"  ROI normalized: x={x0:.2f}-{x1:.2f}, y={y0:.2f}-{y1:.2f}")
        print(f"  ROI pixels: x={px0}-{px1}, y={py0}-{py1}")
        print(f"  ROI size: {roi_img.shape[1]}x{roi_img.shape[0]}")
    else:
        test_img = img
        print(f"\n{region_name}:")
        print(f"  Size: {img.shape[1]}x{img.shape[0]}")
    
    # Try to detect QR code
    detector = cv2.QRCodeDetector()
    data, points, _ = detector.detectAndDecode(test_img)
    
    if points is not None and len(points) > 0 and data:
        print(f"  ✓ QR DETECTED: '{data}'")
        
        # Show position within the test image
        pts = points[0]
        x_coords = pts[:, 0]
        y_coords = pts[:, 1]
        center_x = np.mean(x_coords)
        center_y = np.mean(y_coords)
        
        if roi:
            # Convert back to full image coordinates
            full_x = px0 + center_x
            full_y = py0 + center_y
            norm_x = full_x / img.shape[1]
            norm_y = full_y / img.shape[0]
            print(f"  Position in ROI: ({center_x:.0f}, {center_y:.0f})")
            print(f"  Position in full frame: ({full_x:.0f}, {full_y:.0f}) = ({norm_x:.3f}, {norm_y:.3f}) normalized")
        else:
            print(f"  Position: ({center_x:.0f}, {center_y:.0f})")
        
        return True, data
    else:
        print(f"  ✗ No QR code detected")
        return False, None

def main():
    print("=" * 70)
    print("QR Code Detection Diagnostic (Server Camera)")
    print("=" * 70)
    
    # Load config
    print("\nLoading QR regions from config.yaml...")
    qr_regions = load_config()
    
    if not qr_regions:
        print("ERROR: No QR regions configured in config.yaml")
        return
    
    print(f"Found {len(qr_regions)} configured regions:")
    for region in qr_regions:
        cell = region['cell']
        roi = region['roi']
        print(f"  {cell}: x={roi[0]:.2f}-{roi[2]:.2f}, y={roi[1]:.2f}-{roi[3]:.2f}")
    
    # Get frame from server
    print()
    frame = get_frame_from_server()
    
    if frame is None:
        return
    
    height, width = frame.shape[:2]
    print(f"Frame dimensions: {width}x{height}")
    
    # Test 1: Full frame detection
    print("\n" + "=" * 70)
    print("TEST 1: Full Frame Detection")
    print("=" * 70)
    
    full_detected, full_data = test_qr_detection(frame, "Full Frame")
    
    # Test 2: Each configured ROI region
    print("\n" + "=" * 70)
    print("TEST 2: ROI Region Detection")
    print("=" * 70)
    
    roi_results = []
    for region in qr_regions:
        cell = region['cell']
        roi = region['roi']
        detected, data = test_qr_detection(frame, f"Region {cell}", roi)
        roi_results.append((cell, detected, data))
    
    # Test 3: Convert to grayscale and test again
    print("\n" + "=" * 70)
    print("TEST 3: Grayscale Conversion (mimics scanner behavior)")
    print("=" * 70)
    
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    print("\nFull frame (grayscale):")
    detector = cv2.QRCodeDetector()
    data, points, _ = detector.detectAndDecode(gray)
    
    if points is not None and len(points) > 0 and data:
        print(f"  ✓ QR DETECTED: '{data}'")
    else:
        print(f"  ✗ No QR code detected")
    
    # Test each ROI in grayscale
    print("\nROI regions (grayscale):")
    for region in qr_regions:
        cell = region['cell']
        roi = region['roi']
        x0, y0, x1, y1 = roi
        
        px0 = int(x0 * width)
        py0 = int(y0 * height)
        px1 = int(x1 * width)
        py1 = int(y1 * height)
        
        roi_gray = gray[py0:py1, px0:px1]
        data, points, _ = detector.detectAndDecode(roi_gray)
        
        if points is not None and len(points) > 0 and data:
            print(f"  {cell}: ✓ QR DETECTED: '{data}'")
        else:
            print(f"  {cell}: ✗ No QR code detected")
    
    # Save debug image
    debug_path = "qr_debug_frame.jpg"
    cv2.imwrite(debug_path, frame)
    print(f"\nDebug frame saved to: {debug_path}")
    
    # Draw ROI rectangles on debug image
    debug_img = frame.copy()
    colors = [(0, 255, 0), (255, 0, 0), (0, 0, 255)]  # Green, Blue, Red
    
    for i, region in enumerate(qr_regions):
        cell = region['cell']
        roi = region['roi']
        x0, y0, x1, y1 = roi
        
        px0 = int(x0 * width)
        py0 = int(y0 * height)
        px1 = int(x1 * width)
        py1 = int(y1 * height)
        
        color = colors[i % len(colors)]
        cv2.rectangle(debug_img, (px0, py0), (px1, py1), color, 3)
        cv2.putText(debug_img, cell, (px0 + 10, py0 + 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)
    
    roi_debug_path = "qr_debug_with_roi.jpg"
    cv2.imwrite(roi_debug_path, debug_img)
    print(f"Debug frame with ROI boxes saved to: {roi_debug_path}")
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    if full_detected:
        print(f"✓ QR code IS visible in camera view: '{full_data}'")
    else:
        print("✗ QR code NOT detected in full camera view")
        print("  Possible reasons:")
        print("  - QR code not in camera frame")
        print("  - QR code too small, large, or out of focus")
        print("  - Poor lighting or reflections")
        print("  - QR code damaged or obscured")
        return
    
    print("\nROI Region Results:")
    any_roi_detected = False
    for cell, detected, data in roi_results:
        if detected:
            print(f"  {cell}: ✓ DETECTED: '{data}'")
            any_roi_detected = True
        else:
            print(f"  {cell}: ✗ Not detected")
    
    if not any_roi_detected:
        print("\n⚠ WARNING: QR code visible in full frame but NOT in any ROI region!")
        print("  Solution: Adjust ROI coordinates in config.yaml to match QR position")
        print(f"  Check qr_debug_with_roi.jpg to see where ROI boxes are vs QR position")
    else:
        print("\n✓ QR code detected in ROI region(s)!")
        print("  The scanner SHOULD be detecting it.")
        print("\n  If /qr/scan still shows no detection, possible issues:")
        print("  1. Server needs restart to load new config")
        print("  2. QR scanner not properly initialized")
        print("  3. Check server startup logs for QR scanner errors")

if __name__ == "__main__":
    main()
