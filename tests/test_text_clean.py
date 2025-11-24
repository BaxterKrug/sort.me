from app.services import text_clean


def test_normalize_ocr_text_strips_noise_and_duplicates():
    raw = "\n".join(
        [
            "  “Rákshasa -- Debaser”  ",
            "Rákshasa -- Debaser",
            "Rákshasa -- Debaser",
            "   |Collector 123/456   ",
        ]
    )

    cleaned = text_clean.normalize_ocr_text(raw, max_lines=3)

    assert "rakshasa" in cleaned.lower()
    assert "--" not in cleaned
    assert cleaned.count("Debaser") == 1


def test_normalize_collector_keeps_digits_and_slash():
    assert text_clean.normalize_collector("  12 B / 345a ") == "12/345"


def test_normalize_ocr_map_handles_regions():
    raw_map = {
        "name": "  Æther Gust  ",
        "collector": "  01 2/ 345  ",
    }

    normalized = text_clean.normalize_ocr_map(raw_map)

    assert normalized["name"] == "Aether Gust"
    assert normalized["collector"] == "012/345"


def test_normalize_ocr_text_filters_gibberish_tokens():
    raw = "Reprieve sc X Instant WU Knave LTR o EN Colasinin DUR TG"

    cleaned = text_clean.normalize_ocr_text(raw)

    assert "Reprieve" in cleaned
    assert "Knave" in cleaned
    assert "LTR" not in cleaned
    assert "DUR" not in cleaned
