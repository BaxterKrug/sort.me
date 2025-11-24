"""
Card identification helper.

Given OCR region->text (e.g. {'name': "...", 'oracle': "...", 'collector': "12/264", ...})
attempt to find the best matching card from a local card database.

Database loader supports:
 - JSON file containing a list of card objects (common keys: 'name','oracle_text','collector_number','set','id')
 - NDJSON (one JSON object per line)
 - SQLite DB with a 'cards' table (columns: name, oracle_text, collector_number, set_code, id)

Matching strategy:
 - exact normalized name -> immediate match
 - collector number + set -> strong match
 - fuzzy name match (rapidfuzz if available, otherwise difflib)
 - refine candidates by checking oracle/type tokens overlap
 - return best candidate + debug scoring info
"""

from typing import Dict, Any, List, Optional, Tuple
from . import text_clean
import json
import os
import sqlite3
import unicodedata
import re
import logging
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from scipy import sparse
from sklearn.neighbors import NearestNeighbors
from sklearn.feature_extraction.text import TfidfVectorizer

LOG = logging.getLogger("sort.card_id")

_DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"
_EMBED_META_FILE = "embeddings.meta.json"
_EMBED_CACHES: Dict[str, Dict[str, Any]] = {}


def _dynamic_embed_batch_size() -> int:
    try:
        return max(8, int(os.environ.get("SORT_CARD_EMBED_BATCH", "256")))
    except Exception:
        return 256


def _runtime_sentence_build_enabled() -> bool:
    value = os.environ.get("SORT_CARD_EMBED_RUNTIME_BUILD", "0")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class _TfidfEncoder:
    """Minimal encoder wrapper so TF-IDF vectors slot into existing pipeline."""

    def __init__(self, vectorizer: TfidfVectorizer):
        self.vectorizer = vectorizer

    def encode(self, texts: List[str], convert_to_numpy: bool = True):  # pylint: disable=unused-argument
        return self.vectorizer.transform(texts)

# try to use rapidfuzz for better fuzzy matching, otherwise fallback
try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except Exception:
    HAVE_RAPIDFUZZ = False
    import difflib

# ------ helpers ------

def _normalize(s: Optional[str]) -> str:
    if not s:
        return ""
    s = str(s)
    # remove diacritics, lowercase, remove punctuation, collapse whitespace
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def load_local_db(path: str) -> List[Dict[str, Any]]:
    """
    Load local db from JSON/NDJSON or SQLite file. Returns a list of card dicts.
    Expected minimal keys per card: 'name' and ideally 'oracle_text'/'collector_number'/'set'
    """
    if not path:
        return []
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.endswith(".json"):
        with open(path, "r", encoding="utf8") as fh:
            data = json.load(fh)
            if isinstance(data, dict) and "data" in data:
                # some exports (scryfall) wrap list in "data"
                data = data["data"]
            return data
    if path.endswith(".ndjson") or path.endswith(".ndjsonl") or path.endswith(".ndjsonl.txt"):
        out = []
        with open(path, "r", encoding="utf8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out
    # try sqlite
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        # try common column names
        for colset in (("name","oracle_text","collector_number","set","id"),
                       ("name","oracle","collector_number","set_code","id"),
                       ("name","oracle_text","collector","set_code","id")):
            try:
                col_list = ", ".join(col for col in colset if col)
                cur.execute(f"SELECT {col_list} FROM cards LIMIT 1")
                rows = cur.fetchall()
                # if query succeeded, fetch all rows
                cur.execute(f"SELECT {col_list} FROM cards")
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                out = []
                for r in rows:
                    out.append({cols[i]: r[i] for i in range(len(cols))})
                conn.close()
                return out
            except Exception:
                continue
        conn.close()
    except Exception:
        pass
    # fallback: try to read as JSON anyway
    with open(path, "r", encoding="utf8") as fh:
        try:
            return json.load(fh)
        except Exception:
            raise RuntimeError("Unsupported DB format or no cards found")

# ------ scoring / matching ------

def _name_candidates_from_db(name: str, cards: List[Dict[str,Any]], top_n: int = 10) -> List[Tuple[Dict[str,Any], float]]:
    """
    Return up to top_n candidate cards with a name similarity score (0..100).
    """
    if not name:
        return []
    norm_name = _normalize(name)
    # build name list and mapping
    name_map = {}
    names = []
    for c in cards:
        n = _normalize(c.get("name") or c.get("title") or "")
        names.append(n)
        name_map[n] = name_map.get(n, []) + [c]
    if HAVE_RAPIDFUZZ:
        # rapidfuzz can return (match, score, index)
        choices = list(set(names))
        matches = rf_process.extract(norm_name, choices, scorer=rf_fuzz.WRatio, limit=top_n)
        out = []
        for match, score, _ in matches:
            for card in name_map.get(match, []):
                out.append((card, float(score)))
        return out
    else:
        # difflib fallback
        choices = list(set(names))
        matches = difflib.get_close_matches(norm_name, choices, n=top_n, cutoff=0.0)
        out = []
        for m in matches:
            # approximate score with SequenceMatcher ratio *100
            score = int(difflib.SequenceMatcher(None, norm_name, m).ratio() * 100)
            for card in name_map.get(m, []):
                out.append((card, float(score)))
        return out

def _oracle_overlap_score(ocr_oracle: str, card_oracle: str) -> float:
    """
    Compute a simple token overlap score (0..1) between OCR oracle text and card oracle text.
    """
    if not ocr_oracle or not card_oracle:
        return 0.0
    toks_a = set([t for t in re.split(r"\W+", _normalize(ocr_oracle)) if t])
    toks_b = set([t for t in re.split(r"\W+", _normalize(card_oracle)) if t])
    if not toks_a or not toks_b:
        return 0.0
    inter = toks_a.intersection(toks_b)
    # Jaccard-like but weighted towards OCR tokens (we want most OCR tokens found in the card)
    score = len(inter) / max(1, len(toks_a))
    return float(score)


def _compose_embedding_query(ocr_map: Dict[str, str]) -> str:
    parts: List[str] = []
    for key in ("name", "oracle", "rules", "collector", "full", "full_text"):
        value = ocr_map.get(key)
        cleaned = text_clean.normalize_ocr_text(value, region=key, max_lines=4, max_chars=320)
        if cleaned:
            parts.append(cleaned)
    # deduplicate while preserving order
    seen = set()
    unique_parts: List[str] = []
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        unique_parts.append(part)
    return "\n".join(unique_parts).strip()


def _prepare_metadata_cards(cards: List[Any]) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        card = dict(raw)
        card_id = card.get("id") or card.get("scryfall_id")
        if card_id and not card.get("scryfall_id"):
            card["scryfall_id"] = card_id
        prepared.append(card)
    return prepared


def _build_card_text(card: Dict[str, Any]) -> str:
    parts: List[str] = []
    keys = (
        "name",
        "printed_name",
        "flavor_name",
        "oracle_text",
        "type_line",
        "set",
        "set_code",
        "set_name",
        "collector_number",
    )
    for key in keys:
        value = card.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            value = str(value)
        cleaned = value.strip()
        if cleaned:
            parts.append(cleaned)
    if not parts:
        card_id = card.get("scryfall_id") or card.get("id")
        if card_id:
            parts.append(str(card_id))
    return " ".join(parts)


def _distance_to_score(distance: float, metric: str) -> float:
    if metric == "cosine":
        return max(0.0, (1.0 - float(distance)) * 100.0)
    return max(0.0, 100.0 - (float(distance) * 100.0))


def _resolve_embeddings_dir(directory: str) -> Path:
    path = Path(directory).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _build_tfidf_cache(resolved: str, metadata: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not metadata:
        return None
    try:
        texts: List[str] = []
        for idx, raw_card in enumerate(metadata):
            card = raw_card if isinstance(raw_card, dict) else {}
            text = _build_card_text(card).strip()
            if not text:
                text = f"card-{idx}"
            texts.append(text)
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5))
        matrix = vectorizer.fit_transform(texts)
        if matrix.shape[0] == 0 or matrix.shape[1] == 0:
            return None
        nn = NearestNeighbors(metric="cosine", algorithm="brute")
        nn.fit(matrix)
        encoder = _TfidfEncoder(vectorizer)
        return {
            "ready": True,
            "embeddings": matrix,
            "meta": metadata,
            "nn": nn,
            "dir": resolved,
            "encoder": encoder,
            "encoder_failed": False,
            "encoder_error": None,
            "model_name": "tfidf-char-ngram",
            "vectorizer": vectorizer,
            "distance_metric": "cosine",
        }
    except Exception as exc:  # pragma: no cover - defensive fallback
        LOG.warning("Failed to build TF-IDF embeddings in %s: %s", resolved, exc)
        return None


def _embedding_meta_path(resolved: str) -> Path:
    return Path(resolved) / _EMBED_META_FILE


def _load_embedding_meta(resolved: str) -> Dict[str, Any]:
    path = _embedding_meta_path(resolved)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive IO
        LOG.debug("Failed to read embedding meta info from %s: %s", path, exc)
        return {}


def _write_embedding_meta(resolved: str, info: Dict[str, Any]) -> None:
    path = _embedding_meta_path(resolved)
    try:
        with path.open("w", encoding="utf8") as fh:
            json.dump(info, fh)
    except Exception as exc:  # pragma: no cover - defensive IO
        LOG.warning("Failed to write embedding meta to %s: %s", path, exc)


def _extract_inline_embeddings(
    metadata: List[Dict[str, Any]],
    *,
    key: str = "embedding",
) -> Tuple[Optional[np.ndarray], List[Dict[str, Any]]]:
    vectors: List[np.ndarray] = []
    stripped_meta: List[Dict[str, Any]] = []
    dims: Optional[int] = None
    for card in metadata:
        vec = card.get(key)
        if not isinstance(vec, (list, tuple)):
            continue
        try:
            arr = np.asarray(vec, dtype=np.float32)
        except Exception:  # pragma: no cover - resilience to malformed entries
            continue
        if arr.ndim != 1:
            continue
        if dims is None:
            dims = arr.shape[0]
        if arr.shape[0] != dims:
            LOG.debug(
                "Skipping card %s due to embedding dim mismatch (%s != %s)",
                card.get("id") or card.get("scryfall_id"),
                arr.shape[0],
                dims,
            )
            continue
        vectors.append(arr)
        trimmed = dict(card)
        trimmed.pop(key, None)
        stripped_meta.append(trimmed)
    if not vectors:
        return None, metadata
    matrix = np.vstack(vectors)
    if len(stripped_meta) != len(metadata):
        LOG.debug(
            "Inline embeddings available for %d/%d cards", len(stripped_meta), len(metadata)
        )
    return matrix, stripped_meta


def _build_embedding_cache(
    resolved: str,
    embeddings: Any,
    metadata: List[Dict[str, Any]],
    model_name: Optional[str],
    metric: Optional[str],
) -> Optional[Dict[str, Any]]:
    if embeddings is None or not metadata:
        return None
    arr = np.asarray(embeddings, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2 or arr.size == 0:
        return None
    rows = arr.shape[0]
    if rows != len(metadata):
        limit = min(rows, len(metadata))
        LOG.warning(
            "Embedding rows (%s) do not match metadata count (%s) in %s; truncating to %s",
            rows,
            len(metadata),
            resolved,
            limit,
        )
        arr = arr[:limit]
        metadata = metadata[:limit]
        rows = limit
    if rows == 0:
        return None
    nn_metric = "cosine" if str(metric).lower() == "cosine" else "euclidean"
    algorithm = "brute" if nn_metric == "cosine" else "auto"
    nn = NearestNeighbors(metric=nn_metric, algorithm=algorithm)
    nn.fit(arr)
    cache = {
        "ready": True,
        "embeddings": arr,
        "meta": metadata,
        "nn": nn,
        "dir": resolved,
        "encoder": None,
        "encoder_failed": False,
        "encoder_error": None,
        "model_name": model_name or _DEFAULT_EMBED_MODEL,
        "distance_metric": nn_metric,
    }
    return cache


def _build_sentence_cache(resolved: str, metadata: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not metadata:
        return None
    texts: List[str] = []
    filtered_meta: List[Dict[str, Any]] = []
    for idx, card in enumerate(metadata):
        text = _build_card_text(card).strip()
        if not text:
            text = f"card-{idx}"
        texts.append(text)
        filtered_meta.append(card)
    if not texts:
        return None
    cache_seed: Dict[str, Any] = {
        "model_name": os.environ.get("SORT_CARD_EMBED_MODEL", _DEFAULT_EMBED_MODEL),
        "encoder": None,
        "encoder_failed": False,
        "encoder_error": None,
    }
    encoder = _ensure_sentence_encoder(cache_seed)
    if encoder is None:
        return None
    batch_size = _dynamic_embed_batch_size()
    LOG.info(
        "Generating %d card embeddings via %s (batch=%d)",
        len(texts),
        cache_seed["model_name"],
        batch_size,
    )
    embeddings = encoder.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if embeddings.size == 0:
        return None
    meta_info = {
        "model_name": cache_seed["model_name"],
        "distance_metric": "cosine",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    emb_path = Path(resolved) / "embeddings.npy"
    try:
        np.save(str(emb_path), embeddings)
    except Exception as exc:  # pragma: no cover - IO safeguards
        LOG.warning("Failed to persist generated embeddings to %s: %s", emb_path, exc)
    else:
        _write_embedding_meta(resolved, meta_info)
    nn = NearestNeighbors(metric="cosine", algorithm="brute")
    nn.fit(embeddings)
    return {
        "ready": True,
        "embeddings": embeddings,
        "meta": filtered_meta,
        "nn": nn,
        "dir": resolved,
        "encoder": cache_seed.get("encoder"),
        "encoder_failed": False,
        "encoder_error": None,
        "model_name": cache_seed["model_name"],
        "distance_metric": "cosine",
    }


def _get_embedding_cache(directory: Optional[str]) -> Optional[Dict[str, Any]]:
    if not directory:
        return None
    resolved = str(_resolve_embeddings_dir(directory))
    cache = _EMBED_CACHES.get(resolved)
    if cache and cache.get("ready"):
        return cache
    emb_path = Path(resolved) / "embeddings.npy"
    meta_path = Path(resolved) / "cards_metadata.json"
    metadata: List[Dict[str, Any]] = []
    if meta_path.exists():
        try:
            with meta_path.open("r", encoding="utf8") as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                metadata = raw
            else:
                LOG.warning("cards_metadata.json at %s is not a list", meta_path)
        except Exception as exc:  # pragma: no cover - defensive IO
            LOG.warning("Failed to read metadata from %s: %s", meta_path, exc)
    else:
        LOG.debug("cards_metadata.json missing in %s", resolved)
    metadata = _prepare_metadata_cards(metadata)
    meta_info = _load_embedding_meta(resolved)

    inline_embeddings, inline_meta = _extract_inline_embeddings(metadata)
    if inline_embeddings is not None:
        cache = _build_embedding_cache(
            resolved,
            inline_embeddings,
            inline_meta,
            meta_info.get("model_name") if meta_info else None,
            meta_info.get("distance_metric") if meta_info else "cosine",
        )
        if cache:
            _EMBED_CACHES[resolved] = cache
            return cache

    if emb_path.exists():
        try:
            embeddings = np.load(str(emb_path))
        except Exception as exc:  # pragma: no cover - IO safeguards
            LOG.warning("Failed to load embeddings from %s: %s", resolved, exc)
            _EMBED_CACHES[resolved] = {"ready": False, "error": "load_failed"}
            return None
        cache = _build_embedding_cache(
            resolved,
            embeddings,
            metadata,
            meta_info.get("model_name") if meta_info else None,
            meta_info.get("distance_metric") if meta_info else "euclidean",
        )
        if cache:
            _EMBED_CACHES[resolved] = cache
            return cache
        LOG.warning("Failed to initialize embedding cache from %s", emb_path)

    if _runtime_sentence_build_enabled():
        sentence_cache = _build_sentence_cache(resolved, metadata)
        if sentence_cache:
            _EMBED_CACHES[resolved] = sentence_cache
            return sentence_cache
    else:
        LOG.info(
            "Runtime embedding build disabled (SORT_CARD_EMBED_RUNTIME_BUILD=0); "
            "use embed_scryfall.py or scripts/embed_single_card.py to precompute vectors."
        )
    fallback_cache = _build_tfidf_cache(resolved, metadata)
    if fallback_cache:
        _EMBED_CACHES[resolved] = fallback_cache
        return fallback_cache
    LOG.debug("Embedding assets missing in %s", resolved)
    _EMBED_CACHES[resolved] = {"ready": False, "error": "files_missing"}
    return None


def _ensure_sentence_encoder(cache: Dict[str, Any]):
    if cache.get("encoder") is not None:
        return cache["encoder"]
    if cache.get("encoder_failed"):
        return None
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - optional dependency
        cache["encoder_failed"] = True
        cache["encoder_error"] = f"sentence_transformers_import_failed: {exc}"
        LOG.debug("SentenceTransformer unavailable: %s", exc)
        return None
    model_name = cache.get("model_name") or _DEFAULT_EMBED_MODEL
    try:
        encoder = SentenceTransformer(model_name)
    except Exception as exc:  # pragma: no cover - runtime/env dependent
        cache["encoder_failed"] = True
        cache["encoder_error"] = f"encoder_load_failed: {exc}"
        LOG.warning("Failed to load SentenceTransformer %s: %s", model_name, exc)
        return None
    cache["encoder"] = encoder
    return encoder


def _run_embedding_search(
    query_text: str,
    cache: Dict[str, Any],
    encoder: Any,
    max_results: int,
) -> List[Dict[str, Any]]:
    if not query_text:
        return []
    try:
        query_emb = encoder.encode(
            [query_text],
            convert_to_numpy=True,
        )
    except Exception as exc:  # pragma: no cover - runtime dependent
        cache["encoder_failed"] = True
        cache["encoder_error"] = f"encode_failed: {exc}"
        LOG.warning("Embedding encode failed: %s", exc)
        return []
    embeddings = cache.get("embeddings")
    nn = cache.get("nn")
    if embeddings is None or not isinstance(nn, NearestNeighbors):
        return []
    neighbor_count = min(max_results, embeddings.shape[0])
    if neighbor_count <= 0:
        return []
    if sparse.issparse(query_emb):
        query_vec = query_emb
    else:
        arr = np.asarray(query_emb)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        query_vec = arr
    dists, idxs = nn.kneighbors(query_vec, n_neighbors=neighbor_count)
    matches: List[Dict[str, Any]] = []
    metric = cache.get("distance_metric", "euclidean")
    for dist, idx in zip(dists[0], idxs[0]):
        meta = cache["meta"][int(idx)] if cache.get("meta") else {}
        matches.append(
            {
                "card": meta,
                "distance": float(dist),
                "score": _distance_to_score(float(dist), metric),
                "index": int(idx),
            }
        )
    return matches


def embedding_matches_from_ocr(
    ocr_map: Dict[str, str],
    embeddings_dir: Optional[str],
    *,
    max_results: int = 5,
) -> Dict[str, Any]:
    """Return embedding-based nearest matches for an OCR map."""
    info: Dict[str, Any] = {
        "query": "",
        "matches": [],
        "best": None,
        "engine": None,
        "error": None,
        "available": False,
    }
    if not embeddings_dir:
        info["error"] = "embeddings_dir_missing"
        return info
    query_text = _compose_embedding_query(ocr_map)
    info["query"] = query_text
    if not query_text:
        info["error"] = "empty_query"
        return info
    cache = _get_embedding_cache(embeddings_dir)
    if not cache:
        info["error"] = "embedding_index_unavailable"
        return info
    encoder = _ensure_sentence_encoder(cache)
    if encoder is None:
        info["error"] = cache.get("encoder_error") or "encoder_unavailable"
        return info
    matches = _run_embedding_search(query_text, cache, encoder, max_results)
    info["matches"] = matches
    info["engine"] = cache.get("model_name") or _DEFAULT_EMBED_MODEL
    if matches:
        info["best"] = matches[0]
        info["available"] = True
    elif cache.get("encoder_error"):
        info["error"] = cache["encoder_error"]
    return info

# ------ public API ------

def identify_card_from_ocr(ocr_map: Dict[str,str],
                           db_path: Optional[str] = None,
                           cards_list: Optional[List[Dict[str,Any]]] = None,
                           top_n: int = 8,
                           name_weight: float = 0.75,
                           oracle_weight: float = 0.20,
                           collector_weight: float = 0.05
                           ,
                           embeddings_dir: Optional[str] = None
                           ) -> Dict[str,Any]:
    """
    Identify the most probable card given OCR regions.

    Returns:
     {
       'best': {card dict or None},
       'score': combined_score (0..100),
       'candidates': [ {card, name_score, oracle_score, collector_score, total_score}, ... ],
       'debug': {...}
     }

    Provide either db_path to load local DB or cards_list directly.
    """
    # If embeddings_dir provided, allow running without a local card DB (embedding lookup uses its own metadata)
    if not cards_list and not db_path and not embeddings_dir:
        raise ValueError("Provide db_path, cards_list or embeddings_dir")
    cards = cards_list if cards_list is not None else (load_local_db(db_path) if db_path else [])
    # normalize OCRed regions
    o_name = (ocr_map.get("name") or ocr_map.get("title") or "").strip()
    o_oracle = (ocr_map.get("oracle") or ocr_map.get("rules") or "").strip()
    o_full = (ocr_map.get("full") or "").strip()
    o_collector = (ocr_map.get("collector") or "").strip()

    norm_o_name = _normalize(o_name)

    results = {
        'best': None,
        'score': 0.0,
        'candidates': [],
        'debug': {
            'ocr_name': o_name,
            'ocr_oracle': o_oracle,
            'ocr_collector': o_collector,
            'num_cards_in_db': len(cards)
        }
    }

    # --- optional embedding-based matching (if precomputed embeddings exist) ---
    query_text = _compose_embedding_query(ocr_map)
    results['debug']['ocr_query'] = query_text
    if embeddings_dir:
        embed_info = embedding_matches_from_ocr(ocr_map, embeddings_dir, max_results=max(8, top_n))
        results['debug']['embedding'] = embed_info
        matches = embed_info.get('matches') or []
        if matches:
            cand_list = []
            for match in matches:
                card = match.get('card') or {}
                score_val = float(match.get('score') or 0.0)
                candidate = {
                    'card': card,
                    'name_score': score_val,
                    'oracle_score': 0.0,
                    'collector_score': 0.0,
                    'total_score': score_val
                }
                if 'distance' in match:
                    candidate['distance'] = float(match.get('distance') or 0.0)
                candidate['source'] = 'embedding'
                cand_list.append(candidate)
            if cand_list:
                results['candidates'] = cand_list
                results['best'] = cand_list[0]['card']
                results['score'] = cand_list[0]['total_score']
                results['debug']['embed_match'] = True
                return results

    # 1) try exact normalized name match
    if norm_o_name:
        for c in cards:
            if _normalize(c.get("name") or c.get("title") or "") == norm_o_name:
                # immediate perfect-ish match
                results['best'] = c
                results['score'] = 100.0
                results['candidates'] = [{
                    'card': c, 'name_score': 100.0, 'oracle_score': 1.0, 'collector_score': 1.0, 'total_score': 100.0
                }]
                return results

    # 2) if collector present, try collector+set exact match (collector often unique)
    if o_collector:
        oc_norm = o_collector.strip()
        for c in cards:
            cc = str(c.get("collector_number") or c.get("collector") or "").strip()
            setc = str(c.get("set") or c.get("set_code") or "").strip()
            if cc and cc == oc_norm:
                # prefer if set matches or name similar
                name_score = 100.0 if norm_o_name and _normalize(c.get("name","")) == norm_o_name else 85.0
                total = name_score * name_weight + 100.0 * collector_weight
                results['best'] = c
                results['score'] = total
                results['candidates'] = [{
                    'card': c, 'name_score': name_score, 'oracle_score': 0.0, 'collector_score': 100.0, 'total_score': total
                }]
                return results

    # 3) fuzzy name candidates
    name_cands = _name_candidates_from_db(o_name, cards, top_n=top_n)
    scored = []
    for cand, name_score in name_cands:
        oracle_score = _oracle_overlap_score(o_oracle, cand.get("oracle_text") or cand.get("oracle") or "")
        collector_score = 100.0 if o_collector and str(cand.get("collector_number") or cand.get("collector") or "").strip() == o_collector.strip() else 0.0
        # combine into 0..100
        total = (name_score * name_weight) + (oracle_score * 100.0 * oracle_weight) + (collector_score * collector_weight)
        scored.append({
            'card': cand,
            'name_score': float(name_score),
            'oracle_score': float(oracle_score),
            'collector_score': float(collector_score),
            'total_score': float(total)
        })
    # sort by total_score desc
    scored.sort(key=lambda x: x['total_score'], reverse=True)
    results['candidates'] = scored
    if scored:
        results['best'] = scored[0]['card']
        results['score'] = scored[0]['total_score']
    return results

# small CLI for quick manual testing
if __name__ == "__main__":
    import sys, pprint
    if len(sys.argv) < 3:
        print("Usage: card_id.py <db.json|db.sqlite> <image_ocr_json>")
        print("Example: card_id.py /path/to/cards.json sample_ocr.json")
        sys.exit(1)
    db = sys.argv[1]
    ocr_file = sys.argv[2]
    with open(ocr_file, "r", encoding="utf8") as fh:
        ocr_map = json.load(fh)
    out = identify_card_from_ocr(ocr_map, db_path=db)
    pprint.pprint(out)