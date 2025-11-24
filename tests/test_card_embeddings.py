import json
import numpy as np
from sklearn.neighbors import NearestNeighbors

from app.services import card_id


def test_embedding_matches_handles_empty_query():
    info = card_id.embedding_matches_from_ocr({}, "data/embeddings")
    assert info["matches"] == []
    assert info["error"] == "empty_query"


def test_embedding_matches_returns_best_when_cache_available(monkeypatch):
    embeddings = np.array([[0.0, 0.0], [0.5, 0.5]], dtype=np.float32)
    cache = {
        "embeddings": embeddings,
        "meta": [
            {"name": "Alpha", "set": "abc", "collector_number": "1"},
            {"name": "Beta", "set": "def", "collector_number": "2"},
        ],
        "model_name": "dummy-model",
        "encoder_failed": False,
    }
    nn = NearestNeighbors(n_neighbors=2, algorithm="auto")
    nn.fit(embeddings)
    cache["nn"] = nn

    monkeypatch.setattr(card_id, "_get_embedding_cache", lambda _dir: cache)

    class DummyEncoder:
        def encode(self, texts, convert_to_numpy=True):
            return np.array([[0.0, 0.0]], dtype=np.float32)

    def fake_encoder_loader(cache_obj):
        cache_obj["encoder"] = DummyEncoder()
        return cache_obj["encoder"]

    monkeypatch.setattr(card_id, "_ensure_sentence_encoder", fake_encoder_loader)

    info = card_id.embedding_matches_from_ocr({"name": "Alpha"}, "ignored", max_results=1)
    assert info["best"]["card"]["name"] == "Alpha"
    assert info["best"]["score"] > 0
    assert info["engine"] == "dummy-model"


def test_get_embedding_cache_builds_sentence_embeddings_when_missing(tmp_path, monkeypatch):
    embeddings_dir = tmp_path / "emb"
    embeddings_dir.mkdir()
    meta_path = embeddings_dir / "cards_metadata.json"
    with meta_path.open("w", encoding="utf8") as fh:
        json.dump(
            [
                {"id": "alpha-1", "name": "Alpha", "set": "abc"},
                {"id": "beta-2", "name": "Beta", "set": "def"},
            ],
            fh,
        )

    class DummyEncoder:
        def encode(self, texts, batch_size=32, show_progress_bar=False, convert_to_numpy=True):
            data = []
            for idx, _ in enumerate(texts):
                data.append([float(idx), float(idx + 1)])
            return np.asarray(data, dtype=np.float32)

    def fake_ensure(cache_obj):
        cache_obj["encoder"] = DummyEncoder()
        return cache_obj["encoder"]

    monkeypatch.setattr(card_id, "_ensure_sentence_encoder", fake_ensure)
    monkeypatch.setenv("SORT_CARD_EMBED_RUNTIME_BUILD", "1")

    cache = card_id._get_embedding_cache(str(embeddings_dir))
    assert cache is not None
    assert cache["ready"] is True
    assert cache["distance_metric"] == "cosine"
    assert cache["embeddings"].shape == (2, 2)
    assert cache["meta"][0]["scryfall_id"] == "alpha-1"
    assert cache["encoder"] is not None
    assert (embeddings_dir / "embeddings.npy").exists()


def test_get_embedding_cache_skips_sentence_build_without_flag(tmp_path, monkeypatch):
    embeddings_dir = tmp_path / "emb"
    embeddings_dir.mkdir()
    meta_path = embeddings_dir / "cards_metadata.json"
    with meta_path.open("w", encoding="utf8") as fh:
        json.dump([
            {"id": "alpha-1", "name": "Alpha"},
        ], fh)

    def fail_build(*_args, **_kwargs):
        raise AssertionError("_build_sentence_cache should not run when disabled")

    sentinel = {"ready": True, "sentinel": True}

    monkeypatch.setenv("SORT_CARD_EMBED_RUNTIME_BUILD", "0")
    monkeypatch.setattr(card_id, "_build_sentence_cache", fail_build)
    monkeypatch.setattr(card_id, "_build_tfidf_cache", lambda _dir, _meta: sentinel)

    cache = card_id._get_embedding_cache(str(embeddings_dir))
    assert cache is sentinel


def test_get_embedding_cache_prefers_inline_embeddings(tmp_path):
    embeddings_dir = tmp_path / "emb"
    embeddings_dir.mkdir()
    meta_path = embeddings_dir / "cards_metadata.json"
    with meta_path.open("w", encoding="utf8") as fh:
        json.dump(
            [
                {"id": "alpha-1", "name": "Alpha", "embedding": [0.0, 1.0]},
                {"id": "beta-2", "name": "Beta", "embedding": [1.0, 0.0]},
            ],
            fh,
        )

    cache = card_id._get_embedding_cache(str(embeddings_dir))
    assert cache is not None
    assert cache["ready"] is True
    assert cache["distance_metric"] == "cosine"
    assert cache["embeddings"].shape == (2, 2)
    assert cache["meta"][0]["scryfall_id"] == "alpha-1"
