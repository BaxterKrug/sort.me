from pathlib import Path

from scripts import embed_single_card
from app.services import card_id


def test_infer_model_name_prefers_override(tmp_path):
    metadata_path = tmp_path / "cards_metadata.json"
    metadata_path.write_text("[]", encoding="utf8")
    meta_info_path = metadata_path.parent / "embeddings.meta.json"
    meta_info_path.write_text('{"model_name": "custom-model"}', encoding="utf8")

    resolved = embed_single_card._infer_model_name(metadata_path, "override-model")

    assert resolved == "override-model"


def test_infer_model_name_reads_meta_file(tmp_path):
    metadata_path = tmp_path / "cards_metadata.json"
    metadata_path.write_text("[]", encoding="utf8")
    meta_info_path = metadata_path.parent / "embeddings.meta.json"
    meta_info_path.write_text('{"model_name": "meta-model"}', encoding="utf8")

    resolved = embed_single_card._infer_model_name(metadata_path, None)

    assert resolved == "meta-model"


def test_infer_model_name_falls_back_to_default(tmp_path):
    metadata_path = tmp_path / "cards_metadata.json"
    metadata_path.write_text("[]", encoding="utf8")

    resolved = embed_single_card._infer_model_name(metadata_path, None)

    assert resolved == card_id.DEFAULT_EMBED_MODEL
