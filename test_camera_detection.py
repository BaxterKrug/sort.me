#!/usr/bin/env python3
"""Test script to verify camera detection is working."""

import sys
import cv2
import logging

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

def test_opencv_version():
    """Check OpenCV version."""
    LOG.info(f"OpenCV version: {cv2.__version__}")
    LOG.info(f"OpenCV build info:")
    print(cv2.getBuildInformation())

def test_camera_direct(device_id=0):
    """Test opening a camera directly with OpenCV."""
    LOG.info(f"\n=== Testing camera {device_id} directly ===")
    
    try:
        cap = cv2.VideoCapture(device_id)
        if not cap.isOpened():
            LOG.error(f"Failed to open camera {device_id}")
            return False
        
        LOG.info(f"✓ Camera {device_id} opened successfully")
        
        # Get properties
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = int(cap.get(cv2.CAP_PROP_FPS))
        backend = cap.get(cv2.CAP_PROP_BACKEND)
        
        LOG.info(f"  Resolution: {width}x{height}")
        LOG.info(f"  FPS: {fps}")
        LOG.info(f"  Backend: {backend}")
        
        # Try to read a frame
        ret, frame = cap.read()
        if not ret or frame is None:
            LOG.error(f"Failed to read frame from camera {device_id}")
            cap.release()
            return False
        
        LOG.info(f"✓ Frame read successfully: shape={frame.shape}")
        cap.release()
        return True
        
    except Exception as e:
        LOG.error(f"Exception testing camera {device_id}: {e}")
        return False

def test_camera_with_v4l2(device_id=0):
    """Test opening a camera with V4L2 backend."""
    LOG.info(f"\n=== Testing camera {device_id} with V4L2 backend ===")
    
    try:
        cap = cv2.VideoCapture(device_id, cv2.CAP_V4L2)
        if not cap.isOpened():
            LOG.error(f"Failed to open camera {device_id} with V4L2")
            return False
        
        LOG.info(f"✓ Camera {device_id} opened with V4L2")
        
        ret, frame = cap.read()
        if not ret or frame is None:
            LOG.error(f"Failed to read frame from camera {device_id} with V4L2")
            cap.release()
            return False
        
        LOG.info(f"✓ Frame read successfully with V4L2: shape={frame.shape}")
        cap.release()
        return True
        
    except Exception as e:
        LOG.error(f"Exception testing camera {device_id} with V4L2: {e}")
        return False

def scan_all_cameras(max_index=5):
    """Scan for all available cameras."""
    LOG.info(f"\n=== Scanning cameras 0-{max_index} ===")
    found = []
    
    for i in range(max_index + 1):
        try:
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    LOG.info(f"✓ Camera {i}: {width}x{height}, frame shape={frame.shape}")
                    found.append(i)
                else:
                    LOG.warning(f"Camera {i} opened but cannot read frame")
                cap.release()
            else:
                LOG.debug(f"Camera {i} not available")
        except Exception as e:
            LOG.debug(f"Camera {i} error: {e}")
    
    LOG.info(f"\nFound {len(found)} working camera(s): {found}")
    return found

if __name__ == "__main__":
    LOG.info("=" * 60)
    LOG.info("Camera Detection Test")
    LOG.info("=" * 60)
    
    test_opencv_version()
    
    # Scan all cameras
    cameras = scan_all_cameras(max_index=5)
    
    if not cameras:
        LOG.error("\n❌ No cameras detected!")
        LOG.error("Check:")
        LOG.error("  1. ls -la /dev/video*")
        LOG.error("  2. groups (should include 'video')")
        LOG.error("  3. lsof /dev/video* (check if in use)")
        sys.exit(1)
    
    # Test first camera in detail
    LOG.info("\n" + "=" * 60)
    LOG.info("Testing first camera in detail")
    LOG.info("=" * 60)
    test_camera_direct(cameras[0])
    test_camera_with_v4l2(cameras[0])
    
    LOG.info("\n✓ Camera detection test completed successfully!")
