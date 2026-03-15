#!/usr/bin/env python3
"""Test script for QR code detection in camera frames.

This script helps you:
1. Test QR code detection with your camera
2. Generate test QR codes for feeder markers
3. Verify ROI configuration for QR scanning

Usage:
    python test_qr_detection.py --mode [detect|generate|test]
"""

import argparse
import logging
import sys
from pathlib import Path

import cv2
import numpy as np
import qrcode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
LOG = logging.getLogger("test_qr")


def generate_qr_codes(output_dir: str = "qr_codes"):
    """Generate QR codes for feeder end markers."""
    output_path = Path(output_dir)
    output_path.mkdir(exist_ok=True)
    
    feeders = ["A1", "A2", "A3"]
    
    for feeder in feeders:
        data = f"FEEDER_{feeder}_END"
        
        # Create QR code
        qr = qrcode.QRCode(
            version=1,  # Size of QR code (1-40)
            error_correction=qrcode.constants.ERROR_CORRECT_H,  # High error correction
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        # Generate image
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Save as PNG
        filepath = output_path / f"{feeder}_end_marker.png"
        img.save(str(filepath))
        LOG.info(f"Generated QR code for {feeder}: {filepath}")
        LOG.info(f"  Data: {data}")
        
        # Also save a larger version for printing
        large_qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=20,
            border=8,
        )
        large_qr.add_data(data)
        large_qr.make(fit=True)
        large_img = large_qr.make_image(fill_color="black", back_color="white")
        large_filepath = output_path / f"{feeder}_end_marker_large.png"
        large_img.save(str(large_filepath))
        LOG.info(f"  Large version: {large_filepath}")
    
    LOG.info(f"\nQR codes saved to {output_path}/")
    LOG.info("Print these codes and place them at the bottom of your feeder cells.")


def test_detection_on_image(image_path: str):
    """Test QR detection on a saved image."""
    img = cv2.imread(image_path)
    if img is None:
        LOG.error(f"Could not load image: {image_path}")
        return
    
    LOG.info(f"Testing QR detection on: {image_path}")
    LOG.info(f"Image shape: {img.shape}")
    
    # Initialize QR detector
    detector = cv2.QRCodeDetector()
    
    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Detect and decode
    data, corners, _ = detector.detectAndDecode(gray)
    
    if data:
        LOG.info(f"✓ QR code detected!")
        LOG.info(f"  Data: {data}")
        LOG.info(f"  Corners: {corners}")
        
        # Draw detection on image
        if corners is not None:
            corners = corners.astype(int)
            cv2.polylines(img, [corners], True, (0, 255, 0), 3)
        
        # Save result
        output_path = Path(image_path).parent / f"{Path(image_path).stem}_detected.jpg"
        cv2.imwrite(str(output_path), img)
        LOG.info(f"  Detection visualization saved to: {output_path}")
    else:
        LOG.warning("✗ No QR code detected in image")


def test_live_camera(device_id: int = 0):
    """Test QR detection with live camera feed."""
    LOG.info(f"Opening camera {device_id}...")
    cap = cv2.VideoCapture(device_id)
    
    if not cap.isOpened():
        LOG.error(f"Could not open camera {device_id}")
        return
    
    # Set resolution
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    
    actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    LOG.info(f"Camera resolution: {actual_width}x{actual_height}")
    
    detector = cv2.QRCodeDetector()
    
    LOG.info("Press 'q' to quit, 's' to save current frame")
    
    frame_count = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            LOG.warning("Failed to read frame")
            break
        
        frame_count += 1
        
        # Detect QR code
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        data, corners, _ = detector.detectAndDecode(gray)
        
        # Draw detection
        display = frame.copy()
        if data:
            LOG.info(f"Frame {frame_count}: QR detected - {data}")
            if corners is not None:
                corners = corners.astype(int)
                cv2.polylines(display, [corners], True, (0, 255, 0), 3)
                cv2.putText(display, data, (10, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Draw ROI guides for feeder positions (based on typical config)
        h, w = display.shape[:2]
        # A1 ROI (left third, bottom quarter)
        cv2.rectangle(display, (0, int(h * 0.75)), (int(w * 0.33), h), (255, 0, 0), 2)
        cv2.putText(display, "A1", (10, int(h * 0.75) + 20), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
        
        # A2 ROI (middle third, bottom quarter)
        cv2.rectangle(display, (int(w * 0.33), int(h * 0.75)), (int(w * 0.66), h), (255, 0, 0), 2)
        cv2.putText(display, "A2", (int(w * 0.33) + 10, int(h * 0.75) + 20), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
        
        # A3 ROI (right third, bottom quarter)
        cv2.rectangle(display, (int(w * 0.66), int(h * 0.75)), (w, h), (255, 0, 0), 2)
        cv2.putText(display, "A3", (int(w * 0.66) + 10, int(h * 0.75) + 20), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
        
        cv2.imshow("QR Detection Test", display)
        
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('s'):
            save_path = f"qr_test_frame_{frame_count}.jpg"
            cv2.imwrite(save_path, frame)
            LOG.info(f"Saved frame to {save_path}")
    
    cap.release()
    cv2.destroyAllWindows()
    LOG.info("Camera test completed")


def main():
    parser = argparse.ArgumentParser(description="Test QR code detection for feeder markers")
    parser.add_argument(
        "--mode",
        choices=["generate", "detect", "live"],
        default="generate",
        help="Mode: generate QR codes, detect in image, or live camera test"
    )
    parser.add_argument(
        "--image",
        help="Path to image file for detection test"
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        help="Camera device ID for live test (default: 0)"
    )
    parser.add_argument(
        "--output",
        default="qr_codes",
        help="Output directory for generated QR codes (default: qr_codes)"
    )
    
    args = parser.parse_args()
    
    if args.mode == "generate":
        generate_qr_codes(args.output)
    elif args.mode == "detect":
        if not args.image:
            LOG.error("--image required for detect mode")
            parser.print_help()
            sys.exit(1)
        test_detection_on_image(args.image)
    elif args.mode == "live":
        test_live_camera(args.camera)


if __name__ == "__main__":
    main()
