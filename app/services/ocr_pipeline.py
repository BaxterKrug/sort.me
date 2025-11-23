"""Dual-photo OCR helpers for improved card orientation detection."""
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

LOG = logging.getLogger("sort.ocr_pipeline")
CARD_BORDER_MARGIN_PX = 20
_TESSERACT_PATH = shutil.which("tesseract")
HAVE_TESSERACT = bool(pytesseract and _TESSERACT_PATH)
HAVE_EASYOCR = bool(easyocr)
_OCR_ENGINE_WARNED = False
_EASYOCR_READER = None
_EASYOCR_LOCK = threading.Lock()


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

    composite = np.vstack([top_crop, blend, bottom_crop])

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

    dest_width = max(width, 1)
    dest_height = max(height, 1)
    if dest_width > dest_height:
        dest_width, dest_height = dest_height, dest_width

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
    if warped.shape[1] > warped.shape[0]:
        warped = cv2.rotate(warped, cv2.ROTATE_90_COUNTERCLOCKWISE)
        warped_mask = cv2.rotate(warped_mask, cv2.ROTATE_90_COUNTERCLOCKWISE)
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

    candidates = []
    rotations = {
        "clockwise": cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE),
        "counterclockwise": cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE),
    }
    mask_rotations = {
        "clockwise": cv2.rotate(source_mask, cv2.ROTATE_90_CLOCKWISE),
        "counterclockwise": cv2.rotate(source_mask, cv2.ROTATE_90_COUNTERCLOCKWISE),
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
    }
    return cleaned, meta


def _segment_ocr_regions(image: np.ndarray) -> Dict[str, Tuple[int, int]]:
    height = image.shape[0]
    if height <= 0:
        return {"full": (0, 0)}
    name_end = int(height * 0.20)
    collector_start = int(height * 0.78)
    return {
        "full": (0, height),
        "name": (0, max(name_end, 1)),
        "oracle": (max(name_end - 5, 0), max(collector_start, name_end + 1)),
        "collector": (max(collector_start - 5, 0), height),
    }


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


def _perform_easyocr(ocr_image: np.ndarray, regions: Dict[str, Tuple[int, int]]) -> Tuple[Dict[str, str], Dict[str, Any]]:
    reader = _easyocr_reader()
    ocr_map = {key: "" for key in ("full", "name", "oracle", "collector")}
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


def _perform_ocr(ocr_image: np.ndarray) -> Tuple[Dict[str, str], Dict[str, Any]]:
    regions = _segment_ocr_regions(ocr_image)
    ocr_map = {key: "" for key in ("full", "name", "oracle", "collector")}
    meta: Dict[str, Any] = {
        "engine": "tesseract" if HAVE_TESSERACT else "unavailable",
        "psm": 6,
        "regions": regions,
        "error": None,
    }
    global _OCR_ENGINE_WARNED
    attempts: List[Dict[str, Any]] = []

    if HAVE_TESSERACT and pytesseract is not None:
        config_common = "--oem 3 --psm 6 -c preserve_interword_spaces=1"
        config_name = "--oem 3 --psm 7 -c preserve_interword_spaces=1"
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
                cfg = config_name if region == "name" else config_common
                text = pytesseract.image_to_string(slice_img, config=cfg, lang="eng")
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

    ocr_map["rules"] = ocr_map["oracle"]
    ocr_map["full_text"] = ocr_map["full"] or ocr_map["oracle"]

    if attempts:
        meta["engine_attempts"] = attempts
    if fallback_meta:
        meta["fallback_engine"] = fallback_meta.get("engine")
    return ocr_map, meta


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


def prepare_snapshot_artifacts(
    frame_top: np.ndarray,
    frame_bottom: np.ndarray,
    *,
    timestamp_slug: str,
    save_dir: Optional[Path] = None,
    jpeg_quality: int = 90,
    persist: bool = True,
    include_bytes: bool = True,
    orientation_hint: Optional[Dict[str, Any]] = None,
    embeddings_dir: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Build composite + OCR-ready images, optionally persisting them to disk."""
    save_dir = Path(save_dir) if save_dir is not None else Path("data") / "snapshots"
    jpeg_quality = int(max(10, min(jpeg_quality, 100)))
    if embeddings_dir is None:
        embeddings_dir_path: Optional[Path] = Path("data") / "embeddings"
    elif isinstance(embeddings_dir, Path):
        embeddings_dir_path = embeddings_dir
    else:
        embeddings_dir_path = Path(embeddings_dir)

    top_aligned, bottom_aligned = _normalize_frames(frame_top, frame_bottom)
    composite_raw, source_mask, composite_meta = _composite_frames(top_aligned, bottom_aligned)
    card_aligned, card_mask, border_meta = _warp_card_to_bounds(composite_raw, source_mask)
    pair_orientation = orientation_hint or analyze_orientation(frame_top, frame_bottom)
    composite_oriented, orientation_meta = _orient_composite(card_aligned, card_mask, pair_orientation)
    aligned_meta = {
        **composite_meta,
        "width": int(card_aligned.shape[1]),
        "height": int(card_aligned.shape[0]),
        "border": border_meta,
    }
    rotated_meta = {
        **aligned_meta,
        "width": int(composite_oriented.shape[1]),
        "height": int(composite_oriented.shape[0]),
        "orientation": orientation_meta,
    }
    ocr_ready, ocr_meta = _prepare_for_ocr(composite_oriented)
    ocr_meta.update(
        {
            "source": "composite_rotated",
            "source_shape": list(composite_oriented.shape),
            "rotation_direction": orientation_meta.get("rotation_direction"),
        }
    )
    ocr_text_map, ocr_text_meta = _perform_ocr(ocr_ready)
    embedding_dir_str = str(embeddings_dir_path) if embeddings_dir_path else None
    embedding_info = card_id.embedding_matches_from_ocr(ocr_text_map, embedding_dir_str)
    ocr_text_meta["embedding"] = embedding_info

    top_bytes = _encode_image(top_aligned, ".jpg", [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
    bottom_bytes = _encode_image(bottom_aligned, ".jpg", [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
    composite_raw_bytes = _encode_image(
        composite_raw,
        ".jpg",
        [int(cv2.IMWRITE_JPEG_QUALITY), max(jpeg_quality - 10, 70)],
    )
    composite_aligned_bytes = _encode_image(
        card_aligned,
        ".jpg",
        [int(cv2.IMWRITE_JPEG_QUALITY), max(jpeg_quality - 5, 80)],
    )
    composite_rotated_bytes = _encode_image(
        composite_oriented,
        ".jpg",
        [int(cv2.IMWRITE_JPEG_QUALITY), max(jpeg_quality, 92)],
    )
    ocr_bytes = _encode_image(ocr_ready, ".png")

    base_name = f"snapshot-{timestamp_slug}"
    top_path = bottom_path = composite_path = composite_aligned_path = composite_raw_path = ocr_path = meta_path = None
    if persist:
        save_dir.mkdir(parents=True, exist_ok=True)
        top_path = save_dir / f"{base_name}-top.jpg"
        bottom_path = save_dir / f"{base_name}-bottom.jpg"
        composite_path = save_dir / f"{base_name}-composite.jpg"
        composite_aligned_path = save_dir / f"{base_name}-composite-aligned.jpg"
        composite_raw_path = save_dir / f"{base_name}-composite-raw.jpg"
        ocr_path = save_dir / f"{base_name}-ocr.png"
        top_path.write_bytes(top_bytes)
        bottom_path.write_bytes(bottom_bytes)
        composite_path.write_bytes(composite_rotated_bytes)
        composite_aligned_path.write_bytes(composite_aligned_bytes)
        composite_raw_path.write_bytes(composite_raw_bytes)
        ocr_path.write_bytes(ocr_bytes)

        meta_payload = {
            "timestamp": timestamp_slug,
            "paths": {
                "top": str(top_path),
                "bottom": str(bottom_path),
                "composite": str(composite_path),
                "composite_aligned": str(composite_aligned_path),
                "composite_raw": str(composite_raw_path),
                "ocr_prepared": str(ocr_path),
            },
            "ocr_map": ocr_text_map,
            "ocr_result": ocr_text_meta,
            "ocr_meta": ocr_meta,
            "pair_orientation": pair_orientation,
            "composite_meta": {
                "raw": composite_meta,
                "aligned": aligned_meta,
                "rotated": rotated_meta,
            },
            "embedding": embedding_info,
        }
        meta_path = save_dir / f"{base_name}-meta.json"
        meta_path.write_text(json.dumps(meta_payload, indent=2, sort_keys=True), encoding="utf8")

    assets: Dict[str, Any] = {
        "top": {
            "mime": "image/jpeg",
            "path": str(top_path) if top_path else "",
            "shape": list(top_aligned.shape),
        },
        "bottom": {
            "mime": "image/jpeg",
            "path": str(bottom_path) if bottom_path else "",
            "shape": list(bottom_aligned.shape),
        },
        "composite_raw": {
            "mime": "image/jpeg",
            "path": str(composite_raw_path) if composite_raw_path else "",
            "shape": list(composite_raw.shape),
            "meta": composite_meta,
        },
        "composite_aligned": {
            "mime": "image/jpeg",
            "path": str(composite_aligned_path) if composite_aligned_path else "",
            "shape": list(card_aligned.shape),
            "meta": aligned_meta,
        },
        "composite_rotated": {
            "mime": "image/jpeg",
            "path": str(composite_path) if composite_path else "",
            "shape": list(composite_oriented.shape),
            "meta": rotated_meta,
        },
        "composite": {
            "mime": "image/jpeg",
            "path": str(composite_path) if composite_path else "",
            "shape": list(composite_oriented.shape),
            "meta": rotated_meta,
        },
        "ocr_prepared": {
            "mime": "image/png",
            "path": str(ocr_path) if ocr_path else "",
            "shape": list(ocr_ready.shape),
            "meta": ocr_meta,
        },
        "meta": {
            "timestamp_slug": timestamp_slug,
            "orientation": orientation_meta,
            "composite": rotated_meta,
            "composite_raw": composite_meta,
            "composite_aligned": aligned_meta,
            "composite_rotated": rotated_meta,
            "ocr": ocr_meta,
            "ocr_map": ocr_text_map,
            "ocr_result": ocr_text_meta,
            "pair_orientation": pair_orientation,
            "path": str(meta_path) if meta_path else "",
            "embedding": embedding_info,
        },
    }

    if include_bytes:
        assets["top"]["bytes"] = top_bytes
        assets["bottom"]["bytes"] = bottom_bytes
        assets["composite_raw"]["bytes"] = composite_raw_bytes
        assets["composite_aligned"]["bytes"] = composite_aligned_bytes
        assets["composite_rotated"]["bytes"] = composite_rotated_bytes
        assets["composite"]["bytes"] = composite_rotated_bytes
        assets["ocr_prepared"]["bytes"] = ocr_bytes

    assets["ocr_text"] = {
        "mime": "text/plain",
        "path": "",
        "meta": ocr_text_meta,
        "text": ocr_text_map,
    }

    return assets


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
    artifact_task = functools.partial(
        prepare_snapshot_artifacts,
        frame_top,
        frame_bottom,
        timestamp_slug=timestamp,
        save_dir=Path(save_dir),
        jpeg_quality=92,
        persist=True,
        include_bytes=False,
    )
    try:
        artifacts = await loop.run_in_executor(None, artifact_task)
    except Exception as exc:  # pragma: no cover - diagnostic only
        LOG.warning("Failed to persist OCR snapshot artifacts: %s", exc)
        artifacts = prepare_snapshot_artifacts(
            frame_top,
            frame_bottom,
            timestamp_slug=timestamp,
            save_dir=Path(save_dir),
            jpeg_quality=92,
            persist=False,
            include_bytes=False,
        )

    return {
        "success": True,
        "captured_at": timestamp,
        "offset_mm": offset_mm,
        "orientation": orientation,
        "artifacts": {
            "paths": {
                "top": artifacts.get("top", {}).get("path", ""),
                "bottom": artifacts.get("bottom", {}).get("path", ""),
                "composite": artifacts.get("composite", {}).get("path", ""),
                "ocr_prepared": artifacts.get("ocr_prepared", {}).get("path", ""),
            },
            "meta": artifacts.get("meta", {}),
        },
        "capture": {
            "top_shape": list(frame_top.shape),
            "bottom_shape": list(frame_bottom.shape),
        },
    }


def run_ocr_from_image(image: np.ndarray) -> Tuple[Dict[str, str], Dict[str, Any]]:
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    return _perform_ocr(gray)


def run_ocr_from_bytes(data: bytes) -> Dict[str, Any]:
    image = _decode_image_bytes(data, cv2.IMREAD_GRAYSCALE)
    ocr_map, ocr_meta = run_ocr_from_image(image)
    ocr_meta = dict(ocr_meta)
    ocr_meta["input_shape"] = list(image.shape)
    return {
        "ocr_map": ocr_map,
        "ocr_meta": ocr_meta,
    }


def run_ocr_from_path(path: Path | str) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"Image not found: {file_path}")
    data = file_path.read_bytes()
    return run_ocr_from_bytes(data)
