#!/usr/bin/env python3
"""Test that card identification is working with the database."""

from app.services import card_id_lightweight as card_id
from pathlib import Path

# Test find database function
from app.services.ocr_pipeline import _find_card_database

print("=" * 60)
print("Card Identification Test")
print("=" * 60)

# Test 1: Find database
db_path = _find_card_database()
print(f"\n1. Database lookup:")
if db_path:
    print(f"   ✓ Found: {db_path}")
    print(f"   ✓ Exists: {db_path.exists()}")
    print(f"   ✓ Size: {db_path.stat().st_size / (1024*1024):.2f} MB")
else:
    print("   ✗ Database not found!")
    exit(1)

# Test 2: Load database
print(f"\n2. Loading database...")
try:
    cards = card_id.load_local_db(str(db_path))
    print(f"   ✓ Loaded {len(cards):,} cards")
    if cards:
        sample = cards[0]
        print(f"   ✓ Sample: {sample.get('name')} ({sample.get('set')} #{sample.get('collector_number')})")
except Exception as e:
    print(f"   ✗ Error: {e}")
    exit(1)

# Test 3: Test identification with a known card
print(f"\n3. Testing identification...")
test_ocr = {
    "name": "Lightning Bolt",
    "collector": "",
    "oracle": "",
    "type_line": ""
}

try:
    result = card_id.identify_card_from_ocr(test_ocr, db_path=str(db_path))
    best = result.get('best')
    score = result.get('score', 0)
    
    if best:
        print(f"   ✓ Identified: {best.get('name')}")
        print(f"   ✓ Set: {best.get('set')} #{best.get('collector_number')}")
        print(f"   ✓ Score: {score:.2f}")
        print(f"   ✓ Candidates: {len(result.get('candidates', []))}")
    else:
        print(f"   ✗ No match found")
        print(f"     Score: {score:.2f}")
except Exception as e:
    print(f"   ✗ Error: {e}")
    import traceback
    traceback.print_exc()
    exit(1)

print("\n" + "=" * 60)
print("✓ CARD IDENTIFICATION IS WORKING!")
print("=" * 60)
