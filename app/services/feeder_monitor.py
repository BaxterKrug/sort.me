import asyncio
import logging
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import cv2  # type: ignore
import numpy as np

from . import camera

LOG = logging.getLogger("sort.feeder_monitor")


@dataclass
class FeederRegion:
    cell: str
    roi: Tuple[float, float, float, float]
    empty_ratio: float = 0.06
    history: int = 5

    def pixels(self, frame_shape: Sequence[int]) -> Tuple[int, int, int, int]:
        if len(frame_shape) < 2:
            raise ValueError("frame_shape must include height and width")
        h, w = frame_shape[0], frame_shape[1]
        x0, y0, x1, y1 = self.roi
        # allow normalized coordinates (0..1) or absolute pixel coordinates (>1)
        if max(self.roi) <= 1.0:
            x0 = int(x0 * w)
            y0 = int(y0 * h)
            x1 = int(x1 * w)
            y1 = int(y1 * h)
        else:
            x0 = int(x0)
            y0 = int(y0)
            x1 = int(x1)
            y1 = int(y1)
        # clamp to frame bounds
        x0 = max(0, min(w - 1, x0))
        y0 = max(0, min(h - 1, y0))
        x1 = max(x0 + 1, min(w, x1))
        y1 = max(y0 + 1, min(h, y1))
        return x0, y0, x1, y1


class FeederMonitor:
    """Detect whether feeders still contain cards based on the camera feed."""

    def __init__(self, regions: Sequence[FeederRegion]) -> None:
        self.regions: List[FeederRegion] = list(regions)
        self._history: Dict[str, Deque[float]] = {r.cell: deque(maxlen=r.history) for r in self.regions}
        self.last_results: Dict[str, Dict[str, Any]] = {}
        self.enabled: bool = bool(self.regions)

    async def measure(self, target_cells: Optional[Sequence[str]] = None, frame: Optional[np.ndarray] = None) -> Dict[str, Dict[str, Any]]:
        if not self.enabled:
            return {}
        if frame is None:
            frame = await camera.get_manager().grab_frame()
        results: Dict[str, Dict[str, Any]] = {}
        h, w = frame.shape[:2]
        cells = set(c.upper() for c in target_cells) if target_cells else None
        for region in self.regions:
            if cells and region.cell.upper() not in cells:
                continue
            x0, y0, x1, y1 = region.pixels(frame.shape)
            crop = frame[y0:y1, x0:x1]
            ratio = self._compute_fill_ratio(crop)
            history = self._history.get(region.cell)
            if history is None:
                history = deque(maxlen=region.history)
                self._history[region.cell] = history
            history.append(ratio)
            smooth = statistics.median(history) if history else ratio
            empty = bool(smooth < region.empty_ratio)
            w_safe = w or 1
            h_safe = h or 1
            results[region.cell] = {
                "cell": region.cell,
                "fill_ratio": ratio,
                "smoothed_ratio": smooth,
                "threshold": region.empty_ratio,
                "empty": empty,
                "timestamp": asyncio.get_running_loop().time(),
                "roi": {
                    "x0": x0 / w_safe,
                    "y0": y0 / h_safe,
                    "x1": x1 / w_safe,
                    "y1": y1 / h_safe,
                },
            }
        self.last_results = results
        return results

    async def measure_single(self, cell: str) -> Optional[Dict[str, Any]]:
        data = await self.measure(target_cells=[cell])
        return data.get(cell)

    @staticmethod
    def _compute_fill_ratio(crop: np.ndarray) -> float:
        if crop.size == 0:
            return 0.0
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # compute ratio of bright pixels
        filled = float(cv2.countNonZero(mask))
        total = float(mask.size)
        if total <= 0:
            return 0.0
        return filled / total


_monitor: Optional[FeederMonitor] = None


def _parse_roi(raw: Any) -> Tuple[float, float, float, float]:
    if isinstance(raw, dict):
        if {"x", "y", "w", "h"}.issubset(raw.keys()):
            x = float(raw["x"])
            y = float(raw["y"])
            w = float(raw["w"])
            h = float(raw["h"])
            return (x, y, x + w, y + h)
        if {"x0", "y0", "x1", "y1"}.issubset(raw.keys()):
            return (float(raw["x0"]), float(raw["y0"]), float(raw["x1"]), float(raw["y1"]))
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return tuple(float(v) for v in raw)  # type: ignore
    raise ValueError(f"Unsupported ROI format: {raw}")


def configure_from_cfg(cfg: Any, camera_cfg: Optional[Dict[str, Any]]) -> None:
    global _monitor
    camera_cfg = camera_cfg or {}
    feed_cfg = camera_cfg.get("feeders") if isinstance(camera_cfg, dict) else None
    if not feed_cfg:
        LOG.warning("camera.feeders not defined; feeder monitor disabled")
        _monitor = FeederMonitor([])
        _monitor.enabled = False
        return

    regions: List[FeederRegion] = []
    configured_cells = set()
    for entry in feed_cfg:
        try:
            cell = str(entry.get("cell") or entry.get("id")).strip().upper()
            if not cell:
                continue
            roi = _parse_roi(entry.get("roi") or entry)
            empty_ratio = float(entry.get("empty_threshold", entry.get("empty_ratio", 0.06)))
            history = int(entry.get("history", 5))
            regions.append(FeederRegion(cell=cell, roi=roi, empty_ratio=empty_ratio, history=history))
            configured_cells.add(cell)
        except Exception as exc:
            LOG.warning("Skipping feeder region %s due to error: %s", entry, exc)

    if not regions:
        LOG.warning("No valid feeder regions configured; disabling feeder monitor")
        _monitor = FeederMonitor([])
        _monitor.enabled = False
        return

    _monitor = FeederMonitor(regions)
    LOG.info("Feeder monitor configured for cells: %s", sorted(configured_cells))


def get_monitor() -> Optional[FeederMonitor]:
    return _monitor