"""
Test the lightweight card identification system.
"""
import json
from app.services import card_id_lightweight as card_id

def test_exact_match():
    """Test exact name matching."""
    cards = [
        {"name": "Lightning Bolt", "set": "LEA", "collector_number": "162"},
        {"name": "Black Lotus", "set": "LEA", "collector_number": "232"},
        {"name": "Counterspell", "set": "LEA", "collector_number": "54"},
    ]
    
    ocr_map = {"name": "Lightning Bolt"}
    result = card_id.identify_card_from_ocr(ocr_map, cards_list=cards)
    
    assert result['best'] is not None
    assert result['best']['name'] == "Lightning Bolt"
    assert result['score'] == 100.0
    print("✓ Exact match test passed")


def test_collector_match():
    """Test collector number matching."""
    cards = [
        {"name": "Lightning Bolt", "set": "M11", "collector_number": "146"},
        {"name": "Lightning Bolt", "set": "M10", "collector_number": "146"},
        {"name": "Lightning Bolt", "set": "LEA", "collector_number": "162"},
    ]
    
    ocr_map = {"name": "Lightninq Bolt", "collector": "162"}  # typo in name
    result = card_id.identify_card_from_ocr(ocr_map, cards_list=cards)
    
    assert result['best'] is not None
    assert result['best']['collector_number'] == "162"
    print("✓ Collector match test passed")


def test_fuzzy_match():
    """Test fuzzy name matching."""
    cards = [
        {"name": "Lightning Bolt", "set": "LEA", "collector_number": "162"},
        {"name": "Lightning Strike", "set": "M19", "collector_number": "152"},
        {"name": "Shock", "set": "M19", "collector_number": "156"},
    ]
    
    ocr_map = {"name": "Lightninq Bolt"}  # OCR typo: 'g' -> 'q'
    result = card_id.identify_card_from_ocr(ocr_map, cards_list=cards)
    
    assert result['best'] is not None
    assert result['best']['name'] == "Lightning Bolt"
    assert result['score'] > 50  # Should still score reasonably despite typo
    print(f"✓ Fuzzy match test passed (score: {result['score']:.2f})")


def test_oracle_text_scoring():
    """Test oracle text helps distinguish similar cards."""
    cards = [
        {
            "name": "Lightning Bolt",
            "oracle_text": "Lightning Bolt deals 3 damage to any target.",
            "set": "LEA",
            "collector_number": "162"
        },
        {
            "name": "Lightning Strike",
            "oracle_text": "Lightning Strike deals 3 damage to any target.",
            "set": "M19",
            "collector_number": "152"
        },
    ]
    
    ocr_map = {
        "name": "Lightning",  # Ambiguous
        "oracle": "Lightning Bolt deals 3 damage"
    }
    result = card_id.identify_card_from_ocr(ocr_map, cards_list=cards)
    
    assert result['best'] is not None
    # Should prefer Lightning Bolt due to oracle text match
    assert "Bolt" in result['best']['name']
    print("✓ Oracle text scoring test passed")


def test_empty_database():
    """Test handling of empty database."""
    cards = []
    ocr_map = {"name": "Lightning Bolt"}
    
    result = card_id.identify_card_from_ocr(ocr_map, cards_list=cards)
    
    assert result['best'] is None
    assert result['score'] == 0.0
    assert len(result['candidates']) == 0
    print("✓ Empty database test passed")


def test_no_match():
    """Test when no good match exists."""
    cards = [
        {"name": "Island", "set": "LEA", "collector_number": "1"},
        {"name": "Mountain", "set": "LEA", "collector_number": "2"},
    ]
    
    ocr_map = {"name": "XYZABC123"}  # Gibberish
    result = card_id.identify_card_from_ocr(ocr_map, cards_list=cards)
    
    # Should still return candidates but with low scores
    assert result['score'] < 50  # Low confidence
    print("✓ No match test passed")


def test_with_real_database():
    """Test with the actual Scryfall database if available."""
    import os
    db_path = "data/oracle-cards-20251112100306.json"
    
    if not os.path.exists(db_path):
        print("⊘ Skipping real database test - file not found")
        return
    
    ocr_map = {
        "name": "Lightning Bolt",
        "oracle": "deals 3 damage to any target",
        "set": "LEA"
    }
    
    result = card_id.identify_card_from_ocr(ocr_map, db_path=db_path)
    
    assert result['best'] is not None
    assert "bolt" in result['best']['name'].lower()
    print(f"✓ Real database test passed - found: {result['best']['name']}")
    print(f"  Score: {result['score']:.2f}")
    print(f"  Candidates: {len(result['candidates'])}")


def test_multiface_card():
    """Test double-faced card matching."""
    cards = [
        {
            "name": "Delver of Secrets // Insectile Aberration",
            "oracle_text": "At the beginning of your upkeep, look at the top card...",
            "set": "ISD",
            "collector_number": "51"
        }
    ]
    
    ocr_map = {"name": "Delver of Secrets"}
    result = card_id.identify_card_from_ocr(ocr_map, cards_list=cards)
    
    assert result['best'] is not None
    assert "Delver of Secrets" in result['best']['name']
    print("✓ Multiface card test passed")


if __name__ == "__main__":
    print("Testing lightweight card identification...")
    print()
    
    test_exact_match()
    test_collector_match()
    test_fuzzy_match()
    test_oracle_text_scoring()
    test_empty_database()
    test_no_match()
    test_multiface_card()
    test_with_real_database()
    
    print()
    print("All tests passed! ✓")
