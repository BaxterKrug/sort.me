"""Dual-photo OCR helpers for improved card orientation detection."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import cv2  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]

from . import camera as camera_svc
from . import motion

LOG = logging.getLogger("sort.ocr_pipeline")


def _calc_density(frame: np.ndarray) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    lap = cv2.Laplacian(blurred, cv2.CV_64F)
    return float(np.mean(np.abs(lap)))


def _split_frame(frame: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    midpoint = max(1, frame.shape[0] // 2)
    return frame[:midpoint, :], frame[midpoint:, :]


def analyze_orientation(top_frame: np.ndarray, bottom_frame: np.ndarray) -> Dict[str, Any]:
    """Return gradient-based metrics indicating which side is leading."""
    top_upper, top_lower = _split_frame(top_frame)
    bottom_upper, bottom_lower = _split_frame(bottom_frame)

    scores = {
        "top_full_density": _calc_density(top_frame),
        "bottom_full_density": _calc_density(bottom_frame),
        "top_upper_density": _calc_density(top_upper),
        "top_lower_density": _calc_density(top_lower),
        "bottom_upper_density": _calc_density(bottom_upper),
        "bottom_lower_density": _calc_density(bottom_lower),
    }

    determination = "balanced"
    top_signal = scores["top_lower_density"] + scores["top_full_density"]
    bottom_signal = scores["bottom_upper_density"] + scores["bottom_full_density"]
    if top_signal > bottom_signal * 1.08:
        determination = "top_leading"
    elif bottom_signal > top_signal * 1.08:
        determination = "bottom_leading"

    return {
        "method": "gradient-density",
        "determination": determination,
        "segments": {
            "top_upper": {"density": scores["top_upper_density"]},
            "top_lower": {"density": scores["top_lower_density"]},
            "bottom_upper": {"density": scores["bottom_upper_density"]},
            "bottom_lower": {"density": scores["bottom_lower_density"]},
        },
        **scores,
    }


async def capture_dual_snapshot(offset_mm: float = 44.0, save_dir: str = "data/snapshots") -> Dict[str, Any]:
    """Capture two photos (top/bottom), analyse orientation and persist frames."""
    ctrl = motion.get_controller()
    cam = camera_svc.get_manager()
    loop = asyncio.get_running_loop()

    original: Tuple[float, float, float] = (
        float(ctrl.current[0]),
        float(ctrl.current[1]),
        float(ctrl.current[2]),
    )
    frame_top: np.ndarray
    frame_bottom: np.ndarray
    target_y = original[1] + offset_mm

    async with ctrl.lock:
        frame_top = await loop.run_in_executor(None, cam.snapshot_for_ocr)
        try:
            await ctrl.driver.set_speed(ctrl.default_speed)
            await ctrl.driver.move_absolute(original[0], target_y, original[2], ctrl.default_speed)
            ctrl.current = (original[0], target_y, original[2])
            await asyncio.sleep(0.05)
            frame_bottom = await loop.run_in_executor(None, cam.snapshot_for_ocr)
        finally:
            await ctrl.driver.move_absolute(original[0], original[1], original[2], ctrl.default_speed)
            ctrl.current = original

    orientation = analyze_orientation(frame_top, frame_bottom)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")

    save_paths: Dict[str, str] = {}
    try:
        base_dir = Path(save_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        top_path = base_dir / f"{timestamp}_top.jpg"
        bottom_path = base_dir / f"{timestamp}_bottom.jpg"
        await loop.run_in_executor(None, cv2.imwrite, str(top_path), frame_top)
        await loop.run_in_executor(None, cv2.imwrite, str(bottom_path), frame_bottom)
        save_paths = {"top": str(top_path), "bottom": str(bottom_path)}
    except Exception as exc:  # pragma: no cover - best effort persistence
        LOG.warning("Failed to persist OCR snapshots: %s", exc)

    return {
        "success": True,
        "captured_at": timestamp,
        "offset_mm": offset_mm,
        "orientation": orientation,
        "image_paths": save_paths,
        "capture": {
            "top_shape": list(frame_top.shape),
            "bottom_shape": list(frame_bottom.shape),
        },
    }
