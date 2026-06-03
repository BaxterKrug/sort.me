"""OCR pipeline for card processing - applies 90° CCW rotation."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple, Optional, List

import cv2  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
import re
try:  # pragma: no cover - optional dependency
    import pytesseract  # type: ignore[import-not-found]
    from pytesseract import TesseractError  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None  # type: ignore[assignment]

    class TesseractError(RuntimeError):
        """Fallback exception when pytesseract is not available."""

        pass

try:  # pragma: no cover - optional dependency
    import easyocr  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    easyocr = None  # type: ignore[assignment]

from . import camera as camera_svc
from . import motion
from . import card_id
from . import text_clean

LOG = logging.getLogger("sort.ocr_pipeline")
CARD_BORDER_MARGIN_PX = 10  # Balanced margin - not too tight, not too loose
_TESSERACT_PATH = shutil.which("tesseract")
HAVE_TESSERACT = bool(pytesseract and _TESSERACT_PATH)
HAVE_EASYOCR = bool(easyocr)
_OCR_ENGINE_WARNED = False
_EASYOCR_READER = None
_EASYOCR_LOCK = threading.Lock()

COLLECTOR_CHAR_WHITELIST = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz/"
_TESSERACT_REGION_CONFIGS = {
    "full": "--oem 3 --psm 4 -c preserve_interword_spaces=1",
    "name": "--oem 3 --psm 7 -c preserve_interword_spaces=1",
    "oracle": "--oem 3 --psm 4 -c preserve_interword_spaces=1",
    "collector": (
        "--oem 3 --psm 7 -c preserve_interword_spaces=1 "
        f"-c tessedit_char_whitelist={COLLECTOR_CHAR_WHITELIST}"
    ),
}
_REGION_SLICE_MIN_HEIGHT = {
    "full": 420,
    "name": 140,
    "oracle": 360,
    "collector": 110,
}


def _calc_density(frame: np.ndarray) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    lap = cv2.Laplacian(blurred, cv2.CV_64F)
    return float(np.mean(np.abs(lap)))


def _region_config(region: str) -> str:
    return _TESSERACT_REGION_CONFIGS.get(region, _TESSERACT_REGION_CONFIGS["full"])


def _prepare_region_slice(slice_img: np.ndarray, region: str) -> np.ndarray:
    """Prepare image region for OCR - minimal preprocessing."""
    if slice_img.size == 0:
        return slice_img
    
    # Convert to grayscale
    if slice_img.ndim == 3:
        gray = cv2.cvtColor(slice_img, cv2.COLOR_BGR2GRAY)
    else:
        gray = slice_img.copy()
    
    # Ensure minimum height for OCR accuracy - scale up small images
    min_height = _REGION_SLICE_MIN_HEIGHT.get(region, 160)
    if gray.shape[0] < min_height:
        scale = float(min_height) / max(float(gray.shape[0]), 1.0)
        width = max(1, int(round(gray.shape[1] * scale)))
        # Use INTER_LANCZOS4 for best quality when upscaling
        gray = cv2.resize(gray, (width, min_height), interpolation=cv2.INTER_LANCZOS4)
    
    # MINIMAL processing - just return clean grayscale
    # Let Tesseract's internal binarization do the work
    return np.ascontiguousarray(gray)


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


def _normalize_frames(frame_top: np.ndarray, frame_bottom: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Crop both frames to the shared minimum width/height for easier blending."""
    min_height = min(frame_top.shape[0], frame_bottom.shape[0])
    min_width = min(frame_top.shape[1], frame_bottom.shape[1])
    top_aligned = np.ascontiguousarray(frame_top[:min_height, :min_width])
    bottom_aligned = np.ascontiguousarray(frame_bottom[:min_height, :min_width])
    return top_aligned, bottom_aligned


def _estimate_overlap_px(
    top_gray: np.ndarray,
    bottom_gray: np.ndarray,
    *,
    min_ratio: float = 0.30,
    max_ratio: float = 0.60,
    step_px: int = 2,
) -> int:
    """Estimate how many pixels the camera overlap captured between the two frames."""
    height = min(top_gray.shape[0], bottom_gray.shape[0])
    min_overlap = max(8, int(height * min_ratio))
    max_overlap = max(min_overlap, min(height - 8, int(height * max_ratio)))
    if max_overlap <= min_overlap:
        max_overlap = min(height - 8, min_overlap + 12)
    best_overlap = min_overlap
    best_score = float("inf")
    penalty_factor = 18.0

    for overlap in range(min_overlap, max_overlap + 1, max(1, step_px)):
        start_row = max(top_gray.shape[0] - overlap, 0)
        top_slice = top_gray[start_row:, :]
        bottom_slice = bottom_gray[:overlap, :]
        diff = cv2.absdiff(top_slice, bottom_slice)
        score = float(np.mean(np.asarray(diff, dtype=np.float32)))
        if overlap > 0:
            score += penalty_factor / float(overlap)
        if score < best_score:
            best_score = score
            best_overlap = overlap

    return int(best_overlap)


def _composite_frames(frame_top: np.ndarray, frame_bottom: np.ndarray) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    """Combine two vertically shifted frames into a single composite image."""
    top_aligned, bottom_aligned = _normalize_frames(frame_top, frame_bottom)
    top_gray = cv2.cvtColor(top_aligned, cv2.COLOR_BGR2GRAY)
    bottom_gray = cv2.cvtColor(bottom_aligned, cv2.COLOR_BGR2GRAY)

    overlap_px = _estimate_overlap_px(top_gray, bottom_gray)
    max_valid = min(top_aligned.shape[0], bottom_aligned.shape[0]) - 12
    overlap_px = max(12, min(overlap_px, max_valid))

    min_keep_ratio = 0.35
    min_keep_top = max(40, int(top_aligned.shape[0] * min_keep_ratio))
    min_keep_bottom = max(40, int(bottom_aligned.shape[0] * min_keep_ratio))

    max_overlap_top = max(12, top_aligned.shape[0] - min_keep_top)
    max_overlap_bottom = max(12, bottom_aligned.shape[0] - min_keep_bottom)
    overlap_px = min(overlap_px, max_overlap_top, max_overlap_bottom)

    top_crop = top_aligned[: top_aligned.shape[0] - overlap_px, :]
    bottom_crop = bottom_aligned[overlap_px:, :]

    blend_top = top_aligned[top_aligned.shape[0] - overlap_px :, :]
    blend_bottom = bottom_aligned[:overlap_px, :]
    alpha = np.linspace(1.0, 0.0, overlap_px, dtype=np.float32).reshape(-1, 1, 1)
    blend = (blend_top.astype(np.float32) * alpha + blend_bottom.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)

    composite = np.vstack([bottom_crop, blend, top_crop])

    width = composite.shape[1]
    mask_top = np.zeros((top_crop.shape[0], width), dtype=np.float32)
    mask_blend = np.linspace(0.0, 1.0, overlap_px, dtype=np.float32).reshape(-1, 1)
    mask_blend = np.tile(mask_blend, (1, width))
    mask_bottom = np.ones((bottom_crop.shape[0], width), dtype=np.float32)
    source_mask = np.vstack([mask_top, mask_blend, mask_bottom])
    meta = {
        "overlap_px": int(overlap_px),
        "top_rows_used": int(top_crop.shape[0] + overlap_px),
        "bottom_rows_used": int(bottom_crop.shape[0] + overlap_px),
        "width": int(composite.shape[1]),
        "height": int(composite.shape[0]),
    }
    return composite, source_mask, meta


def _header_band_activity(image: np.ndarray, band_ratio: float = 0.22) -> Dict[str, float]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height = gray.shape[0]
    band = max(16, int(height * band_ratio))
    top_slice = gray[:band, :]
    bottom_slice = gray[-band:, :]

    sobel = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    sobel_abs = np.abs(sobel)
    top_energy = float(np.mean(np.asarray(sobel_abs[:band, :], dtype=np.float32)))
    bottom_energy = float(np.mean(np.asarray(sobel_abs[-band:, :], dtype=np.float32)))

    top_slice_f = np.asarray(top_slice, dtype=np.float32)
    bottom_slice_f = np.asarray(bottom_slice, dtype=np.float32)

    top_contrast = float(np.std(top_slice_f))
    bottom_contrast = float(np.std(bottom_slice_f))
    top_brightness = float(np.mean(top_slice_f))
    bottom_brightness = float(np.mean(bottom_slice_f))

    top_score = top_energy * 0.7 + top_contrast * 0.25 + (255.0 - abs(top_brightness - bottom_brightness)) * 0.05
    bottom_score = bottom_energy * 0.7 + bottom_contrast * 0.25 + abs(top_brightness - bottom_brightness) * 0.05

    return {
        "band_height": band,
        "top_energy": top_energy,
        "bottom_energy": bottom_energy,
        "top_contrast": top_contrast,
        "bottom_contrast": bottom_contrast,
        "top_brightness": top_brightness,
        "bottom_brightness": bottom_brightness,
        "top_score": top_score,
        "bottom_score": bottom_score,
    }


def _order_box_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]  # top-left
    rect[2] = pts[np.argmax(s)]  # bottom-right
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # top-right
    rect[3] = pts[np.argmax(diff)]  # bottom-left
    return rect


def _enhance_for_border_detection(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(6, 6))
    equalized = clahe.apply(gray)
    blurred = cv2.GaussianBlur(equalized, (5, 5), 0)
    binary = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        7,
    )
    cleaned = cv2.medianBlur(binary, 3)
    return cleaned


def _visualize_edge_detection(image: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Create a visualization of the edge detection process for debugging."""
    # Enhance for border detection
    processed = _enhance_for_border_detection(image)
    
    # Canny edge detection
    edges = cv2.Canny(processed, 50, 150)
    
    # Use moderate dilation to connect card edges
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges_dilated = cv2.dilate(edges, kernel, iterations=2)
    
    # Find contours
    contours, _ = cv2.findContours(edges_dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    # Create a color visualization
    vis = cv2.cvtColor(image.copy(), cv2.COLOR_BGR2RGB)
    
    meta = {
        "num_contours": len(contours),
        "found": False,
        "area": 0.0,
    }
    
    if contours:
        # Find the largest contour
        contour = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(contour)
        
        if area >= 0.2 * image.shape[0] * image.shape[1]:
            meta["area"] = float(area)
            meta["found"] = True
            
            # Approximate the contour to get main edges
            epsilon = 0.005 * cv2.arcLength(contour, True)  # Tighter approximation
            approx = cv2.approxPolyDP(contour, epsilon, True)
            
            # Draw the approximated contour in green
            cv2.drawContours(vis, [approx], -1, (0, 255, 0), 3)
            
            # Use the approximated polygon to find the convex hull for a clean rectangle
            hull = cv2.convexHull(approx)
            
            # Get the minimum area rectangle from the convex hull (follows longest edges)
            rect = cv2.minAreaRect(hull)
            box = cv2.boxPoints(rect)
            box = np.array(box, dtype=np.int32)
            
            # Alternatively, use straight bounding rect if you want axis-aligned
            # x, y, w, h = cv2.boundingRect(hull)
            # box = np.array([[x, y], [x+w, y], [x+w, y+h], [x, y+h]], dtype=np.int32)
            
            # Draw the rectangle based on longest edges (red box)
            cv2.drawContours(vis, [box], -1, (255, 0, 0), 2)
            
            # Draw corner points
            for point in box:
                cv2.circle(vis, tuple(point), 8, (255, 255, 0), -1)
                
            meta["rect_box"] = box.tolist()
    
    # Convert back to BGR for consistency
    vis = cv2.cvtColor(vis, cv2.COLOR_RGB2BGR)
    
    return vis, meta


def _warp_card_to_bounds(
    image: np.ndarray,
    source_mask: np.ndarray,
    *,
    border_margin_px: int = CARD_BORDER_MARGIN_PX,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    processed = _enhance_for_border_detection(image)
    edges = cv2.Canny(processed, 40, 160)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, kernel, iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    meta: Dict[str, Any] = {
        "found": False,
        "area": 0.0,
        "margin_px": int(border_margin_px),
    }
    if not contours:
        return image, source_mask, meta

    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    meta["area"] = area
    if area < 0.25 * image.shape[0] * image.shape[1]:
        return image, source_mask, meta

    rect = cv2.minAreaRect(contour)
    ((cx, cy), (rw, rh), angle) = rect
    margin = float(max(0, border_margin_px))
    expanded_rect = ((cx, cy), (max(rw, 1.0) + margin * 2.0, max(rh, 1.0) + margin * 2.0), angle)
    box = cv2.boxPoints(expanded_rect)
    box[:, 0] = np.clip(box[:, 0], 0, image.shape[1] - 1)
    box[:, 1] = np.clip(box[:, 1], 0, image.shape[0] - 1)
    box = _order_box_points(box)
    width = int(round(np.linalg.norm(box[0] - box[1])))
    height = int(round(np.linalg.norm(box[0] - box[3])))
    if width == 0 or height == 0:
        return image, source_mask, meta

    # Preserve the original aspect ratio - don't force portrait here
    # Rotation will be handled separately by _orient_composite
    dest_width = max(width, 1)
    dest_height = max(height, 1)

    dst = np.array(
        [
            [0, 0],
            [dest_width - 1, 0],
            [dest_width - 1, dest_height - 1],
            [0, dest_height - 1],
        ],
        dtype=np.float32,
    )
    M = cv2.getPerspectiveTransform(box.astype(np.float32), dst)
    warped = cv2.warpPerspective(
        image,
        M,
        (dest_width, dest_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    warped_mask = cv2.warpPerspective(
        source_mask.astype(np.float32),
        M,
        (dest_width, dest_height),
        borderMode=cv2.BORDER_REPLICATE,
    )
    # Don't auto-rotate here - let _orient_composite handle rotation
    meta.update(
        {
            "found": True,
            "box": box.tolist(),
            "dest_width": int(warped.shape[1]),
            "dest_height": int(warped.shape[0]),
            "margin_px": int(margin),
        }
    )
    return warped, warped_mask, meta


def _text_band_features(image: np.ndarray) -> Dict[str, Any]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sobel = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    row_energy = np.mean(np.abs(sobel), axis=1)
    if row_energy.size == 0:
        return {
            "segments": [],
            "name_band": None,
            "rules_band": None,
            "gap_ok": False,
            "score": -0.25,
        }
    smooth = cv2.GaussianBlur(row_energy.reshape(-1, 1), (1, 5), 0).reshape(-1)
    min_e = float(np.min(smooth))
    max_e = float(np.max(smooth))
    denom = max(max_e - min_e, 1e-6)
    norm = (smooth - min_e) / denom
    threshold = float(max(0.2, min(0.85, np.percentile(norm, 65))))
    active = norm > threshold
    segments = []
    start = None
    for idx, flag in enumerate(active.tolist()):
        if flag and start is None:
            start = idx
        elif not flag and start is not None:
            segments.append((start, idx - 1))
            start = None
    if start is not None:
        segments.append((start, len(active) - 1))

    height = float(len(norm))
    info = []
    for seg_start, seg_end in segments:
        seg_len = seg_end - seg_start + 1
        info.append(
            {
                "start": seg_start,
                "end": seg_end,
                "length": seg_len,
                "start_norm": seg_start / max(height - 1.0, 1.0),
                "end_norm": seg_end / max(height - 1.0, 1.0),
                "center_norm": (seg_start + seg_end) / 2.0 / max(height - 1.0, 1.0),
            }
        )

    name_band = next((seg for seg in info if seg["start_norm"] <= 0.22), None)
    rules_band = next((seg for seg in reversed(info) if seg["end_norm"] >= 0.62), None)
    gap_ok = False
    score = -0.1
    if name_band and rules_band:
        gap = rules_band["start_norm"] - name_band["end_norm"]
        gap_ok = gap >= 0.28
        score = 0.15
        if gap_ok:
            score += 0.25
        if name_band["length"] / height < 0.15:
            score += 0.05
    return {
        "segments": info,
        "name_band": name_band,
        "rules_band": rules_band,
        "gap_ok": gap_ok,
        "score": score,
    }


def _detect_text_bands_gray(gray: np.ndarray) -> Dict[str, Any]:
    height = int(gray.shape[0]) if gray.ndim >= 2 else 0
    info: Dict[str, Any] = {
        "segments": [],
        "inactive_segments": [],
        "name_band": None,
        "rules_band": None,
        "gap_rows": 0,
        "height": height,
    }
    if height <= 0:
        return info
    sobel = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    row_energy = np.mean(np.abs(sobel), axis=1)
    smooth = cv2.GaussianBlur(row_energy.reshape(-1, 1), (1, 9), 0).reshape(-1)
    min_e = float(np.min(smooth))
    max_e = float(np.max(smooth))
    denom = max(max_e - min_e, 1e-3)
    norm = (smooth - min_e) / denom
    threshold = float(np.percentile(norm, 60))
    threshold = min(max(threshold, 0.18), 0.6)
    active = norm > threshold
    segments = []
    start = None
    active_list = active.tolist()
    for idx, flag in enumerate(active_list):
        if flag and start is None:
            start = idx
        elif not flag and start is not None:
            segments.append((start, idx - 1))
            start = None
    if start is not None:
        segments.append((start, len(active_list) - 1))

    inactive_segments = []
    start = None
    for idx, flag in enumerate(active_list):
        inactive = not flag
        if inactive and start is None:
            start = idx
        elif not inactive and start is not None:
            inactive_segments.append((start, idx - 1))
            start = None
    if start is not None:
        inactive_segments.append((start, len(active_list) - 1))

    norm_denom = max(height - 1, 1)

    def _segment_dict(seg_start: int, seg_end: int) -> Dict[str, Any]:
        seg_len = seg_end - seg_start + 1
        return {
            "start": int(seg_start),
            "end": int(seg_end),
            "length": int(seg_len),
            "start_norm": float(seg_start / norm_denom),
            "end_norm": float(seg_end / norm_denom),
        }

    info["segments"] = [_segment_dict(a, b) for a, b in segments]
    info["inactive_segments"] = [_segment_dict(a, b) for a, b in inactive_segments]
    name_band = next((seg for seg in info["segments"] if seg["start_norm"] < 0.28), None)
    rules_band = None
    if info["segments"]:
        min_gap_rows = int(height * 0.02)
        for seg in reversed(info["segments"]):
            if seg["end_norm"] <= 0.4:
                continue
            if name_band and seg["start"] <= name_band["end"] + min_gap_rows:
                continue
            rules_band = seg
            break
        if rules_band is None:
            rules_band = next((seg for seg in reversed(info["segments"]) if seg["end_norm"] > 0.55), None)
    gap_rows = 0
    if name_band and rules_band:
        gap_rows = max(0, int(rules_band["start"] - name_band["end"]))
    info.update(
        {
            "name_band": name_band,
            "rules_band": rules_band,
            "gap_rows": int(gap_rows),
        }
    )
    return info


def _mask_artwork_rows(gray: np.ndarray, bands: Dict[str, Any]) -> Tuple[np.ndarray, Dict[str, Any]]:
    height = int(gray.shape[0]) if gray.ndim >= 2 else 0
    meta: Dict[str, Any] = {
        "masked": False,
        "art_band_start": None,
        "art_band_end": None,
        "gap_rows": int(bands.get("gap_rows") or 0),
        "height": height,
        "source": None,
    }
    name_band = bands.get("name_band") or {}
    rules_band = bands.get("rules_band") or {}
    if height <= 0:
        return gray, meta

    inactive_segments = bands.get("inactive_segments") or []
    min_length = max(10, int(height * 0.08))
    padding = max(4, int(height * 0.015))
    art_segment = None
    if inactive_segments and name_band and rules_band:
        ordered = sorted(inactive_segments, key=lambda seg: seg["length"], reverse=True)
        for seg in ordered:
            if seg["length"] < min_length:
                continue
            center = (seg["start_norm"] + seg["end_norm"]) / 2.0
            if 0.2 <= center <= 0.75:
                art_segment = seg
                meta["source"] = "inactive"
                break

    if art_segment is None and name_band and rules_band:
        gap_rows = int(rules_band["start"] - name_band["end"])
        min_gap = max(8, int(height * 0.08))
        if gap_rows > min_gap:
            art_segment = {
                "start": int(name_band["end"]),
                "end": int(rules_band["start"]),
            }
            meta["source"] = "gap"

    if art_segment is None:
        return gray, meta

    start = min(height, max(0, int(art_segment["start"]) + padding))
    end = min(height, max(start, int(art_segment.get("end", start)) - padding))
    if end - start <= max(6, int(height * 0.03)):
        return gray, meta
    masked = gray.copy()
    masked[start:end, :] = 255
    meta.update(
        {
            "masked": True,
            "art_band_start": int(start),
            "art_band_end": int(end),
            "gap_rows": int(rules_band.get("start", start) - name_band.get("end", start))
            if name_band and rules_band
            else meta["gap_rows"],
        }
    )
    return masked, meta


def _derive_region_rows(height: int, bands: Dict[str, Any]) -> Dict[str, Tuple[int, int]]:
    rows: Dict[str, Tuple[int, int]] = {}
    if height <= 0:
        return rows

    min_span = max(10, int(height * 0.025))
    pad_small = max(4, int(height * 0.01))
    pad_large = max(pad_small * 3, int(height * 0.05))
    norm_denom = max(height - 1, 1)

    def _clamp(start: int, end: int) -> Tuple[int, int]:
        start = max(0, min(start, height - 1))
        end = max(start + min_span, min(end, height))
        return int(start), int(end)

    def _segment_rows(seg: Dict[str, Any]) -> Tuple[int, int]:
        start = int(round(float(seg.get("start_norm", 0.0)) * norm_denom))
        end = int(round(float(seg.get("end_norm", 0.0)) * norm_denom))
        return _clamp(start - pad_small, end + pad_small)

    segments = bands.get("segments") or []
    name_band = bands.get("name_band")
    rules_band = bands.get("rules_band")

    if name_band:
        rows["name"] = _segment_rows(name_band)
    default_name_end = _clamp(0, int(height * 0.22))[1]
    rows.setdefault("name", (0, default_name_end))

    if rules_band:
        start, end = _segment_rows(rules_band)
        rows["oracle"] = _clamp(max(start - pad_small, rows["name"][1] + pad_small), end + pad_large)

    if "oracle" not in rows:
        lower_bound = max(rows["name"][1] + pad_small, int(height * 0.28))
        rows["oracle"] = _clamp(lower_bound, int(height * 0.8))

    collector_band = None
    for seg in reversed(segments):
        if float(seg.get("end_norm", 0.0)) < 0.72:
            continue
        collector_band = seg
        break
    if collector_band:
        start, end = _segment_rows(collector_band)
        rows["collector"] = _clamp(start, end + pad_small)
    else:
        rows["collector"] = _clamp(int(height * 0.8), height)

    if rows["collector"][0] <= rows["oracle"][1]:
        rows["oracle"] = _clamp(rows["oracle"][0], rows["collector"][0] - pad_small)

    rows["full"] = (0, height)
    return rows


def _orientation_candidate_metrics(image: np.ndarray, source_mask: np.ndarray) -> Dict[str, Any]:
    height = image.shape[0]
    upper = image[: height // 2, :]
    lower = image[height // 2 :, :]
    density_upper = _calc_density(upper) if upper.size else 0.0
    density_lower = _calc_density(lower) if lower.size else 0.0
    header_stats = _header_band_activity(image)

    band = max(8, int(source_mask.shape[0] * 0.2))
    mask_top_mean = float(np.mean(source_mask[:band, :])) if band > 0 else 0.0
    mask_bottom_mean = float(np.mean(source_mask[-band:, :])) if band > 0 else 1.0
    mask_score = float(mask_top_mean - mask_bottom_mean)

    top_score = header_stats["top_score"] + 1e-3
    bottom_score = header_stats["bottom_score"] + 1e-3
    ratio_header = top_score / bottom_score

    density_upper_b = density_upper + 1e-3
    density_lower_b = density_lower + 1e-3
    ratio_density = density_upper_b / density_lower_b

    energy_ratio = (header_stats["top_energy"] + 1e-3) / (header_stats["bottom_energy"] + 1e-3)

    score = float(ratio_header * 0.65 + ratio_density * 0.25 + energy_ratio * 0.10)
    text_features = _text_band_features(image)

    return {
        "header": header_stats,
        "density_upper": float(density_upper),
        "density_lower": float(density_lower),
        "ratio_header": float(ratio_header),
        "ratio_density": float(ratio_density),
        "energy_ratio": float(energy_ratio),
        "text_features": text_features,
        "mask_top_mean": float(mask_top_mean),
        "mask_bottom_mean": float(mask_bottom_mean),
        "mask_score": mask_score,
        "score": score,
    }


def _orient_composite(
    image: np.ndarray,
    source_mask: np.ndarray,
    hint: Optional[Dict[str, Any]] = None,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Rotate the composite so card name faces up and portrait orientation is enforced."""
    meta: Dict[str, Any] = {
        "rotated_90": True,
        "rotated_180": False,
        "hint_determination": (hint or {}).get("determination"),
    }

    hint_det = str(meta["hint_determination"] or "").lower()

    # Always rotate 90 degrees counterclockwise first
    rotated_90 = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    mask_90 = cv2.rotate(source_mask, cv2.ROTATE_90_COUNTERCLOCKWISE)
    
    candidates = []
    rotations = {
        "90_only": rotated_90,
        "90_plus_180": cv2.rotate(rotated_90, cv2.ROTATE_180),
    }
    mask_rotations = {
        "90_only": mask_90,
        "90_plus_180": cv2.rotate(mask_90, cv2.ROTATE_180),
    }
    for direction, candidate_img in rotations.items():
        metrics = _orientation_candidate_metrics(candidate_img, mask_rotations[direction])
        bias = 0.0
        if hint_det == "top_leading":
            bias += 0.02
        elif hint_det == "bottom_leading":
            bias -= 0.02
        mask_bonus = metrics["mask_score"] * 2.2
        text_bonus = metrics["text_features"].get("score", 0.0)
        score = metrics["score"] + mask_bonus + text_bonus + bias
        candidates.append({
            "direction": direction,
            "score": float(score),
            "score_raw": float(metrics["score"]),
            "bias": float(bias),
            "mask_bonus": float(mask_bonus),
            "text_bonus": float(text_bonus),
            **metrics,
        })

    best_candidate = max(candidates, key=lambda c: c["score"])
    best_direction = best_candidate["direction"]
    oriented = rotations[best_direction]
    meta["rotation_direction"] = best_direction
    meta["rotated_90"] = True  # Always rotate 90 degrees
    meta["rotated_180"] = best_direction == "90_plus_180"
    meta["header_activity_initial"] = candidates[0]["header"]
    meta["density_upper_initial"] = candidates[0]["density_upper"]
    meta["density_lower_initial"] = candidates[0]["density_lower"]
    meta["density_upper_final"] = best_candidate["density_upper"]
    meta["density_lower_final"] = best_candidate["density_lower"]
    meta["header_activity_final"] = best_candidate["header"]
    meta["mask_stats"] = {
        "top_mean": best_candidate["mask_top_mean"],
        "bottom_mean": best_candidate["mask_bottom_mean"],
        "score": best_candidate["mask_score"],
        "bonus": best_candidate.get("mask_bonus", 0.0),
    }
    meta["text_band"] = best_candidate.get("text_features")
    meta["orientation_candidates"] = candidates
    meta["rotated"] = {
        "direction": best_direction,
        "selected_score": best_candidate["score"],
        "scores": {c["direction"]: c["score"] for c in candidates},
    }

    return oriented, meta


def _prepare_for_ocr(image: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Enhance and binarize the composite for OCR accuracy."""
    portrait_image = image
    portrait_rotation = None
    if image.ndim >= 2 and image.shape[1] > image.shape[0]:
        portrait_image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        portrait_rotation = "rotate_90_ccw"

    if portrait_image.ndim == 2:
        gray = portrait_image
    else:
        gray = cv2.cvtColor(portrait_image, cv2.COLOR_BGR2GRAY)

    height, width = gray.shape[:2]
    target_height = 1400.0
    scale = max(1.0, target_height / float(max(height, 1)))
    resized = cv2.resize(gray, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_CUBIC)

    text_bands = _detect_text_bands_gray(resized)
    # Skip artwork masking since we use zone-based OCR now
    # masked_gray, art_mask_meta = _mask_artwork_rows(resized, text_bands)
    art_mask_meta = {"masked": False, "source": "disabled"}

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    equalized = clahe.apply(resized)
    blurred = cv2.GaussianBlur(equalized, (5, 5), 0)
    binary = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        7,
    )
    cleaned = cv2.medianBlur(binary, 3)
    text_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3))
    enhanced = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, text_kernel, iterations=1)

    region_rows = _derive_region_rows(enhanced.shape[0], text_bands)
    region_rows_serializable = {key: [int(start), int(end)] for key, (start, end) in region_rows.items()}

    meta = {
        "scale": float(round(scale, 3)),
        "method": "adaptive_gaussian",
        "block_size": 31,
        "constant": 7,
        "clip_limit": 2.0,
        "median_kernel": 3,
        "output_height": int(cleaned.shape[0]),
        "output_width": int(cleaned.shape[1]),
        "portrait_rotated": bool(portrait_rotation),
        "portrait_rotation": portrait_rotation or "none",
        "input_shape": list(image.shape),
        "portrait_shape": list(portrait_image.shape),
        "text_band_features": text_bands,
        "art_mask": art_mask_meta,
        "region_rows": region_rows_serializable,
        "region_rows_source": "text_bands" if region_rows else "default",
    }
    return enhanced, meta


def _segment_ocr_regions(
    image: np.ndarray,
    region_hints: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Dict[str, Tuple[int, int]]:
    height = image.shape[0]
    if height <= 0:
        return {"full": (0, 0)}

    def _clamp(start: int, end: int) -> Tuple[int, int]:
        start = max(0, min(start, height - 1))
        end = max(start + 4, min(end, height))
        return int(start), int(end)

    defaults = {
        "full": (0, height),
        "name": (0, max(int(height * 0.25), 1)),
        "oracle": (max(int(height * 0.25) - 5, 0), max(int(height * 0.78), int(height * 0.25) + 1)),
        "set_symbol": (max(int(height * 0.45), 1), max(int(height * 0.62), int(height * 0.45) + 1)),
        "collector": (max(int(height * 0.78) - 5, 0), height),
    }

    if region_hints:
        for key, value in region_hints.items():
            if key not in defaults or not isinstance(value, (tuple, list)):
                continue
            if len(value) != 2:
                continue
            start, end = int(value[0]), int(value[1])
            defaults[key] = _clamp(start, end)
    return {key: _clamp(*value) for key, value in defaults.items()}


def _easyocr_reader() -> Optional[Any]:
    global _EASYOCR_READER
    if not HAVE_EASYOCR or easyocr is None:
        return None
    if _EASYOCR_READER is not None:
        return _EASYOCR_READER
    with _EASYOCR_LOCK:
        if _EASYOCR_READER is None:
            try:
                _EASYOCR_READER = easyocr.Reader(["en"], gpu=False, verbose=False)
            except Exception as exc:  # pragma: no cover - runtime dependent
                LOG.warning("EasyOCR initialization failed: %s", exc)
                return None
    return _EASYOCR_READER


def _map_has_text(ocr_map: Dict[str, str]) -> bool:
    for key in ("name", "oracle", "full", "full_text"):
        value = ocr_map.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _finalize_ocr_map(raw_map: Dict[str, str | None] | Dict[str, str]) -> Dict[str, str]:
    cleaned = text_clean.normalize_ocr_map(raw_map)
    # ensure rules alias
    if not cleaned.get("rules"):
        cleaned["rules"] = cleaned.get("oracle", "")

    # Build a single unified full_text by concatenating available regions in a sensible order.
    # Keep this non-destructive: preserve any existing 'full_text' if explicitly provided,
    # otherwise compose from available parts while avoiding duplication.
    if not cleaned.get("full_text"):
        parts = []
        # prefer any explicit 'full' block first if it contains substantial text
        for key in ("full", "name", "oracle", "rules", "collector"):
            val = cleaned.get(key)
            if isinstance(val, str):
                v = val.strip()
                if v:
                    parts.append(v)
        # deduplicate while preserving order
        seen = set()
        unique_parts = []
        for p in parts:
            if p in seen:
                continue
            seen.add(p)
            unique_parts.append(p)
        # join with space and collapse whitespace/newlines to keep a single-line
        joined = " ".join(unique_parts).strip()
        # collapse multiple whitespace/newlines into single space
        joined = re.sub(r"\s+", " ", joined)
        cleaned["full_text"] = joined or ""
    else:
        # normalize any existing full_text: collapse whitespace/newlines into single space
        existing = cleaned.get("full_text") or ""
        existing = re.sub(r"\s+", " ", str(existing)).strip()
        cleaned["full_text"] = existing
    return cleaned


def _perform_easyocr(ocr_image: np.ndarray, regions: Dict[str, Tuple[int, int]]) -> Tuple[Dict[str, str], Dict[str, Any]]:
    reader = _easyocr_reader()
    ocr_map = {key: "" for key in ("full", "name", "oracle", "set_symbol", "collector")}
    meta: Dict[str, Any] = {
        "engine": "easyocr",
        "regions": regions,
        "error": None,
        "duration_ms": 0,
        "detail": 0,
    }
    if reader is None:
        meta["error"] = "easyocr_not_available"
        return ocr_map, meta

    if ocr_image.ndim == 2:
        base = cv2.cvtColor(ocr_image, cv2.COLOR_GRAY2RGB)
    else:
        base = cv2.cvtColor(ocr_image, cv2.COLOR_BGR2RGB)

    start = time.perf_counter()
    try:
        for region, (row_start, row_end) in regions.items():
            if row_end <= row_start:
                continue
            slice_img = base[row_start:row_end, :]
            if slice_img.size == 0:
                continue
            try:
                text_segments = reader.readtext(slice_img, detail=0, paragraph=True)
            except Exception as region_exc:  # pragma: no cover - depends on runtime
                LOG.debug("EasyOCR region %s failed: %s", region, region_exc)
                continue
            if isinstance(text_segments, list):
                text = " ".join(seg.strip() for seg in text_segments if isinstance(seg, str) and seg.strip())
                if text.strip():
                    ocr_map[region] = text.strip()
    except Exception as exc:  # pragma: no cover - runtime dependent
        meta["error"] = str(exc)
        LOG.warning("EasyOCR processing failed: %s", exc)
    finally:
        meta["duration_ms"] = int(round((time.perf_counter() - start) * 1000))

    ocr_map["rules"] = ocr_map["oracle"]
    ocr_map["full_text"] = ocr_map["full"] or ocr_map["oracle"]
    return ocr_map, meta


def _perform_ocr(
    ocr_image: np.ndarray,
    *,
    region_hints: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    regions = _segment_ocr_regions(ocr_image, region_hints=region_hints)
    ocr_map = {key: "" for key in ("full", "name", "oracle", "set_symbol", "collector")}
    meta: Dict[str, Any] = {
        "engine": "tesseract" if HAVE_TESSERACT else "unavailable",
        "psm": 6,
        "regions": regions,
        "region_hints_used": bool(region_hints),
        "error": None,
    }
    if region_hints:
        meta["region_hints"] = {key: [int(start), int(end)] for key, (start, end) in regions.items() if key in region_hints}
    global _OCR_ENGINE_WARNED
    attempts: List[Dict[str, Any]] = []

    if HAVE_TESSERACT and pytesseract is not None:
        start = time.perf_counter()
        tesseract_meta: Dict[str, Any] = {
            "engine": "tesseract",
            "psm": meta["psm"],
        }
        try:
            for region, (row_start, row_end) in regions.items():
                if row_end <= row_start:
                    continue
                slice_img = ocr_image[row_start:row_end, :]
                if slice_img.size == 0:
                    continue
                prepared_slice = _prepare_region_slice(slice_img, region)
                cfg = _region_config(region)
                text = pytesseract.image_to_string(prepared_slice, config=cfg, lang="eng")
                ocr_map[region] = text.strip()
        except (TesseractError, RuntimeError) as exc:  # pragma: no cover - depends on system binary
            meta["error"] = str(exc)
            tesseract_meta["error"] = str(exc)
            LOG.warning("Tesseract OCR failed: %s", exc)
        finally:
            duration = int(round((time.perf_counter() - start) * 1000))
            meta["duration_ms"] = duration
            tesseract_meta["duration_ms"] = duration
            attempts.append(tesseract_meta)
    else:
        meta["error"] = "pytesseract_not_available"
        if not _OCR_ENGINE_WARNED:
            LOG.warning("pytesseract not available; falling back to EasyOCR if installed")
            _OCR_ENGINE_WARNED = True

    fallback_needed = not _map_has_text(ocr_map)
    fallback_meta: Optional[Dict[str, Any]] = None

    if fallback_needed and HAVE_EASYOCR:
        easy_map, easy_meta = _perform_easyocr(ocr_image, regions)
        fallback_meta = easy_meta
        attempts.append(easy_meta)
        if _map_has_text(easy_map):
            for key, value in easy_map.items():
                if isinstance(value, str) and value.strip():
                    ocr_map[key] = value
            meta["engine"] = "easyocr"
            meta["duration_ms"] = easy_meta.get("duration_ms", meta.get("duration_ms", 0))
            meta["error"] = easy_meta.get("error")
        elif not HAVE_TESSERACT:
            meta["engine"] = "easyocr"
            meta["error"] = easy_meta.get("error") or meta.get("error")
    elif fallback_needed and not HAVE_EASYOCR:
        fallback_meta = {"engine": "easyocr", "error": "easyocr_not_available"}
        attempts.append(fallback_meta)
        LOG.error("No OCR engines available; install tesseract or easyocr to enable text extraction")

    if attempts:
        meta["engine_attempts"] = attempts
    if fallback_meta:
        meta["fallback_engine"] = fallback_meta.get("engine")
    finalized_map = _finalize_ocr_map(ocr_map)
    return finalized_map, meta


def _encode_image(image: np.ndarray, ext: str, params: Optional[list] = None) -> bytes:
    ok, buf = cv2.imencode(ext, image, params or [])
    if not ok or buf is None:
        raise RuntimeError(f"Failed to encode image as {ext}")
    return bytes(buf)


def _decode_image_bytes(data: bytes, flag: int = cv2.IMREAD_UNCHANGED) -> np.ndarray:
    if not data:
        raise ValueError("Empty image payload")
    buf = np.frombuffer(data, dtype=np.uint8)
    if buf.size == 0:
        raise ValueError("Invalid image buffer")
    image = cv2.imdecode(buf, flag)
    if image is None:
        raise ValueError("Failed to decode image bytes")
    return image


def prepare_single_snapshot_artifacts(
    frame: np.ndarray,
    *,
    timestamp_slug: str,
    save_dir: Optional[Path] = None,
    jpeg_quality: int = 90,
    persist: bool = True,
    include_bytes: bool = True,
    min_identify_score: float = 70.0,
) -> Dict[str, Any]:
    """Process a single snapshot frame for OCR - simply rotates 90° CCW."""
    save_dir = Path(save_dir) if save_dir is not None else Path("data") / "snapshots"
    jpeg_quality = int(max(10, min(jpeg_quality, 100)))

    # Crop 200 pixels from the left edge
    crop_left_original = 200
    if frame.shape[1] > crop_left_original:
        frame = frame[:, crop_left_original:]
    
    # Create dummy mask for single frame processing
    dummy_mask = np.ones((frame.shape[0], frame.shape[1]), dtype=np.uint8) * 255
    
    # Warp and align the card
    card_aligned, card_mask, border_meta = _warp_card_to_bounds(frame, dummy_mask)
    
    # Simply rotate 90° counterclockwise - no orientation detection
    card_portrait = cv2.rotate(card_aligned, cv2.ROTATE_90_COUNTERCLOCKWISE)
    
    h, w = card_portrait.shape[:2]
    name_zone_height = int(round(min(180, max(90, h * 0.14))))
    collector_zone_height = int(round(min(120, max(55, h * 0.09))))

    def _evaluate_orientation(card_img: np.ndarray) -> Dict[str, Any]:
        local_h, local_w = card_img.shape[:2]
        local_name_h = int(round(min(180, max(90, local_h * 0.14))))
        local_col_h = int(round(min(120, max(55, local_h * 0.09))))

        local_name_zone = card_img[0:local_name_h, :].copy()
        local_collector_zone = card_img[-local_col_h:, :].copy()
        local_zone = _extract_zone_text(local_name_zone, local_collector_zone)

        local_hints = {
            "name": (0, local_name_h),
            "collector": (max(0, local_h - local_col_h), local_h),
        }
        local_region_map, local_region_meta = _perform_ocr(card_img, region_hints=local_hints)

        zone_name = (local_zone.get("name") or "").strip()
        region_name = (local_region_map.get("name") or "").strip()
        zone_name_score = _score_name_text(zone_name)
        region_name_score = _score_name_text(region_name)
        raw_name = zone_name if zone_name_score >= region_name_score else region_name
        chosen_name = raw_name if _score_name_text(raw_name) >= 35.0 else ""

        zone_collector = (local_zone.get("collector") or "").strip()
        region_collector = (local_region_map.get("collector") or "").strip()
        zone_col_score = _score_collector_text(zone_collector)
        region_col_score = _score_collector_text(region_collector)
        raw_collector = zone_collector if zone_col_score >= region_col_score else region_collector
        chosen_collector = raw_collector if _score_collector_text(raw_collector) >= 40.0 else ""

        chosen_oracle = (local_region_map.get("oracle") or local_region_map.get("rules") or "").strip()
        chosen_full = (local_region_map.get("full_text") or local_region_map.get("full") or "").strip()
        chosen_set = (local_region_map.get("set_symbol") or local_region_map.get("set") or "").strip()
        chosen_type = (local_region_map.get("type_line") or "").strip()

        quality_score = _score_name_text(chosen_name) + _score_collector_text(chosen_collector)
        if len(chosen_oracle) >= 24:
            quality_score += 10.0

        return {
            "card": card_img,
            "name_zone_height": local_name_h,
            "collector_zone_height": local_col_h,
            "zone": local_zone,
            "region": local_region_map,
            "region_meta": local_region_meta,
            "name": chosen_name,
            "collector": chosen_collector,
            "oracle": chosen_oracle,
            "full": chosen_full,
            "set": chosen_set,
            "type_line": chosen_type,
            "quality": float(quality_score),
        }

    base_eval = _evaluate_orientation(card_portrait)
    rotated_eval = _evaluate_orientation(cv2.rotate(card_portrait, cv2.ROTATE_180))

    if rotated_eval["quality"] > base_eval["quality"] + 8.0:
        selected_eval = rotated_eval
        selected_orientation = "rotated_180"
        rotation_method = "fixed_90_ccw_plus_180"
        rotation_degrees = 270
    else:
        selected_eval = base_eval
        selected_orientation = "base"
        rotation_method = "fixed_90_ccw"
        rotation_degrees = 90

    card_portrait = selected_eval["card"]
    name_zone_height = int(selected_eval["name_zone_height"])
    collector_zone_height = int(selected_eval["collector_zone_height"])
    ocr_result = selected_eval["zone"]
    region_ocr_map = selected_eval["region"]
    region_ocr_meta = selected_eval["region_meta"]
    
    # Save the actual card with zone overlays for debugging
    debug_dir = Path("data/snapshots")
    if debug_dir.exists():
        try:
            # Create a copy with zone rectangles drawn
            card_with_zones = card_portrait.copy()
            if len(card_with_zones.shape) == 2:
                card_with_zones = cv2.cvtColor(card_with_zones, cv2.COLOR_GRAY2BGR)
            cv2.rectangle(card_with_zones, (0, 0), (w-1, name_zone_height-1), (0, 255, 0), 3)
            cv2.rectangle(card_with_zones, (0, h-collector_zone_height), (w-1, h-1), (255, 0, 0), 3)
            cv2.putText(card_with_zones, "NAME ZONE", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
            cv2.putText(card_with_zones, "COLLECTOR ZONE", (10, h-collector_zone_height+30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
            cv2.imwrite(str(debug_dir / "debug_card_with_zones.jpg"), card_with_zones)
            
            # Also save the raw extracted zones
            name_zone = card_portrait[0:name_zone_height, :].copy()
            collector_zone = card_portrait[-collector_zone_height:, :].copy()
            cv2.imwrite(str(debug_dir / "debug_name_zone_raw.jpg"), name_zone)
            cv2.imwrite(str(debug_dir / "debug_collector_zone_raw.jpg"), collector_zone)
            
            LOG.info(f"Card dimensions: {w}x{h}, Name zone: 0-{name_zone_height}, Collector zone: {h-collector_zone_height}-{h}")
        except Exception as e:
            LOG.warning(f"Failed to save debug zones: {e}")
    
    # Build enriched OCR text map for identification
    ocr_name_raw = (selected_eval.get("name") or "").strip()
    ocr_collector_raw = (selected_eval.get("collector") or "").strip()
    ocr_oracle_raw = (region_ocr_map.get("oracle") or region_ocr_map.get("rules") or "").strip()
    ocr_full_raw = (region_ocr_map.get("full_text") or region_ocr_map.get("full") or "").strip()
    ocr_set_raw = (region_ocr_map.get("set_symbol") or region_ocr_map.get("set") or "").strip()

    # Log OCR results for debugging
    LOG.info(
        "OCR Result - Name: '%s', Collector: '%s', OracleLen: %d",
        ocr_name_raw,
        ocr_collector_raw,
        len(ocr_oracle_raw),
    )
    
    # Identify card from OCR result
    db_path = _find_card_database()
    
    identification = None
    identification_rejected = None
    identification_status = "unavailable"
    identification_reason = None
    
    if db_path:
        # Clean and identify using richer OCR fields
        name_clean = text_clean.normalize_card_name(ocr_name_raw)
        LOG.info("OCR - Normalized name: '%s' (length: %d)", name_clean, len(name_clean))
        id_input = {
            "name": name_clean,
            "collector": ocr_collector_raw,
            "oracle": ocr_oracle_raw,
            "rules": ocr_oracle_raw,
            "full": ocr_full_raw,
            "set": ocr_set_raw,
            "set_symbol": ocr_set_raw,
            "type_line": (region_ocr_map.get("type_line") or "").strip(),
        }

        if len(name_clean) >= 2 or len(ocr_oracle_raw) >= 12:
            raw_identification = card_id.identify_card_from_ocr(
                id_input,
                db_path=str(db_path),
                top_n=8
            )
            raw_score = float(raw_identification.get("score", 0.0)) if isinstance(raw_identification, dict) else 0.0
            if isinstance(raw_identification, dict) and raw_score >= float(min_identify_score):
                identification = raw_identification
                identification_status = "accepted"
            else:
                identification_rejected = raw_identification
                identification_status = "rejected"
                identification_reason = f"score_below_threshold:{raw_score:.1f}<{float(min_identify_score):.1f}"
                LOG.warning(
                    "OCR - Rejected low-confidence identification (score %.1f < %.1f)",
                    raw_score,
                    float(min_identify_score),
                )

            if raw_identification:
                LOG.info("OCR - Best match: '%s' (score: %.1f)", 
                        raw_identification.get('best', {}).get('name', 'unknown'),
                        raw_identification.get('score', 0.0))
        else:
            LOG.warning("OCR - Name too short after normalization, skipping identification")
            identification_status = "rejected"
            identification_reason = "insufficient_ocr_text"
    
    # Create visualization with zone highlights
    card_vis = _create_zone_visualization(card_portrait, name_zone_height, collector_zone_height)
    
    # Build OCR text map
    ocr_text_map = {
        "name": ocr_name_raw,
        "collector": ocr_collector_raw,
        "oracle": ocr_oracle_raw,
        "rules": ocr_oracle_raw,
        "full": (region_ocr_map.get("full") or "").strip(),
        "full_text": ocr_full_raw,
        "set": ocr_set_raw,
        "set_symbol": ocr_set_raw,
        "type_line": (region_ocr_map.get("type_line") or "").strip(),
    }
    
    # Build metadata (simplified - no orientation comparison)
    meta = {
        "timestamp": timestamp_slug,
        "border": border_meta,
        "rotation": {
            "method": rotation_method,
            "degrees": rotation_degrees,
        },
        "zone_ocr": ocr_text_map,
        "ocr_engine": region_ocr_meta,
        "selected_orientation": selected_orientation,
        "orientation_quality": {
            "base": base_eval["quality"],
            "rotated_180": rotated_eval["quality"],
        },
        "identification_status": identification_status,
        "identification_reason": identification_reason,
        "min_identify_score": float(min_identify_score),
        "identification": identification,
        "identification_rejected": identification_rejected,
    }
    
    # Encode images
    jpeg_params = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
    png_params = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    
    assets: Dict[str, Any] = {}
    
    # Encode card visualization
    if persist and save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        
        # Save card visualization
        card_jpeg = _encode_image(card_vis, ".jpg", jpeg_params)
        card_path = save_dir / f"{timestamp_slug}_card.jpg"
        card_path.write_bytes(card_jpeg)
        
        assets["ocr_text"] = {
            "path": str(card_path),
            "mime": "image/jpeg",
            "shape": card_vis.shape[:2],
            "text": ocr_text_map,
        }
        
        if include_bytes:
            assets["ocr_text"]["bytes"] = card_jpeg
        
        # Save original frame
        orig_jpeg = _encode_image(frame, ".jpg", jpeg_params)
        orig_path = save_dir / f"{timestamp_slug}_original.jpg"
        orig_path.write_bytes(orig_jpeg)
        
        assets["original"] = {
            "path": str(orig_path),
            "mime": "image/jpeg",
            "shape": frame.shape[:2],
        }
        
        if include_bytes:
            assets["original"]["bytes"] = orig_jpeg
        
        # Save metadata
        meta_path = save_dir / f"{timestamp_slug}_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf8")
        
        assets["meta"] = {
            "path": str(meta_path),
            **meta,  # Unpack meta directly into this dict
        }
    else:
        # Just include bytes without persisting
        card_jpeg = _encode_image(card_vis, ".jpg", jpeg_params)
        orig_jpeg = _encode_image(frame, ".jpg", jpeg_params)
        
        assets["ocr_text"] = {
            "mime": "image/jpeg",
            "shape": card_vis.shape[:2],
            "bytes": card_jpeg if include_bytes else None,
            "text": ocr_text_map,
        }
        
        assets["original"] = {
            "mime": "image/jpeg",
            "shape": frame.shape[:2],
            "bytes": orig_jpeg if include_bytes else None,
        }
        
        assets["meta"] = meta  # Include meta directly
    
    return assets

def _extract_zone_text(name_zone: np.ndarray, collector_zone: np.ndarray) -> Dict[str, str]:
    """Extract text from name and collector zones using OCR."""
    result = {"name": "", "collector": ""}
    
    if not HAVE_TESSERACT or pytesseract is None:
        return result
    
    try:
        # Prepare and OCR name zone with optimized config
        name_prepared = _prepare_region_slice(name_zone, "name")
        
        # Save debug image to see what OCR is reading
        debug_dir = Path("data/snapshots")
        if debug_dir.exists():
            try:
                cv2.imwrite(str(debug_dir / "debug_name_prepared.jpg"), name_prepared)
            except Exception:
                pass
        
        name_text = ""
        name_candidates: List[str] = []
        configs = [
            ("psm7", "--oem 1 --psm 7 -c preserve_interword_spaces=1"),
            ("psm6", "--oem 1 --psm 6 -c preserve_interword_spaces=1"),
            ("psm13", "--oem 1 --psm 13"),
        ]

        variants: List[Tuple[str, np.ndarray]] = [("base", name_prepared)]
        try:
            name_inverted = cv2.bitwise_not(name_prepared)
            variants.append(("inv", name_inverted))
            if debug_dir.exists():
                cv2.imwrite(str(debug_dir / "debug_name_inverted.jpg"), name_inverted)
        except Exception:
            pass
        try:
            _, name_binary = cv2.threshold(name_prepared, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            variants.append(("otsu", name_binary))
            if debug_dir.exists():
                cv2.imwrite(str(debug_dir / "debug_name_otsu.jpg"), name_binary)
        except Exception:
            pass

        for variant_name, variant_img in variants:
            for psm_name, config in configs:
                try:
                    text = pytesseract.image_to_string(variant_img, config=config, lang="eng").strip()
                except Exception:
                    continue
                if not text:
                    continue
                LOG.info("OCR Strategy %s_%s: '%s' (len=%d)", variant_name, psm_name, text, len(text))
                name_candidates.append(text)

        if name_candidates:
            best_name = max(name_candidates, key=_score_name_text)
            best_name_score = _score_name_text(best_name)
            if best_name_score >= 35.0:
                name_text = text_clean.normalize_card_name(best_name)
            else:
                LOG.warning("✗ Name OCR candidates too weak (best_score=%.1f)", best_name_score)
        else:
            LOG.warning("✗ No OCR strategies returned any name text")
        
        LOG.info(f"Final name OCR result: '{name_text}' (len={len(name_text)})")
        result["name"] = name_text
        
        # Prepare and OCR collector zone - try multiple PSM modes
        collector_prepared = _prepare_region_slice(collector_zone, "collector")
        
        # Save debug image
        if debug_dir.exists():
            try:
                cv2.imwrite(str(debug_dir / "debug_collector_prepared.jpg"), collector_prepared)
            except Exception:
                pass
        
        # Try multiple PSM modes for collector number
        collector_results: List[str] = []
        collector_configs = [
            ('psm7', f'--oem 1 --psm 7 -c tessedit_char_whitelist={COLLECTOR_CHAR_WHITELIST}'),
            ('psm6', f'--oem 1 --psm 6 -c tessedit_char_whitelist={COLLECTOR_CHAR_WHITELIST}'),
            ('psm13', f'--oem 1 --psm 13 -c tessedit_char_whitelist={COLLECTOR_CHAR_WHITELIST}'),
        ]

        collector_variants: List[Tuple[str, np.ndarray]] = [("base", collector_prepared)]
        try:
            collector_inverted = cv2.bitwise_not(collector_prepared)
            collector_variants.append(("inv", collector_inverted))
        except Exception:
            pass
        try:
            _, collector_binary = cv2.threshold(collector_prepared, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            collector_variants.append(("otsu", collector_binary))
        except Exception:
            pass

        for variant_name, variant_img in collector_variants:
            for psm_name, config in collector_configs:
                try:
                    text = pytesseract.image_to_string(variant_img, config=config, lang="eng").strip()
                except Exception:
                    continue
                if text:
                    LOG.info("Collector OCR %s_%s: '%s' (len=%d)", variant_name, psm_name, text, len(text))
                    collector_results.append(text)
        
        # Choose the most collector-like OCR candidate
        if collector_results:
            best_collector = max(collector_results, key=_score_collector_text)
            best_collector_score = _score_collector_text(best_collector)
            if best_collector_score >= 40.0:
                result["collector"] = text_clean.normalize_collector(best_collector)
            else:
                result["collector"] = ""
                LOG.warning("✗ Collector OCR candidates too weak (best_score=%.1f)", best_collector_score)
            LOG.info(f"Final collector OCR: '{result['collector']}'")
        else:
            result["collector"] = ""
            LOG.warning("✗ No collector OCR results")
    except Exception as exc:
        LOG.warning("Zone OCR failed: %s", exc)
    
    return result


def _score_name_text(text: str) -> float:
    cleaned = text_clean.normalize_card_name(text)
    if not cleaned:
        return 0.0
    if len(cleaned) < 3:
        return 0.0

    letters = sum(1 for ch in cleaned if ch.isalpha())
    total = max(1, len(cleaned))
    alpha_ratio = letters / total
    words = [w for w in cleaned.split() if w]
    unique_chars = len(set(cleaned.lower().replace(" ", "")))

    score = 0.0
    score += min(28.0, len(cleaned) * 2.0)
    score += alpha_ratio * 45.0
    score += min(16.0, len(words) * 4.0)
    if cleaned[:1].isupper():
        score += 6.0
    if unique_chars <= 2 and len(cleaned) <= 5:
        score -= 25.0
    if cleaned.lower() in {"ee", "re", "rr", "ii", "oo"}:
        score -= 40.0
    return max(0.0, score)


def _score_collector_text(text: str) -> float:
    raw = (text or "").strip()
    if not raw:
        return 0.0
    norm = text_clean.normalize_collector(raw)
    if not norm:
        return 0.0

    score = 30.0
    if re.fullmatch(r"\d+[a-z]?(?:/\d+[a-z]?)?", norm):
        score += 45.0
    if "/" in norm:
        score += 8.0
    if len(norm) >= 2:
        score += min(12.0, len(norm) * 1.8)
    if len(set(norm)) <= 2:
        score -= 12.0
    return max(0.0, score)

def _find_card_database() -> Optional[Path]:
    """Find the card database JSON file."""
    # First check for cards_metadata.json in data/embeddings
    cards_metadata = Path("data/embeddings/cards_metadata.json")
    if cards_metadata.exists():
        return cards_metadata
    
    # Fall back to oracle-cards pattern
    data_dir = Path("data")
    for json_file in data_dir.glob("oracle-cards-*.json"):
        return json_file
    return None

def _create_zone_visualization(image: np.ndarray, name_height: int, collector_height: int) -> np.ndarray:
    """Create visualization with zone highlights."""
    h, w = image.shape[:2]
    vis = image.copy()
    
    if len(vis.shape) == 2:
        vis = cv2.cvtColor(vis, cv2.COLOR_GRAY2BGR)
    
    # Draw zone rectangles
    cv2.rectangle(vis, (0, 0), (w-1, name_height-1), (0, 255, 0), 2)
    cv2.rectangle(vis, (0, h-collector_height), (w-1, h-1), (0, 255, 0), 2)
    
    # Add labels
    cv2.putText(vis, "NAME", (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
    cv2.putText(vis, "COLLECTOR", (5, h-collector_height+15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
    
    return vis
