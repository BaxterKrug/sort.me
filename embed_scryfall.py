#!/usr/bin/env python3
"""
embed_scryfall.py

Create sentence-transformer embeddings for cards in scryfall_all_cards.json.

Outputs:
 - embeddings.npy         (float32, shape: N x D)
 - cards_metadata.json    (list of dicts with minimal metadata in same order)

Usage:
  python embed_scryfall.py --input data/scryfall_all_cards.json --out-dir data/embeddings

Requirements:
  pip install sentence-transformers numpy tqdm
"""
import os
import json
import argparse
from tqdm import tqdm
import numpy as np

def build_text(card: dict) -> str:
    # Combine key text fields into a single string for embedding.
    parts = []
    if card.get("name"):
        parts.append(card["name"])
    if card.get("type_line"):
        parts.append(card["type_line"])
    # oracle_text may be long; include it
    if card.get("oracle_text"):
        parts.append(card["oracle_text"])
    return " | ".join(parts).strip()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", "-i", required=True, help="Path to scryfall_all_cards.json")
    parser.add_argument("--out-dir", "-o", default="data/embeddings", help="Output directory")
    parser.add_argument("--model", default="all-MiniLM-L6-v2", help="SentenceTransformers model (default: all-MiniLM-L6-v2)")
    parser.add_argument("--batch-size", type=int, default=256, help="Batch size for encoding")
    parser.add_argument("--dtype", choices=["float32","float16"], default="float32", help="Output dtype for embeddings")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    print("Loading cards from", args.input)
    with open(args.input, "r", encoding="utf-8") as fh:
        cards = json.load(fh)

    print(f"Loaded {len(cards)} cards; preparing texts...")
    texts = []
    metadata = []
    for c in cards:
        txt = build_text(c)
        texts.append(txt if txt else "")
        # keep minimal metadata to map back to card later
        metadata.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "set": c.get("set"),
            "collector_number": c.get("collector_number"),
        })

    print("Loading model:", args.model)
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as e:
        raise SystemExit("Please install sentence-transformers (pip install sentence-transformers). Error: " + str(e))

    model = SentenceTransformer(args.model)
    bs = args.batch_size

    print("Encoding texts in batches (batch size = {})...".format(bs))
    embeddings = model.encode(
        texts,
        batch_size=bs,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=False,
    )

    if args.dtype == "float32":
        embeddings = embeddings.astype(np.float32)
    else:
        embeddings = embeddings.astype(np.float16)

    emb_path = os.path.join(args.out_dir, "embeddings.npy")
    meta_path = os.path.join(args.out_dir, "cards_metadata.json")

    print("Saving embeddings ->", emb_path)
    np.save(emb_path, embeddings)

    print("Saving metadata ->", meta_path)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=False)

    print("Done. Embeddings shape:", embeddings.shape)

if __name__ == "__main__":
    main()