"""Dual-photo OCR helpers for improved card orientation detection."""
from __future__ import annotations

import asyncio
import functools
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple, Optional, List

import cv2  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]

try:  # pragma: no cover - optional dependency
    import easyocr  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    easyocr = None  # type: ignore[misc]

try:  # pragma: no cover - optional dependency
    import pytesseract  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None  # type: ignore[misc]

from . import camera as camera_svc
from . import motion

LOG = logging.getLogger("sort.ocr_pipeline")

_EASYOCR_READER: Optional[Any] = None


def _clean_text(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _get_easyocr_reader() -> Optional[Any]:
    global _EASYOCR_READER
    if easyocr is None:
        return None
    if _EASYOCR_READER is None:
        try:
            _EASYOCR_READER = easyocr.Reader(["en"], gpu=False)
        except Exception as exc:  # pragma: no cover - optional dependency init
            LOG.warning("EasyOCR initialization failed: %s", exc)
            _EASYOCR_READER = None
    return _EASYOCR_READER


def _read_with_easyocr(image: np.ndarray) -> List[str]:
    reader = _get_easyocr_reader()
    if reader is None or image.size == 0:
        return []
    try:
        lines = reader.readtext(image, detail=0, paragraph=True)
        cleaned = [_clean_text(line) for line in lines if _clean_text(line)]
        return cleaned
    except Exception as exc:  # pragma: no cover - OCR engine variability
        LOG.debug("EasyOCR failed: %s", exc)
        return []


def _read_with_tesseract(image: np.ndarray, *, psm: int) -> List[str]:
    if pytesseract is None or image.size == 0:
        return []
    try:
        img = image
        if img.dtype != np.uint8:
            img = cv2.convertScaleAbs(img)
        config = f"--psm {psm} --oem 3 -c preserve_interword_spaces=1"
        text = pytesseract.image_to_string(img, config=config, lang="eng")
    except pytesseract.TesseractNotFoundError as exc:  # type: ignore[attr-defined]
        LOG.warning("Tesseract binary not found: %s", exc)
        return []
    except Exception as exc:  # pragma: no cover - OCR engine variability
        LOG.debug("pytesseract failed: %s", exc)
        return []
    cleaned_lines = [_clean_text(line) for line in text.splitlines()]
    return [line for line in cleaned_lines if line]


def _choose_text(candidates: Dict[str, List[str]]) -> Tuple[str, List[str], Optional[str]]:
    ranked = [(engine, " ".join(lines)) for engine, lines in candidates.items() if lines]
    if not ranked:
        return "", [], None
    ranked.sort(key=lambda item: len(item[1]), reverse=True)
    engine, text = ranked[0]
    lines = candidates[engine]
    return _clean_text(text), lines, engine


def _read_region_text(
    region_color: np.ndarray,
    region_gray: np.ndarray,
    *,
    psm: int,
    fallback_psm: Optional[int] = None,
) -> Dict[str, Any]:
    candidates: Dict[str, List[str]] = {}

    easy_lines = _read_with_easyocr(region_color)
    if easy_lines:
        candidates["easyocr"] = easy_lines

    target_gray = region_gray if region_gray.size else cv2.cvtColor(region_color, cv2.COLOR_BGR2GRAY)
    tess_lines = _read_with_tesseract(target_gray, psm=psm)
    if tess_lines:
        candidates[f"pytesseract_psm{psm}"] = tess_lines

    if not candidates and fallback_psm is not None:
        alt_lines = _read_with_tesseract(target_gray, psm=fallback_psm)
        if alt_lines:
            candidates[f"pytesseract_psm{fallback_psm}"] = alt_lines

    text, lines, engine_variant = _choose_text(candidates)
    if engine_variant is None:
        engine_name: Optional[str] = None
    elif engine_variant.startswith("easyocr"):
        engine_name = "easyocr"
    else:
        engine_name = "pytesseract"

    return {
        "text": text,
        "lines": lines,
        "engine": engine_name,
        "engine_variant": engine_variant,
        "candidates": candidates,
    }


def _extract_ocr_fields(composite_color: np.ndarray, ocr_ready: np.ndarray) -> Dict[str, Any]:
    if composite_color.size == 0:
        return {
            "fields": {},
            "regions": {},
            "engines": {},
            "raw": {},
        }

    height, width = composite_color.shape[:2]
    ocr_gray = ocr_ready if ocr_ready.size else cv2.cvtColor(composite_color, cv2.COLOR_BGR2GRAY)

    def clamp_range(start: float, end: float) -> Tuple[int, int]:
        top = max(0, min(height, int(round(start))))
        bottom = max(top, min(height, int(round(end))))
        return top, bottom

    region_specs = {
        "name": {"range": (0.0, height * 0.16), "psm": 7, "fallback": 8},
        "type_line": {"range": (height * 0.16, height * 0.24), "psm": 7, "fallback": 6},
        "oracle": {"range": (height * 0.24, height * 0.82), "psm": 6, "fallback": 4},
        "collector": {"range": (height * 0.82, height * 0.97), "psm": 7, "fallback": 8},
    }

    results: Dict[str, Dict[str, Any]] = {}
    for key, spec in region_specs.items():
        top, bottom = clamp_range(*spec["range"])
        if bottom - top < 12:
            bottom = min(height, top + 12)
        region_color = composite_color[top:bottom, :]
        region_gray = ocr_gray[top:bottom, :]
        if region_color.size == 0 or region_gray.size == 0:
            results[key] = {
                "text": "",
                "lines": [],
                "engine": None,
                "engine_variant": None,
                "candidates": {},
                "bbox": {"top": int(top), "bottom": int(bottom), "width": int(width), "height": int(bottom - top)},
            }
            continue
        fallback_val = spec.get("fallback")
        fallback_psm = int(fallback_val) if fallback_val is not None else None
        outcome = _read_region_text(
            region_color,
            region_gray,
            psm=int(spec.get("psm", 6)),
            fallback_psm=fallback_psm,
        )
        outcome["bbox"] = {"top": int(top), "bottom": int(bottom), "width": int(width), "height": int(bottom - top)}
        results[key] = outcome

    fields = {
        "name": results["name"]["text"],
        "title": results["name"]["text"],
        "type_line": results["type_line"]["text"],
        "oracle": results["oracle"]["text"],
        "rules": results["oracle"]["text"],
        "collector": results["collector"]["text"],
    }

    combined_full_parts = [fields.get("name", ""), fields.get("type_line", ""), fields.get("oracle", ""), fields.get("collector", "")]
    fields["full"] = _clean_text(" ".join(part for part in combined_full_parts if part))

    return {
        "fields": fields,
        "regions": {key: value["bbox"] for key, value in results.items()},
        "engines": {key: value.get("engine") for key, value in results.items()},
        "raw": {key: value.get("lines", []) for key, value in results.items()},
        "candidates": {key: value.get("candidates", {}) for key, value in results.items()},
    }


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


def _composite_frames(frame_top: np.ndarray, frame_bottom: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
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
    meta = {
        "overlap_px": int(overlap_px),
        "top_rows_used": int(top_crop.shape[0] + overlap_px),
        "bottom_rows_used": int(bottom_crop.shape[0] + overlap_px),
        "width": int(composite.shape[1]),
        "height": int(composite.shape[0]),
    }
    return composite, meta


def _orient_composite(image: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Rotate the composite so card name faces up and portrait orientation is enforced."""
    oriented = image
    meta: Dict[str, Any] = {
        "rotated_90": False,
        "rotated_180": False,
    }

    height, width = oriented.shape[:2]
    if width > height:
        oriented = cv2.rotate(oriented, cv2.ROTATE_90_CLOCKWISE)
        meta["rotated_90"] = True
        height, width = oriented.shape[:2]

    upper = oriented[: height // 2, :]
    lower = oriented[height // 2 :, :]
    density_upper_initial = _calc_density(upper) if upper.size else 0.0
    density_lower_initial = _calc_density(lower) if lower.size else 0.0

    meta["density_upper_initial"] = float(density_upper_initial)
    meta["density_lower_initial"] = float(density_lower_initial)

    if density_upper_initial > density_lower_initial * 1.02:
        oriented = cv2.rotate(oriented, cv2.ROTATE_180)
        meta["rotated_180"] = True
        height, width = oriented.shape[:2]
        upper = oriented[: height // 2, :]
        lower = oriented[height // 2 :, :]

    density_upper_final = _calc_density(upper) if upper.size else 0.0
    density_lower_final = _calc_density(lower) if lower.size else 0.0
    meta["density_upper_final"] = float(density_upper_final)
    meta["density_lower_final"] = float(density_lower_final)

    return oriented, meta


def _prepare_for_ocr(image: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Enhance and binarize the composite for OCR accuracy."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
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
    }
    return cleaned, meta


def _encode_image(image: np.ndarray, ext: str, params: Optional[list] = None) -> bytes:
    ok, buf = cv2.imencode(ext, image, params or [])
    if not ok or buf is None:
        raise RuntimeError(f"Failed to encode image as {ext}")
    return bytes(buf)


def prepare_snapshot_artifacts(
    frame_top: np.ndarray,
    frame_bottom: np.ndarray,
    *,
    timestamp_slug: str,
    save_dir: Optional[Path] = None,
    jpeg_quality: int = 90,
    persist: bool = True,
    include_bytes: bool = True,
) -> Dict[str, Any]:
    """Build composite + OCR-ready images, optionally persisting them to disk."""
    save_dir = Path(save_dir) if save_dir is not None else Path("data") / "snapshots"
    jpeg_quality = int(max(10, min(jpeg_quality, 100)))

    top_aligned, bottom_aligned = _normalize_frames(frame_top, frame_bottom)
    composite_raw, composite_meta = _composite_frames(top_aligned, bottom_aligned)
    composite_oriented, orientation_meta = _orient_composite(composite_raw)
    ocr_ready, ocr_meta = _prepare_for_ocr(composite_oriented)
    ocr_result = _extract_ocr_fields(composite_oriented, ocr_ready)

    top_bytes = _encode_image(top_aligned, ".jpg", [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
    bottom_bytes = _encode_image(bottom_aligned, ".jpg", [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
    composite_bytes = _encode_image(
        composite_oriented,
        ".jpg",
        [int(cv2.IMWRITE_JPEG_QUALITY), max(jpeg_quality, 92)],
    )
    ocr_bytes = _encode_image(ocr_ready, ".png")

    base_name = f"snapshot-{timestamp_slug}"
    top_path = bottom_path = composite_path = ocr_path = None
    if persist:
        save_dir.mkdir(parents=True, exist_ok=True)
        top_path = save_dir / f"{base_name}-top.jpg"
        bottom_path = save_dir / f"{base_name}-bottom.jpg"
        composite_path = save_dir / f"{base_name}-composite.jpg"
        ocr_path = save_dir / f"{base_name}-ocr.png"
        top_path.write_bytes(top_bytes)
        bottom_path.write_bytes(bottom_bytes)
        composite_path.write_bytes(composite_bytes)
        ocr_path.write_bytes(ocr_bytes)

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
        "composite": {
            "mime": "image/jpeg",
            "path": str(composite_path) if composite_path else "",
            "shape": list(composite_oriented.shape),
            "meta": {**composite_meta, **{"orientation": orientation_meta}},
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
            "composite": composite_meta,
            "ocr": ocr_meta,
            "ocr_result": ocr_result,
        },
    }

    assets["ocr_result"] = ocr_result

    if include_bytes:
        assets["top"]["bytes"] = top_bytes
        assets["bottom"]["bytes"] = bottom_bytes
        assets["composite"]["bytes"] = composite_bytes
        assets["ocr_prepared"]["bytes"] = ocr_bytes

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

    ocr_payload = artifacts.get("ocr_result", {})

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
            "ocr": ocr_payload,
            "ocr_fields": ocr_payload.get("fields", {}),
        },
        "capture": {
            "top_shape": list(frame_top.shape),
            "bottom_shape": list(frame_bottom.shape),
        },
    }
