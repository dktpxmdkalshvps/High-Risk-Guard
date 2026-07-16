---
name: high-risk-guard
description: Codex가 자금 이동 로직 수정, 프로덕션 배포 등 고위험 작업을 수행하기 전에 반드시 따라야 하는 물리적 승인 절차. 코드 변경/배포를 커밋·적용하기 직전에 이 스킬을 사용해 high-risk-guard MCP 서버로 위험 여부를 확인해야 한다.
---

# High-Risk Guard

AI 에이전트(Codex)가 사람의 실시간 개입 없이 자금 이동 로직이나 프로덕션 배포처럼
되돌리기 어려운 작업을 수행하지 못하도록 막는 감사(Audit) 절차다.

## 언제 사용하는가

다음 중 하나라도 해당하면, 실제로 파일을 쓰거나 명령을 실행하기 **전에** 이 스킬을 따른다.

- 수정하려는 코드에 자금/잔액/이체/결제/배포 관련 로직이 포함된다.
- 수정하려는 파일 경로가 지갑(wallet) 또는 설정(config) 디렉터리 아래에 있다.
- 위 두 가지가 확실히 아니라고 판단되지 않는 애매한 경우 (판단이 서지 않으면 탐지 도구를 호출해서 확인한다).

## 현재 절차

고위험 여부가 조금이라도 의심되면, 파일을 실제로 쓰거나 명령을 실행하기 **전에**
`high-risk-guard` MCP 서버의 `request_high_risk_approval` 툴을 호출한다. 인자:

- `file_path`: 수정하려는 파일의 경로
- `content`: 적용하려는 코드(새 내용 또는 diff 전체 텍스트)
- `diff_summary` (선택): 승인자에게 보여줄 사람이 읽기 쉬운 변경 요약. 생략하면 `content`를
  잘라서 그대로 보여준다.

이 툴은 내부적으로 REQ-001 탐지를 수행하고, 고위험으로 판단되면 백엔드에 승인 요청을 생성한
뒤 사람이 모바일 화면에서 REQ-002(강제 인지)→REQ-003(물리 제스처) 절차를 마치거나
REQ-006(Fail-Closed 타임아웃/연결 끊김)으로 자동 폐기될 때까지 블로킹 대기한다.

반환값 중 **`approved` 필드만 신뢰**한다:
- `approved: true` → 작업을 계속 진행한다. (`status`가 `not_high_risk`이면 애초에 승인 절차
  자체가 생략된 것이고, `approved`이면 사람이 실제로 물리 제스처를 완료한 것이다.)
- `approved: false` → **절대 진행하지 않는다.** `reason`을 사용자에게 그대로 보고한다
  (예: `no_movement_detected`, `mechanical_pattern_detected`, `timeout_fail_closed`,
  `disconnected_fail_closed`, `abandoned_no_response` 등). 재시도가 필요하면 사용자에게
  승인 절차를 다시 밟아달라고 안내하고, 임의로 재시도하거나 우회하지 않는다.

가벼운 사전 확인(실제 승인 요청을 만들지 않고 REQ-001 결과만 보고 싶을 때)이 필요하면
`scan_high_risk(file_path, content)`를 대신 호출할 수 있다 — 이건 순수 탐지 결과
(`is_high_risk`/`matched_keywords`/`matched_paths`)만 반환하고 사람을 기다리지 않는다.

**전제조건**: 이 도구가 동작하려면 FastAPI 백엔드(`backend/app.py`)가 별도 프로세스로
떠 있어야 한다 (`uvicorn backend.app:app`, 기본 포트 8000). 떠 있지 않으면
`request_high_risk_approval` 호출이 연결 오류로 실패한다 — 이 경우 작업을 진행하지 말고
사용자에게 백엔드를 먼저 기동해달라고 알린다 (Fail-Closed 원칙: 확인할 수 없으면 진행하지
않는다).

## 한계 (정직하게 명시)

- `scan_high_risk`는 사전 정의된 키워드/경로 문자열 매칭일 뿐이다. 변수명을 바꾸거나
  동의어를 쓰는 방식으로 우회될 수 있다. 이는 알려진 한계이며, AST 기반 정적 분석은
  이번 MVP 범위에서 의도적으로 제외되었다.
- 탐지되지 않았다고 해서 해당 작업이 실제로 저위험이라는 보장은 없다.
- 단어 경계 판단은 `_`(언더스코어) 등 비영숫자 문자만 boundary로 인정하고,
  camelCase 대문자 전환(`transferAmount`처럼 키워드 뒤에 대문자가 바로 붙는 경우)은
  boundary로 인정하지 않는다. camelCase 전환도 boundary로 인정하도록 보강을 시도했으나,
  `transferAmount`/`balanceIcon`/`deployButtonLabel` 등 자금·배포와 무관한 실제 있을 법한
  camelCase 식별자 8개로 측정한 결과 8/8(100%)이 새로 오탐되어 되돌렸다
  (`mcp_server/tests/test_detector.py::test_unrelated_camelcase_identifiers_not_flagged` 참조).
- **이 보강 시도 이후에도** 문자열 조합(`"trans" + "fer"`), 간접 호출(`getattr(obj, "transfer")`),
  동의어 치환(`mv_funds`) 같은 우회 경로는 여전히 열려 있으며, 이는 정규식 수준 탐지의
  근본적 한계다. 완전한 해결에는 AST 기반 정적 분석이 필요하지만 이번 MVP 범위에서는
  의도적으로 제외했다.
- REQ-005 감사 로그의 `mock_biometric_verified_at`는 **진짜 생체인증이 아니다.** 실기기
  FaceID/지문 연동이 이번 MVP 범위 밖이라, "승인자가 REQ-002 단계를 통과해 제스처 화면에
  진입한 시각"을 대신 기록해두는 것뿐이다. 실제 신원 확인 증적으로 취급하지 않는다.
- REQ-005 해시 체이닝은 애플리케이션 레이어의 삭제 API가 없다는 것과 사후 위변조를
  탐지 가능하다는 것만 보장한다. 인프라 레벨 WORM 저장소가 아니므로, SQLite 파일에 직접
  접근 가능한 DB 관리자가 로그를 조작하는 것 자체를 막지는 못한다 (다만 조작하면
  `verify_chain()`으로 반드시 감지된다).
- `MCP_POLL_MAX_SECONDS`(기본 120초)는 서버 측 REQ-006 Fail-Closed와는 별개의, MCP 툴
  호출 자체가 무한정 블로킹되지 않도록 하는 클라이언트 측 안전장치다. REQ-002 단계
  (제스처 화면 진입 전)에는 서버 측 타임아웃이 없어서, 승인자가 요청을 아예 열어보지도
  않으면 이 안전장치가 발동한다. 이때 MCP 서버는 **그냥 로컬에서 포기하고 끝내지 않는다** —
  `POST /api/requests/{id}/abandon`을 호출해 백엔드 요청 상태 자체를
  `REJECTED("abandoned_no_response")`로 명시적으로 종결시킨 뒤 `approved: false`를 반환한다.
  이렇게 서버 상태까지 종결시키는 이유: 그렇지 않으면 Codex는 이미 작업을 중단했는데
  승인자가 한참 뒤에(예: 5분 뒤) 뒤늦게 승인을 완료할 경우 REQ-005 감사 로그에는 그
  요청이 "승인됨"으로 남아, 실제로 실행되지 않은 작업이 승인된 것처럼 기록되는
  불일치가 생긴다. `/abandon` 이후에는 그 request_id로 들어오는 어떤 뒤늦은 승인 시도도
  서버가 `request_already_closed` 오류로 거부한다 (상태 전이가 이미 종결되었으므로).
