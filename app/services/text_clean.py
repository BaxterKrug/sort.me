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

# Common OCR noise patterns at start/end of card names
_NAME_NOISE_START = re.compile(r"^[^a-zA-Z0-9]+")  # Remove leading non-alphanumeric
_NAME_NOISE_END = re.compile(r"[^a-zA-Z0-9]+$")    # Remove trailing non-alphanumeric
_NAME_PIPE_FIX = re.compile(r"\s*\|\s*")           # Remove pipe symbols and surrounding spaces

# Common OCR misreads - map incorrect characters to likely correct ones
_OCR_CHAR_FIXES = {
    "l": "I",  # lowercase L often misread as I
    "0": "O",  # zero to O in names
    "1": "I",  # one to I in names
}

# Pattern to detect likely garbage text (repeated characters, random symbols)
_GARBAGE_PATTERN = re.compile(r"^([a-z]{1,2})\1{2,}$|^[^a-zA-Z\s]{3,}$")


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


def normalize_card_name(value: str | None) -> str:
    """
    Clean OCR noise from card names.
    Handles common OCR errors like:
    - "ff Wrath of Skies" -> "Wrath of Skies"
    - "Card Name | xX" -> "Card Name"
    - "  Name  " -> "Name"
    - "l1ght" -> "Light" (common OCR character mistakes)
    """
    if not value:
        return ""
    text = str(value)
    
    # Remove pipe symbols and surrounding spaces
    text = _NAME_PIPE_FIX.sub(" ", text)
    
    # Remove leading noise (non-alphanumeric characters)
    text = _NAME_NOISE_START.sub("", text)
    
    # Remove trailing noise (non-alphanumeric characters)
    text = _NAME_NOISE_END.sub("", text)
    
    # Strip and split into words
    text = text.strip()
    if not text:
        return ""
    
    words = text.split()
    if not words:
        return ""
    
    # Remove common OCR noise patterns:
    # - Leading 1-2 letter words that are likely noise (ff, aa, xx, etc.)
    # - Trailing 1-2 letter words (xX, aa, etc.)
    # - Words that match garbage patterns
    # But keep single letters if they're the only word (like "X" the card)
    
    if len(words) > 1:
        # Remove leading noise: 1-2 lowercase letters or mixed case junk
        while words and len(words[0]) <= 2 and not words[0][0].isupper():
            words.pop(0)
        
        # Remove trailing noise: 1-2 letter words at the end
        while len(words) > 1 and len(words[-1]) <= 2:
            words.pop()
        
        # Remove obvious garbage words (repeated chars, symbols)
        words = [w for w in words if not _GARBAGE_PATTERN.match(w)]
    
    # If we have no words left, return empty
    if not words:
        return ""
    
    # Rejoin and collapse spaces
    text = " ".join(words)
    text = _WHITESPACE_RE.sub(" ", text)
    
    return text.strip()
    
    return text.strip()


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