"""REQ-001: rule-based high-risk action detection.

Static keyword/path matching only. This is deliberately NOT an AST-based
analyzer — bypassable by variable renaming or synonym substitution. That
limitation is documented in the SRS ("미해결 한계") rather than hidden, and
is exercised by test_bypass_via_variable_renaming_not_detected below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Base list taken directly from the SRS (REQ-001): transfer, balance,
# deploy, prod. A few closely-related finance/deploy synonyms are added
# since the SRS explicitly says "등" (etc.) — kept short and reviewable.
DEFAULT_KEYWORDS = [
    "transfer",
    "balance",
    "deploy",
    "prod",
    "payment",
    "withdraw",
    "wallet",
]

# Base list taken directly from the SRS (REQ-001).
DEFAULT_PATHS = [
    "/src/wallet/",
    "/src/config/",
]


@dataclass
class DetectionResult:
    is_high_risk: bool
    matched_keywords: list[str] = field(default_factory=list)
    matched_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "is_high_risk": self.is_high_risk,
            "matched_keywords": self.matched_keywords,
            "matched_paths": self.matched_paths,
        }


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lower().strip("/")


def scan_change(
    file_path: str,
    content: str,
    *,
    keywords: list[str] | None = None,
    paths: list[str] | None = None,
) -> DetectionResult:
    """Detect REQ-001 high-risk signals via static keyword/path matching.

    - Keywords: case-insensitive, word-boundary search over `content`.
    - Paths: substring match of configured high-risk path fragments
      against the normalized `file_path` (leading-slash/backslash agnostic).
    """
    keywords = DEFAULT_KEYWORDS if keywords is None else keywords
    paths = DEFAULT_PATHS if paths is None else paths

    content = content or ""
    # Boundary is "not preceded/followed by [A-Za-z0-9]" rather than regex
    # \b, so snake_case identifiers like DEPLOY_to_PROD or transfer_funds
    # still count "deploy"/"transfer" as a whole word (underscore counts as
    # a separator, not part of the word). This is intentionally NOT
    # camelCase-aware: treating an upper-case transition (transferAmount)
    # as a boundary too was measured against 8 unrelated real-world-style
    # identifiers (transferAmount, balanceIcon, deployButtonLabel, ...) and
    # flagged 8/8 of them, since e.g. "transferAmount" (money) and
    # "transferAmount" (UI pixel transfer) are lexically identical. See
    # mcp_server/tests/test_detector.py::test_unrelated_camelcase_identifiers_not_flagged.
    matched_keywords = [
        kw for kw in keywords
        if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])",
            content,
            re.IGNORECASE,
        )
    ]

    norm_path = _normalize_path(file_path or "")
    matched_paths = [
        p for p in paths
        if _normalize_path(p) and _normalize_path(p) in norm_path
    ]

    is_high_risk = bool(matched_keywords or matched_paths)
    return DetectionResult(is_high_risk, matched_keywords, matched_paths)
