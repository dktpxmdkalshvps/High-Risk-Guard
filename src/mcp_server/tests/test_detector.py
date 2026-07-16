"""Unit tests for REQ-001 rule-based high-risk detection (mcp_server.detector).

Includes both normal detection cases and a known-bypass case that documents
the limitation stated in the SRS (keyword/path matching is not an AST
analyzer and can be evaded by renaming).
"""
import pytest

from mcp_server.detector import scan_change


def test_keyword_detects_transfer_function():
    result = scan_change(
        "src/utils/helpers.py",
        "def transfer(from_acc, to_acc, amount):\n    pass\n",
    )
    assert result.is_high_risk is True
    assert "transfer" in result.matched_keywords


def test_path_detects_wallet_directory():
    result = scan_change(
        "src/wallet/balance_store.py",
        "class Store:\n    pass\n",
    )
    assert result.is_high_risk is True
    assert "/src/wallet/" in result.matched_paths


def test_benign_code_not_flagged():
    result = scan_change(
        "src/utils/formatter.py",
        "def format_name(first, last):\n    return f'{first} {last}'\n",
    )
    assert result.is_high_risk is False
    assert result.matched_keywords == []
    assert result.matched_paths == []


def test_case_insensitive_keyword_match():
    result = scan_change(
        "src/ops/release.py",
        "def DEPLOY_to_PROD():\n    pass\n",
    )
    assert result.is_high_risk is True
    assert "deploy" in result.matched_keywords
    assert "prod" in result.matched_keywords


def test_bypass_via_variable_renaming_not_detected():
    """Documents the known REQ-001 limitation: renaming/synonyms evade
    static keyword matching. This is expected current behavior, not a bug
    — see SRS section 4, "미해결 한계"."""
    result = scan_change(
        "src/utils/money_ops.py",
        "def mv_funds(src_acct, dst_acct, amt):\n"
        "    src_acct.qty -= amt\n"
        "    dst_acct.qty += amt\n",
    )
    assert result.is_high_risk is False


def test_camelcase_unrelated_identifier_not_flagged():
    """'wallet' as a camelCase prefix glued to an unrelated word (no
    underscore/space) is not flagged, since the current boundary check
    treats any adjacent alnum char as "still the same word"."""
    result = scan_change(
        "src/ui/theme.py",
        'walletColor = "#FF0000"\n',
    )
    assert result.is_high_risk is False
    assert result.matched_keywords == []


def test_snake_case_unrelated_identifier_flagged_as_false_positive():
    """KNOWN FALSE POSITIVE: 'deploy_notes' has nothing to do with an
    actual deploy action (it's release-note text), but the underscore
    counts as a word boundary, so keyword matching still fires. This is
    an accepted trade-off of REQ-001's plain substring/word matching
    (see SKILL.md/SRS limitations) — flagged here explicitly rather than
    hidden, per REQ-001 being a coarse, over-inclusive filter by design."""
    result = scan_change(
        "src/ops/release.py",
        'deploy_notes = "this release improves startup time"\n',
    )
    assert result.is_high_risk is True
    assert "deploy" in result.matched_keywords


def test_camelcase_keyword_glued_to_next_word_not_detected():
    """KNOWN GAP: unlike the underscore case above, a keyword glued
    directly to a following word in camelCase (no separator) is NOT
    detected, because the char after "transfer" ('F') is alnum and the
    current boundary check requires a non-alnum char there. This is a
    real bypass surface distinct from the renaming bypass documented in
    test_bypass_via_variable_renaming_not_detected, and is reported here
    rather than silently left uncovered."""
    result = scan_change(
        "src/utils/ops.py",
        "def transferFundsToVault(amount):\n    pass\n",
    )
    assert result.is_high_risk is False
    assert result.matched_keywords == []  # keyword itself is NOT caught


# REJECTED ENHANCEMENT: treating a camelCase upper-case transition (e.g.
# "transfer|Amount") as an additional word boundary was prototyped to catch
# test_camelcase_keyword_glued_to_next_word_not_detected above. Measured
# against 8 realistic-but-unrelated camelCase identifiers, it flagged 8/8
# (100%) of them, because "transferAmount" (money) and "transferAmount"
# (UI slide-transfer pixel amount) are lexically identical — a boundary-only
# check cannot tell them apart. Reverted; keyword detection stays
# underscore/non-alnum-boundary only. These cases pin down that decision so
# it isn't silently re-attempted without re-measuring the trade-off.
UNRELATED_CAMELCASE_IDENTIFIERS = [
    ("transferAmount", "transferAmount = calcSlideTransferPx(px)"),
    ("transferListItem", "transferListItem = sourceList.pop()"),
    ("balanceIcon", "balanceIcon = Icon('scale-glyph')"),
    ("balanceCheckAnimation", "balanceCheckAnimation = Animation('wobble')"),
    ("deployButtonLabel", "deployButtonLabel = 'Go'"),
    ("deployStatusBadge", "deployStatusBadge = Badge(color='green')"),
    ("prodConfig", "prodConfig = loadTheme('product-page')"),
    ("prodBannerColor", "prodBannerColor = '#123456'"),
]


@pytest.mark.parametrize("name,content", UNRELATED_CAMELCASE_IDENTIFIERS)
def test_unrelated_camelcase_identifiers_not_flagged(name, content):
    result = scan_change("src/ui/widgets.py", content)
    assert result.is_high_risk is False, (
        f"{name}: camelCase-boundary detection would have been a false "
        f"positive here (measured 8/8 during evaluation) — see comment above"
    )
