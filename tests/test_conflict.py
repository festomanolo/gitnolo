"""
Unit tests for GitLens Conflict Resolver in Gitnolo.
"""

import os
import tempfile
from gitnolo.conflict_resolver import ConflictFileParser, ConflictHunk

def test_parse_conflict_file():
    sample_content = """import os
import sys

<<<<<<< HEAD
def calculate_tax(amount: float) -> float:
    # 2026 standard VAT rate
    return amount * 0.18
=======
def calculate_tax(amount: float, rate: float = 0.16) -> float:
    # Customizable tax rate with default 16%
    return amount * rate
>>>>>>> feat/custom-tax-rates

def process_payment(order_id: str, amount: float):
    tax = calculate_tax(amount)
    print(f"Processing payment for {order_id} with tax: {tax}")
"""
    with tempfile.NamedTemporaryFile("w+", suffix=".py", delete=False) as f:
        f.write(sample_content)
        temp_path = f.name

    try:
        hunks = ConflictFileParser.parse_file(temp_path)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert "HEAD" in hunk.ours_label
        assert "return amount * 0.18" in hunk.ours_content
        assert "rate: float = 0.16" in hunk.theirs_content

        # Test applying resolution
        resolved_code = """def calculate_tax(amount: float, rate: float = 0.18) -> float:
    # Merged: Customizable tax rate defaulting to standard 18%
    return amount * rate
"""
        hunk.resolved_content = resolved_code
        success = ConflictFileParser.apply_resolutions(temp_path, [hunk])
        assert success

        with open(temp_path, "r") as f:
            final_content = f.read()

        assert "<<<<<<<" not in final_content
        assert ">>>>>>>" not in final_content
        assert "======= " not in final_content
        assert "amount * rate" in final_content
        print("[OK] Conflict parser & resolution test passed.")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == "__main__":
    test_parse_conflict_file()
