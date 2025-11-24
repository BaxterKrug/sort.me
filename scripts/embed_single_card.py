#!/usr/bin/env python3
"""Embed a small set of cards from cards_metadata.json.

This utility lets you build a tiny embedding index (even just 1 card) so the
server can load lightweight metadata at startup without generating vectors for
all ~110k cards. Point the FastAPI service at the generated directory via
sorting.embeddings_dir in config.yaml.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List
from datetime import datetime, timezone

import numpy as np

from app.services import card_id


def _load_metadata(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        raise SystemExit(f"Metadata file not found: {path}")
    with path.open("r", encoding="utf8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise SystemExit(f"Expected list in {path}, got {type(data).__name__}")
    return [c for c in data if isinstance(c, dict)]


def _canonical_id(card: Dict[str, object]) -> str:
    value = card.get("id") or card.get("scryfall_id") or card.get("uuid")
    return str(value or "").strip().lower()


def _select_cards(
    metadata: List[Dict[str, object]],
    ids: List[str],
    names: List[str],
) -> List[Dict[str, object]]:
    id_set = {s.strip().lower() for s in ids if s.strip()}
    name_filters = [s.strip().lower() for s in names if s.strip()]
    selected = []
    seen = set()
    for card in metadata:
        cid = _canonical_id(card)
        cname = str(card.get("name") or "").strip().lower()
        match_id = cid and cid in id_set
        match_name = name_filters and any(filter_val in cname for filter_val in name_filters)
        if not match_id and not match_name:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        selected.append(card)
    return selected


def _ensure_output_dir(path: Path, force: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()) and not force:
        raise SystemExit(
            f"Output directory {path} is not empty. Use --force to overwrite its contents."
        )


def _build_text(card: Dict[str, object]) -> str:
    # reuse existing helper for consistent text formatting
    return card_id._build_card_text(card).strip()  # type: ignore[attr-defined]


def _infer_model_name(metadata_path: Path, override: str | None) -> str:
    if override:
        return override
    meta_info_path = metadata_path.parent / "embeddings.meta.json"
    if meta_info_path.exists():
        try:
            with meta_info_path.open("r", encoding="utf8") as fh:
                info = json.load(fh)
        except Exception:
            info = None
        if isinstance(info, dict):
            model = info.get("model_name")
            if isinstance(model, str) and model.strip():
                return model.strip()
    return card_id._DEFAULT_EMBED_MODEL  # type: ignore[attr-defined]


def _load_sentence_transformer(model_name: str):
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - dependency error surfaced to user
        raise SystemExit(
            "sentence-transformers required; install with `pip install sentence-transformers`."
        ) from exc
    try:
        return SentenceTransformer(model_name)
    except Exception as exc:  # pragma: no cover - runtime/env failure
        raise SystemExit(f"Failed to load SentenceTransformer model {model_name!r}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata",
        default="data/embeddings/cards_metadata.json",
        help="Path to cards_metadata.json (default: data/embeddings/cards_metadata.json)",
    )
    parser.add_argument(
        "--card-id",
        action="append",
        dest="card_ids",
        default=[],
        help="Scryfall ID to embed (repeat for multiple).",
    )
    parser.add_argument(
        "--name",
        action="append",
        dest="names",
        default=[],
        help="Case-insensitive substring to select by card name (repeatable).",
    )
    parser.add_argument(
        "--out-dir",
        default="data/embeddings/custom",
        help="Directory where embeddings.npy + cards_metadata.json will be written.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="SentenceTransformer model override (default: inferred from embeddings.meta.json)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=128,
        help="Encoding batch size (default: 128).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output directory if files already exist.",
    )
    args = parser.parse_args()

    if not args.card_ids and not args.names:
        parser.error("Provide at least one --card-id or --name filter to keep the subset small.")

    metadata_path = Path(args.metadata)
    metadata = _load_metadata(metadata_path)
    subset = _select_cards(metadata, args.card_ids, args.names)
    if not subset:
        raise SystemExit("No cards matched the provided filters.")

    model_name = _infer_model_name(metadata_path, args.model)
    texts = [_build_text(card) or card.get("name") or "unknown-card" for card in subset]
    print(f"Using sentence-transformers model: {model_name}")
    model = _load_sentence_transformer(model_name)
    embeddings = model.encode(
        texts,
        batch_size=max(8, args.batch_size),
        show_progress_bar=True,
        convert_to_numpy=True,
    )
    embeddings = np.asarray(embeddings, dtype=np.float32)

    out_dir = Path(args.out_dir)
    _ensure_output_dir(out_dir, args.force)
    emb_path = out_dir / "embeddings.npy"
    meta_path = out_dir / "cards_metadata.json"
    meta_info_path = out_dir / "embeddings.meta.json"

    np.save(str(emb_path), embeddings)
    for idx, vector in enumerate(embeddings.tolist()):
        subset[idx]["embedding"] = vector
    with meta_path.open("w", encoding="utf8") as fh:
        json.dump(subset, fh, ensure_ascii=False, indent=2)

    meta_info = {
        "model_name": model_name,
        "distance_metric": "cosine",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with meta_info_path.open("w", encoding="utf8") as fh:
        json.dump(meta_info, fh, ensure_ascii=False, indent=2)

    print(f"Wrote {embeddings.shape[0]} embeddings to {emb_path}")
    print(f"Metadata saved to {meta_path}")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
