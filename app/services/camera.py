import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple
import glob
import os
import random

import cv2  # type: ignore
import numpy as np  # type: ignore[import-not-found]

LOG = logging.getLogger("sort.camera")


class FakeCamera:
    """Fake camera that returns random images from the Photos directory.
    
    Used for testing without real camera hardware.
    """
    
    def __init__(self, photos_dir: str = "Photos"):
        self.photos_dir = photos_dir
        self._image_files: list = []
        self._current_image: Optional[np.ndarray] = None
        self._scan_photos()
        
    def _scan_photos(self) -> None:
        """Scan the Photos directory for image files."""
        if not os.path.exists(self.photos_dir):
            LOG.warning(f"Photos directory '{self.photos_dir}' does not exist. Creating fake image.")
            return
            
        patterns = ['*.jpg', '*.jpeg', '*.png', '*.bmp']
        self._image_files = []
        for pattern in patterns:
            path_pattern = os.path.join(self.photos_dir, pattern)
            self._image_files.extend(glob.glob(path_pattern))
            
        if self._image_files:
            LOG.info(f"FakeCamera: Found {len(self._image_files)} images in {self.photos_dir}")
        else:
            LOG.warning(f"FakeCamera: No images found in {self.photos_dir}")
    
    def _get_fake_image(self) -> np.ndarray:
        """Get a random image from Photos directory or create a fake one."""
        if self._image_files:
            # Pick a random image from the Photos directory
            image_path = random.choice(self._image_files)
            LOG.debug(f"FakeCamera: Loading image {os.path.basename(image_path)}")
            img = cv2.imread(image_path)
            if img is not None:
                return img
            LOG.warning(f"FakeCamera: Failed to load {image_path}, generating fake image")
        
        # Fallback: create a simple test pattern
        img = np.zeros((720, 1280, 3), dtype=np.uint8)
        # Add some pattern so it's not just black
        cv2.putText(img, "FAKE CAMERA - No Photos", (50, 360), 
                   cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
        cv2.rectangle(img, (100, 100), (1180, 620), (0, 255, 0), 5)
        return img
    
    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """Simulate cv2.VideoCapture.read() interface."""
        # Return a new random image each time
        img = self._get_fake_image()
        return (True, img)
    
    def isOpened(self) -> bool:
        """Simulate cv2.VideoCapture.isOpened() interface."""
        return True
    
    def release(self) -> None:
        """Simulate cv2.VideoCapture.release() interface."""
        self._current_image = None
        LOG.debug("FakeCamera released")


class CameraManager:
    """Simple singleton-friendly manager around cv2.VideoCapture.

    Provides async helpers so the rest of the app can await frame capture
    without blocking the event loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._capture: Optional[cv2.VideoCapture] = None
        self._cfg: Dict[str, Any] = {
            "device": 0,
            "resolution": (1280, 720),
            "fps": 30,
            "fallback_image": None,
            "use_fake": False,  # Flag for fake hardware mode
        }
        self._last_frame: Optional[np.ndarray] = None
        self._last_ts: float = 0.0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # configuration & lifecycle
    # ------------------------------------------------------------------
    def configure(self, cfg: Optional[Dict[str, Any]]) -> None:
        cfg = cfg or {}
        with self._lock:
            self._cfg.update({
                "device": cfg.get("device", self._cfg["device"]),
                "resolution": tuple(cfg.get("resolution", self._cfg["resolution"])),
                "fps": cfg.get("fps", self._cfg.get("fps", 30)),
                "fallback_image": cfg.get("fallback_image"),
                "use_fake": cfg.get("use_fake", False),
            })
            self._dispose_locked()
            self._last_error = None
            LOG.info("Camera configured: device=%s resolution=%s fps=%s use_fake=%s", 
                    self._cfg["device"], self._cfg["resolution"], self._cfg.get("fps"), self._cfg.get("use_fake"))
            
            # Attempt to open camera immediately to catch issues early
            if not self._cfg.get("use_fake", False):
                try:
                    cap = self._ensure_capture_locked()
                    if cap is None or not cap.isOpened():
                        LOG.warning(f"Camera device {self._cfg['device']} could not be opened during configuration")
                    else:
                        LOG.debug(f"Camera device {self._cfg['device']} opened successfully during configuration")
                except Exception as e:
                    LOG.warning(f"Error opening camera during configuration: {e}")

    def _dispose_locked(self) -> None:
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                pass
            self._capture = None
        self._last_frame = None
        self._last_ts = 0.0
        self._last_error = None

    def close(self) -> None:
        with self._lock:
            self._dispose_locked()

    def current_device(self) -> Any:
        with self._lock:
            return self._cfg.get("device")

    def info(self, ensure_capture: bool = False) -> Dict[str, Any]:
        with self._lock:
            if ensure_capture:
                try:
                    cap = self._ensure_capture_locked()
                    if cap is None or not cap.isOpened():
                        if self._last_error is None:
                            self._last_error = f"Unable to open camera device {self._cfg.get('device')}"
                    else:
                        self._last_error = None
                except Exception as exc:  # pragma: no cover - best effort
                    self._last_error = str(exc)
            online = bool(self._capture and self._capture.isOpened())
            return {
                "device": self._cfg.get("device"),
                "resolution": tuple(self._cfg.get("resolution", (1280, 720))),
                "fps": self._cfg.get("fps"),
                "online": online,
                "last_frame_ts": self._last_ts,
                "error": self._last_error,
            }

    def last_frame_age(self) -> Optional[float]:
        if not self._last_ts:
            return None
        return time.time() - self._last_ts

    # ------------------------------------------------------------------
    # capture helpers
    # ------------------------------------------------------------------
    def _ensure_capture_locked(self) -> Optional[cv2.VideoCapture]:
        if self._capture is not None and self._capture.isOpened():
            return self._capture
        
        # Check if we should use fake camera
        if self._cfg.get("use_fake", False):
            LOG.info("Using FakeCamera (fake hardware mode enabled)")
            self._capture = FakeCamera()  # type: ignore
            self._last_error = None
            return self._capture  # type: ignore
        
        device = self._cfg.get("device", 0)
        # Accept either an integer index or a device path string like '/dev/video0'
        use_device = device
        try:
            if isinstance(device, str) and device.isdigit():
                use_device = int(device)
            elif isinstance(device, (int, float)):
                use_device = int(device)
        except Exception:
            use_device = device
        
        LOG.debug(f"Attempting to open camera device: {use_device}")
        
        # cv2.VideoCapture accepts either an int index or a string device path.
        api_prefs = [None]
        v4l2_pref = getattr(cv2, "CAP_V4L2", None)
        if v4l2_pref is not None:
            api_prefs.append(v4l2_pref)
        last_exc: Optional[Exception] = None
        cap: Optional[cv2.VideoCapture] = None
        for api_pref in api_prefs:
            try:
                if api_pref is None:
                    LOG.debug(f"Trying to open camera {use_device} with default API")
                    cap_candidate = cv2.VideoCapture(use_device) if isinstance(use_device, int) else cv2.VideoCapture(str(use_device))
                else:
                    LOG.debug(f"Trying to open camera {use_device} with API preference: {api_pref}")
                    cap_candidate = cv2.VideoCapture(use_device, api_pref) if isinstance(use_device, int) else cv2.VideoCapture(str(use_device), api_pref)
            except Exception as exc:  # pragma: no cover - depends on system drivers
                LOG.debug(f"Failed to create VideoCapture for {use_device}: {exc}")
                last_exc = exc
                continue
            if cap_candidate and cap_candidate.isOpened():
                LOG.debug(f"Successfully opened camera {use_device}")
                cap = cap_candidate
                break
            if cap_candidate:
                try:
                    cap_candidate.release()
                except Exception:
                    pass
        if not cap or not cap.isOpened():
            if last_exc is not None:
                self._last_error = f"Camera open failed for {device}: {last_exc}"
            else:
                self._last_error = f"Unable to open camera device {device} (device may not exist or be in use)"
            LOG.error(self._last_error)
            fallback = self._cfg.get("fallback_image")
            if fallback:
                LOG.warning("Camera device %s unavailable, will use fallback image %s", device, fallback)
            self._capture = None
            return None
        
        # Configure camera properties
        width, height = self._cfg.get("resolution", (1280, 720))
        if width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        if height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        fps = self._cfg.get("fps")
        if fps:
            cap.set(cv2.CAP_PROP_FPS, float(fps))
        
        # Verify actual settings (may differ from requested)
        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = int(cap.get(cv2.CAP_PROP_FPS))
        LOG.info(f"Camera {device} opened: {actual_width}x{actual_height} @ {actual_fps}fps")
        
        self._capture = cap
        self._last_error = None
        return cap
    def _read_frame_locked(self) -> np.ndarray:
        cap = self._ensure_capture_locked()
        if cap is None or not cap.isOpened():
            if self._last_error is None:
                self._last_error = "Camera frame unavailable"
            return self._load_fallback_frame()
        ok, frame = cap.read()
        if not ok or frame is None:
            LOG.warning("Camera read failed; using fallback frame")
            self._last_error = "Camera read failed"
            return self._load_fallback_frame()
        self._last_frame = frame
        self._last_ts = time.time()
        self._last_error = None
        return frame

    def _load_fallback_frame(self) -> np.ndarray:
        fb = self._cfg.get("fallback_image")
        if not fb:
            raise RuntimeError("No camera frame available and no fallback image configured")
        frame = cv2.imread(fb, cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError(f"Fallback image could not be loaded: {fb}")
        self._last_frame = frame
        self._last_ts = time.time()
        self._last_error = None
        return frame

    def grab_frame_sync(self, max_age: float = 0.0) -> np.ndarray:
        with self._lock:
            if max_age > 0 and self._last_frame is not None:
                if (time.time() - self._last_ts) <= max_age:
                    return self._last_frame.copy()
            return self._read_frame_locked().copy()

    async def grab_frame(self, max_age: float = 0.0) -> np.ndarray:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.grab_frame_sync, max_age)

    def encode_jpeg(self, frame: np.ndarray, quality: int = 80) -> bytes:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        ok, buf = cv2.imencode('.jpg', frame, params)
        if not ok or buf is None:
            raise RuntimeError("Failed to encode frame to JPEG")
        return buf.tobytes()

    async def grab_jpeg(self, quality: int = 80, max_age: float = 0.0) -> bytes:
        frame = await self.grab_frame(max_age=max_age)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.encode_jpeg, frame, quality)

    def snapshot_for_ocr(self) -> np.ndarray:
        return self.grab_frame_sync(max_age=0.2)


def list_devices(max_index: int = 10) -> Dict[str, Any]:
    """Probe likely camera devices and return a summary.

    Returns mapping with keys 'candidates' -> list of dicts {id,type,available,info}
    where id is either an index (int) or device path (str).
    """
    candidates = []
    
    # Probe numeric indices - this is more reliable than parsing device paths
    LOG.info(f"Scanning camera devices 0-{max_index}...")
    for i in range(0, max_index + 1):
        available = False
        width = None
        height = None
        fps = None
        backend = "unknown"
        
        try:
            # Try opening with numeric index
            cap = cv2.VideoCapture(int(i))
            if cap and cap.isOpened():
                available = True
                # Try to get camera properties
                try:
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    fps = int(cap.get(cv2.CAP_PROP_FPS))
                    # Try to get backend name
                    backend_id = cap.get(cv2.CAP_PROP_BACKEND)
                    if backend_id == cv2.CAP_V4L2:
                        backend = "V4L2"
                    elif backend_id == cv2.CAP_ANY:
                        backend = "Any"
                except Exception as e:
                    LOG.debug(f"Could not get properties for camera {i}: {e}")
                    pass
                
                # Try to actually read a frame to verify it works
                try:
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        LOG.warning(f"Camera {i} opened but failed to read frame")
                        available = False
                except Exception as e:
                    LOG.warning(f"Camera {i} opened but failed to read: {e}")
                    available = False
                    
            try:
                cap.release()
            except Exception:
                pass
                
        except Exception as e:
            LOG.debug(f"Failed to open camera {i}: {e}")
            available = False
        
        if available:
            device_info = {
                'id': i,
                'type': 'index',
                'available': True,
                'resolution': f"{width}x{height}" if width and height else "unknown",
                'fps': fps if fps and fps > 0 else "unknown",
                'backend': backend,
                'device_index': i,
                'path': f"/dev/video{i}"
            }
            candidates.append(device_info)
            LOG.info(f"Found camera {i}: {device_info['resolution']} @ {device_info['fps']}fps ({backend})")

    LOG.info(f"Camera scan complete: found {len(candidates)} available camera(s)")
    
    # Sort by device index to get consistent ordering
    candidates.sort(key=lambda x: x['device_index'])
    
    return {
        'candidates': candidates,
        'count': len(candidates),
        'recommended': candidates[0]['id'] if candidates else None
    }


_manager: Optional[CameraManager] = None


def get_manager() -> CameraManager:
    global _manager
    if _manager is None:
        _manager = CameraManager()
    return _manager


def configure(cfg: Optional[Dict[str, Any]]) -> None:
    mgr = get_manager()
    mgr.configure(cfg)


def close() -> None:
    mgr = get_manager()
    mgr.close()