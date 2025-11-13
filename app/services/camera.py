import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple
import glob
import os

import cv2  # type: ignore
import numpy as np  # type: ignore[import-not-found]

LOG = logging.getLogger("sort.camera")


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
            })
            self._dispose_locked()
            self._last_error = None
            LOG.info("Camera configured: device=%s resolution=%s fps=%s", self._cfg["device"], self._cfg["resolution"], self._cfg.get("fps"))

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
                    cap_candidate = cv2.VideoCapture(use_device) if isinstance(use_device, int) else cv2.VideoCapture(str(use_device))
                else:
                    cap_candidate = cv2.VideoCapture(use_device, api_pref) if isinstance(use_device, int) else cv2.VideoCapture(str(use_device), api_pref)
            except Exception as exc:  # pragma: no cover - depends on system drivers
                last_exc = exc
                continue
            if cap_candidate and cap_candidate.isOpened():
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
                self._last_error = f"Unable to open camera device {device}"
            fallback = self._cfg.get("fallback_image")
            if fallback:
                LOG.warning("Camera device %s unavailable, will use fallback image %s", device, fallback)
            self._capture = None
            return None
        width, height = self._cfg.get("resolution", (1280, 720))
        if width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        if height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        fps = self._cfg.get("fps")
        if fps:
            cap.set(cv2.CAP_PROP_FPS, float(fps))
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


def list_devices(max_index: int = 4) -> Dict[str, Any]:
    """Probe likely camera devices and return a summary.

    Returns mapping with keys 'candidates' -> list of dicts {id,type,available}
    where id is either an index (int) or device path (str).
    """
    candidates = []
    # include /dev/video* entries first (Linux)
    for path in sorted(glob.glob('/dev/video*')):
        available = False
        try:
            cap = cv2.VideoCapture(path)
            available = bool(cap and cap.isOpened())
            try:
                cap.release()
            except Exception:
                pass
        except Exception:
            available = False
        candidates.append({'id': path, 'type': 'dev', 'available': available})

    # probe numeric indices up to max_index
    for i in range(0, max_index + 1):
        # skip if same path already listed
        if any(str(c.get('id')) == str(i) for c in candidates):
            continue
        available = False
        try:
            cap = cv2.VideoCapture(int(i))
            available = bool(cap and cap.isOpened())
            try:
                cap.release()
            except Exception:
                pass
        except Exception:
            available = False
        candidates.append({'id': i, 'type': 'index', 'available': available})

    return {'candidates': candidates}


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