"""Lightweight helpers for normalizing OCR output before downstream use."""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Mapping

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\u2018\u2019]")
_NON_PRINTABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_COLLECTOR_RE = re.compile(r"[^0-9a-z/]")
_COLLECTOR_SEGMENT_RE = re.compile(r"\d+[a-z]?(?:/\d+[a-z]?)?")
_LIGATURE_MAP = {
    "Æ": "Ae",
    "æ": "ae",
    "Œ": "Oe",
    "œ": "oe",
    "ß": "ss",
}


def _strip_diacritics(text: str) -> str:
    base = "".join(_LIGATURE_MAP.get(ch, ch) for ch in text)
    normalized = unicodedata.normalize("NFKD", base)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _sanitize_line(text: str) -> str:
    text = text.replace("|", "I")
    text = _PUNCT_RE.sub("'", text)
    text = _NON_PRINTABLE_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def normalize_collector(value: str | None) -> str:
    if not value:
        return ""
    text = _strip_diacritics(str(value)).lower()
    text = text.replace("collector", "")
    text = text.replace("col", "")
    text = text.replace(" ", "")
    text = _COLLECTOR_RE.sub("", text)
    matches = _COLLECTOR_SEGMENT_RE.findall(text)
    if matches:
        text = max(matches, key=len)
    return text[:16]


def normalize_ocr_text(
    value: str | None,
    *,
    region: str | None = None,
    max_lines: int = 6,
    max_chars: int = 400,
) -> str:
    if not value:
        return ""
    text = _strip_diacritics(str(value))
    text = text.replace("\r", "\n")
    segments = []
    for raw in text.splitlines():
        cleaned = _sanitize_line(raw)
        if not cleaned:
            continue
        if segments and cleaned == segments[-1]:
            continue
        segments.append(cleaned)
        if len(segments) >= max_lines:
            break
    normalized = "\n".join(segments).strip()
    if region == "collector":
        normalized = normalize_collector(normalized)
    if len(normalized) > max_chars:
        normalized = normalized[:max_chars].rsplit(" ", 1)[0]
    return normalized.strip()


def normalize_ocr_map(ocr_map: Mapping[str, str | None]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for key, value in ocr_map.items():
        normalized[key] = normalize_ocr_text(value, region=key)
    return normalized