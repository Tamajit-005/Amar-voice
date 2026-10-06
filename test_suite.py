import os
import tempfile
from backend.main import sanitize_identifier, get_engine

def test_sanitize_identifier():
    assert sanitize_identifier("grandpa") == "grandpa"
    assert sanitize_identifier("../../evil/path") == "evilpath"
    assert sanitize_identifier("..") == "default"
    assert sanitize_identifier("test-123_abc") == "test-123_abc"
    assert sanitize_identifier("") == "default"

def test_engine_synthesize_c1_and_c2():
    if not os.environ.get("ELEVENLABS_API_KEY", "").strip():
        print("⏭️  Skipping live synthesis (ELEVENLABS_API_KEY not set, no quota burned).")
        return
    engine = get_engine()
    
    # 1. Verify C1 fix: calling synthesize with default speaker does not trigger ambiguous tensor boolean error
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
        out_path = tmp_out.name
        
    try:
        # 2. Verify C2 fix: long sentence with multiple clauses/punctuation
        long_text = "I need some water please. Also please call my family member. The weather is getting cooler and I want some warm tea."
        res = engine.synthesize(text=long_text, lang="en", speaker="default", out_path=out_path)
        assert os.path.exists(res), "Synthesized file must exist"
        assert os.path.getsize(res) > 10000, "Synthesized audio must not be empty or truncated"
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)

if __name__ == "__main__":
    test_sanitize_identifier()
    test_engine_synthesize_c1_and_c2()
    print("✅ All unit tests passed!")
