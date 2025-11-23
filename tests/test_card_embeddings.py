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
