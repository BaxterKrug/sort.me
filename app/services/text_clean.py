"""Utilities for normalizing noisy OCR output before downstream card matching."""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Mapping

try:  # optional dictionary scoring
    from wordfreq import zipf_frequency
except Exception:  # pragma: no cover - optional dependency
    zipf_frequency = None  # type: ignore[assignment]

_WHITESPACE_RE = re.compile(r"\s+")
_ALLOWED_CHARS_RE = re.compile(r"[^a-zA-Z0-9\s.,'/:+#()&-]")
_MULTI_DASH_RE = re.compile(r"[-–—]{2,}")
_REPEAT_PUNCT_RE = re.compile(r"([.,'/:+#()&-])\1{2,}")
_COLLECTOR_RE = re.compile(r"[^0-9/]")
_LIGATURE_MAP = {
    "Æ": "Ae",
    "æ": "ae",
    "Œ": "Oe",
    "œ": "oe",
    "ß": "ss",
}
_TOKEN_STRIP_CHARS = "\"'()[]{}"
_VOWELS = set("aeiouy")
_SHORT_KEEP_WORDS = {
    "a",
    "i",
    "of",
    "to",
    "in",
    "on",
    "by",
    "as",
    "or",
    "if",
    "we",
    "me",
    "my",
    "us",
    "an",
    "be",
    "go",
    "do",
    "up",
    "ox",
}
_MANA_SYMBOLS = {"W", "U", "B", "R", "G", "C", "X"}
_SPECIAL_KEYWORDS = {
    "phyrexian",
    "rakshasa",
    "aether",
    "scryfall",
    "planeswalker",
    "lifelink",
    "trample",
    "menace",
    "hexproof",
    "ward",
    "haste",
    "vigilance",
    "equip",
    "mutate",
    "proliferate",
    "foretell",
    "adventure",
    "initiative",
    "battle",
    "enchant",
    "sagas",
    "aang",
    "lammasu",
    "akar",
    "zendikar",
    "ravnica",
    "simic",
    "izzet",
    "azorius",
    "dimir",
    "golgari",
    "selesnya",
    "boros",
    "gruul",
    "orzhov",
}
_CONSONANT_HEAVY_WORDS = {
    "crypt",
    "crypts",
    "sphinx",
    "sphinxes",
    "wyrm",
    "wyrms",
    "glyph",
    "glyphs",
    "rhythm",
}


def _strip_diacritics(text: str) -> str:
    base = "".join(_LIGATURE_MAP.get(ch, ch) for ch in text)
    normalized = unicodedata.normalize("NFKD", base)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _dictionary_frequency(word: str) -> float:
    if not word or zipf_frequency is None:
        return 0.0
    try:
        return float(zipf_frequency(word, "en"))
    except Exception:  # pragma: no cover - defensive fallback
        return 0.0


def _normalized_token(token: str) -> str:
    token = token.strip(_TOKEN_STRIP_CHARS)
    return token


def _should_keep_token(token: str) -> bool:
    normalized = _normalized_token(token)
    if not normalized:
        return False
    core = normalized.replace("-", "")
    lower = core.lower()
    upper = core.upper()
    letters = sum(ch.isalpha() for ch in core)
    digits = sum(ch.isdigit() for ch in core)
    vowel_count = sum((ch in _VOWELS) for ch in lower if ch.isalpha())
    freq = _dictionary_frequency(lower)

    if lower in _SPECIAL_KEYWORDS:
        return True
    if upper in _MANA_SYMBOLS:
        return True
    if letters == 0:
        return digits > 0 or "/" in normalized
    if letters == 1:
        return lower in _SHORT_KEEP_WORDS or upper in _MANA_SYMBOLS
    if letters == 2:
        if vowel_count == 0:
            return lower in _SHORT_KEEP_WORDS or freq >= 3.0
        return True
    if normalized.isupper() and len(normalized) >= 2 and lower not in _SPECIAL_KEYWORDS:
        if vowel_count <= 1 and freq < 3.0:
            return False
    if vowel_count == 0 and lower not in _CONSONANT_HEAVY_WORDS:
        return freq >= 2.7
    vowel_ratio = vowel_count / max(letters, 1)
    if vowel_ratio < 0.18 and lower not in _CONSONANT_HEAVY_WORDS:
        if freq < 2.3:
            return False
    if digits and digits >= letters and lower not in _SPECIAL_KEYWORDS:
        return freq >= 3.0
    return True


def _filter_line_tokens(line: str, region: str | None = None) -> str:
    if not line:
        return ""
    if region == "collector":
        return line.strip()
    tokens = line.split()
    filtered = [tok for tok in tokens if _should_keep_token(tok)]
    if not filtered and tokens:
        # keep the longest token for names so we don't drop real cards entirely
        fallback = max(tokens, key=len)
        if region == "name":
            filtered = [fallback]
    return " ".join(filtered).strip()


def _sanitize_line(line: str) -> str:
    line = line.replace("|", "I")
    line = _ALLOWED_CHARS_RE.sub(" ", line)
    line = _WHITESPACE_RE.sub(" ", line)
    line = _MULTI_DASH_RE.sub("-", line)
    line = _REPEAT_PUNCT_RE.sub(r"\1", line)
    return line.strip(" .,:;-/")


def normalize_collector(value: str) -> str:
    text = str(value or "")
    text = _strip_diacritics(text)
    text = text.lower()
    text = text.replace("collector", "")
    text = text.replace("col", "")
    text = text.replace(" ", "")
    text = _COLLECTOR_RE.sub("", text)
    return text[:16]


def normalize_ocr_text(
    value: str | None,
    *,
    region: str | None = None,
    max_lines: int = 6,
    max_chars: int = 400,
    min_line_len: int = 3,
) -> str:
    if not value:
        return ""
    text = _strip_diacritics(str(value))
    text = text.replace("\r", "\n")
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    lines = []
    for raw in text.splitlines():
        sanitized = _sanitize_line(raw)
        filtered = _filter_line_tokens(sanitized, region=region)
        if len(filtered) < min_line_len:
            continue
        if lines and filtered == lines[-1]:
            continue
        lines.append(filtered)
        if len(lines) >= max_lines:
            break
    cleaned = "\n".join(lines).strip()
    if region == "collector":
        cleaned = normalize_collector(cleaned)
    elif region == "name":
        cleaned = re.sub(r"\s+\d[\w\s/+-]*$", "", cleaned).strip()
    if len(cleaned) > max_chars:
        cutoff = cleaned[:max_chars]
        space_idx = cutoff.rfind(" ")
        cleaned = cutoff if space_idx == -1 else cutoff[:space_idx]
    return cleaned.strip()


def normalize_ocr_map(ocr_map: Mapping[str, str | None]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for key, value in ocr_map.items():
        normalized[key] = normalize_ocr_text(value, region=key)
    return normalized
