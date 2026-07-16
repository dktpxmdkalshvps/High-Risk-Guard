"""REQ-001 verification: run N "obfuscated high-risk commit" cases through
the real detector (mcp_server.detector.scan_change) and report exactly how
many were caught -- "N건 중 M건", no rounding to a percentage, no framing
as a pass/fail rate. Some of these cases are *expected* to slip through;
that's the documented limitation of static keyword/path matching (see
skills/high-risk-guard/SKILL.md and README.md), not a bug in this script.

Run from src/: `python scripts/verify_req001_detection.py`
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.detector import scan_change  # noqa: E402

# (label, file_path, content, obfuscation technique used)
CASES = [
    (
        "리터럴 transfer, 무관 경로",
        "src/services/order.py",
        "def transfer(from_acc, to_acc, amount):\n    pass\n",
        "obfuscation 없음 (대조군)",
    ),
    (
        "변수명 치환 + 무관 경로",
        "src/utils/money_ops.py",
        "def mv_funds(src_acct, dst_acct, amt):\n"
        "    src_acct.qty -= amt\n    dst_acct.qty += amt\n",
        "동의어 치환 (transfer -> mv_funds)",
    ),
    (
        "변수명 치환 + wallet 경로",
        "src/wallet/money_ops.py",
        "def mv_funds(src_acct, dst_acct, amt):\n"
        "    src_acct.qty -= amt\n    dst_acct.qty += amt\n",
        "동의어 치환이지만 경로 기반으로는 여전히 탐지 가능",
    ),
    (
        "camelCase로 이어붙인 키워드",
        "src/services/checkout.py",
        "def transferFundsToVault(amount):\n    pass\n",
        "camelCase 접합 (word-boundary 우회)",
    ),
    (
        "문자열 분할 조합 후 getattr 간접 호출",
        "src/services/checkout.py",
        "op = 'trans' + 'fer'\ngetattr(account, op)(amount)\n",
        "문자열 분할 + 간접 호출",
    ),
    (
        "동형이의 유니코드 문자 치환",
        "src/services/checkout.py",
        "def trа_nsfer(amount):\n    pass\n",  # contains a Cyrillic 'а' (U+0430)
        "유니코드 동형이의자(homoglyph) 치환",
    ),
    (
        "base64 인코딩된 코드 실행",
        "src/services/checkout.py",
        "import base64\nexec(base64.b64decode('dHJhbnNmZXIoYSwgYik='))\n",
        "base64 인코딩 후 동적 실행",
    ),
    (
        "'production'은 'prod' 경계 불일치로 통과",
        "src/ops/release.py",
        "def push_live(env='production'):\n    pass\n",
        "동의어(push_live) + 'production'은 word-boundary상 'prod'와 불일치",
    ),
    (
        "잔액 로직을 회계 용어로 치환",
        "src/ledger/entries.py",
        "def adjust(account, delta):\n    account.qty += delta\n    ledger.record(account)\n",
        "잔액/이체 관련 동의어 전면 치환",
    ),
    (
        "상수에만 키워드 리터럴이 남은 불완전한 위장",
        "src/ops/release.py",
        "CMD = 'deploy'\nrun(CMD + '_to_' + 'prod')\n",
        "간접화 시도했지만 리터럴 'deploy' 문자열이 소스에 그대로 남음",
    ),
]


def main() -> int:
    detected = 0
    print(f"{'#':<3} {'탐지':<6} {'케이스':<32} {'기법'}")
    print("-" * 100)
    for i, (label, file_path, content, technique) in enumerate(CASES, 1):
        result = scan_change(file_path, content)
        mark = "탐지" if result.is_high_risk else "미탐지"
        if result.is_high_risk:
            detected += 1
        print(f"{i:<3} {mark:<6} {label:<32} {technique}")
        if result.is_high_risk:
            print(f"    -> matched_keywords={result.matched_keywords} matched_paths={result.matched_paths}")

    print("-" * 100)
    print(f"결과: 난독화된 고위험 커밋 {len(CASES)}건 중 {detected}건 탐지됨 "
          f"(REQ-001은 정적 키워드/경로 매칭이며 AST 분석이 아니므로, "
          f"일부 케이스가 통과하는 것은 알려진 한계입니다.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
