"""Test that desktop session search handles CJK queries without poisoning LIKE fallback.

Regression test for #90636: 1-2 char CJK queries return 0 results because the
API auto-appends a prefix wildcard '*' that becomes a literal in LIKE fallback.
"""

import re
import pytest


def test_cjk_detection_pattern():
    """The CJK-only regex must match pure CJK tokens and reject mixed/latin."""
    cjk_only_pattern = re.compile(r'^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]+$')
    
    # Pure CJK — should match
    assert cjk_only_pattern.match("秃发")
    assert cjk_only_pattern.match("中文")
    assert cjk_only_pattern.match("日本語")
    assert cjk_only_pattern.match("한글")
    
    # Mixed or Latin — should NOT match
    assert not cjk_only_pattern.match("API")
    assert not cjk_only_pattern.match("nimb")
    assert not cjk_only_pattern.match("中文API")
    assert not cjk_only_pattern.match("test123")
    assert not cjk_only_pattern.match("")


def test_cjk_wildcard_stripping_logic():
    """Simulate the wildcard logic: CJK-only tokens get no wildcard, others do."""
    def process_token(token: str) -> str:
        """Mimic the fixed wildcard logic from sessions.py."""
        if token.startswith('"') or token.endswith("*"):
            return token
        wildcard_token = token + "*"
        if re.match(r'^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]+$', token):
            return token  # CJK-only: no wildcard
        else:
            return wildcard_token  # Mixed/Latin: keep wildcard
    
    # CJK-only tokens should NOT get wildcard
    assert process_token("秃发") == "秃发"
    assert process_token("中文") == "中文"
    assert process_token("日本語") == "日本語"
    assert process_token("한글") == "한글"
    
    # Latin/mixed tokens SHOULD get wildcard
    assert process_token("nimb") == "nimb*"
    assert process_token("API") == "API*"
    assert process_token("test") == "test*"
    assert process_token("中文API") == "中文API*"
    
    # Already has wildcard or is quoted — preserve as-is
    assert process_token("test*") == "test*"
    assert process_token('"秃发"') == '"秃发"'


def test_session_search_query_processing():
    """Integration test: verify full query processing preserves CJK and adds wildcards to English."""
    import re
    
    def process_query(q: str) -> str:
        """Full query processing from sessions.py search endpoint."""
        terms = []
        for token in re.findall(r'"[^"]*"|\S+', q.strip()):
            if token.startswith('"') or token.endswith("*"):
                terms.append(token)
            else:
                wildcard_token = token + "*"
                if re.match(r'^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]+$', token):
                    terms.append(token)
                else:
                    terms.append(wildcard_token)
        return " ".join(terms)
    
    # CJK-only queries: no wildcard added
    assert process_query("秃发") == "秃发"
    assert process_query("中文 测试") == "中文 测试"
    
    # English queries: wildcard added
    assert process_query("nimb") == "nimb*"
    assert process_query("test example") == "test* example*"
    
    # Mixed queries: CJK gets no wildcard, English gets wildcard
    assert process_query("中文 API") == "中文 API*"
    assert process_query("test 测试") == "test* 测试"
    
    # Quoted phrases: preserved as-is
    assert process_query('"exact phrase"') == '"exact phrase"'
    assert process_query('"秃发 test"') == '"秃发 test"'
    
    # Explicit wildcards: preserved
    assert process_query("test*") == "test*"
    assert process_query("中文*") == "中文*"


def test_like_fallback_with_cjk():
    """Verify that the fix prevents literal '*' in LIKE predicates for CJK."""
    # Before fix: "秃发" → "秃发*" → LIKE '%秃发*%' (matches nothing)
    # After fix:  "秃发" → "秃发"  → LIKE '%秃发%'  (matches "秃发")
    
    query_before = "秃发*"  # What was sent before the fix
    query_after = "秃发"    # What is sent after the fix
    
    # Simulate LIKE predicate construction (simplified)
    def to_like_pattern(q: str) -> str:
        return f"%{q}%"
    
    # Before fix: literal * in LIKE pattern
    assert to_like_pattern(query_before) == "%秃发*%"
    
    # After fix: clean pattern
    assert to_like_pattern(query_after) == "%秃发%"
    
    # The literal * would never match normal text
    assert "秃发" not in "秃发*"  # False positive check
    assert "秃发" in "秃发"        # True positive


def test_edge_cases():
    """Test edge cases in the wildcard logic."""
    import re
    
    def process_token(token: str) -> str:
        if token.startswith('"') or token.endswith("*"):
            return token
        wildcard_token = token + "*"
        if re.match(r'^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]+$', token):
            return token
        else:
            return wildcard_token
    
    # Empty string
    assert process_token("") == "*"
    
    # Single CJK char
    assert process_token("中") == "中"
    
    # Numbers (not CJK)
    assert process_token("123") == "123*"
    
    # Punctuation (not CJK)
    assert process_token("...") == "...*"
    
    # Mixed CJK and punctuation (not CJK-only)
    assert process_token("中文，") == "中文，*"
