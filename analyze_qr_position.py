#!/usr/bin/env python3
"""
Analyze QR code position in the last snapshot image to determine optimal ROI configuration.
"""

import cv2
import numpy as np
from pathlib import Path

def analyze_qr_position(image_path):
    """Analyze where QR codes appear in an image and suggest ROI regions."""
    
    # Load image
    img = cv2.imread(str(image_path))
    if img is None:
        print(f"Error: Could not load image from {image_path}")
        return
    
    height, width = img.shape[:2]
    print(f"Image dimensions: {width}x{height}")
    print()
    
    # Initialize QR detector
    detector = cv2.QRCodeDetector()
    
    # Detect QR codes
    data, points, _ = detector.detectAndDecode(img)
    
    if points is not None and len(points) > 0:
        print("✓ QR Code detected!")
        print(f"  Data: {data}")
        print()
        
        # Get bounding box of QR code
        points = points[0]  # First QR code
        x_coords = points[:, 0]
        y_coords = points[:, 1]
        
        min_x = int(np.min(x_coords))
        max_x = int(np.max(x_coords))
        min_y = int(np.min(y_coords))
        max_y = int(np.max(y_coords))
        
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2
        
        print(f"QR Code position (pixels):")
        print(f"  X range: {min_x} - {max_x} (center: {center_x:.0f})")
        print(f"  Y range: {min_y} - {max_y} (center: {center_y:.0f})")
        print()
        
        # Convert to normalized coordinates (0.0 - 1.0)
        norm_x_min = min_x / width
        norm_x_max = max_x / width
        norm_y_min = min_y / height
        norm_y_max = max_y / height
        norm_center_x = center_x / width
        norm_center_y = center_y / height
        
        print(f"Normalized coordinates (0.0 - 1.0):")
        print(f"  X: {norm_x_min:.3f} - {norm_x_max:.3f} (center: {norm_center_x:.3f})")
        print(f"  Y: {norm_y_min:.3f} - {norm_y_max:.3f} (center: {norm_center_y:.3f})")
        print()
        
        # Determine which third horizontally
        if norm_center_x < 0.33:
            h_position = "LEFT third (A1)"
        elif norm_center_x < 0.66:
            h_position = "MIDDLE third (A2)"
        else:
            h_position = "RIGHT third (A3)"
        
        # Determine vertical position
        if norm_center_y < 0.25:
            v_position = "TOP quarter"
        elif norm_center_y < 0.5:
            v_position = "UPPER-MIDDLE quarter"
        elif norm_center_y < 0.75:
            v_position = "LOWER-MIDDLE quarter"
        else:
            v_position = "BOTTOM quarter"
        
        print(f"Position analysis:")
        print(f"  Horizontal: {h_position}")
        print(f"  Vertical: {v_position}")
        print()
        
        # Suggest ROI regions
        print("Suggested ROI configuration:")
        print()
        
        # Add margin around detected position
        margin = 0.15  # 15% margin on each side
        
        suggested_y_min = max(0.0, norm_center_y - margin)
        suggested_y_max = min(1.0, norm_center_y + margin)
        
        print("For single QR code (centered in this position):")
        print(f"  roi: [x_min, {suggested_y_min:.2f}, x_max, {suggested_y_max:.2f}]")
        print()
        
        print("For three feeders spanning full width:")
        print(f"  A1: [0.0, {suggested_y_min:.2f}, 0.33, {suggested_y_max:.2f}]")
        print(f"  A2: [0.33, {suggested_y_min:.2f}, 0.66, {suggested_y_max:.2f}]")
        print(f"  A3: [0.66, {suggested_y_min:.2f}, 1.0, {suggested_y_max:.2f}]")
        
    else:
        print("✗ No QR code detected in image")
        print("  This could mean:")
        print("  - QR code not in frame")
        print("  - QR code too small/large")
        print("  - Poor lighting/focus")
        print("  - QR code obscured")

if __name__ == "__main__":
    # Find the most recent snapshot
    snapshots_dir = Path("data/snapshots")
    
    # Look for original images
    original_images = sorted(snapshots_dir.glob("*_original.jpg"))
    
    if original_images:
        latest_image = original_images[-1]
        print(f"Analyzing: {latest_image.name}")
        print("=" * 60)
        print()
        analyze_qr_position(latest_image)
    else:
        print("No snapshot images found in data/snapshots/")
