# 29. 크로스유저 에이전트 연결

## 개요

서로 **다른 유저**의 에이전트끼리, **사람이 승인한 "친구" 관계**에서만 메시지·작업의뢰·프로젝트 핸드오프를 주고받는 시스템.

기존 [11. 에이전트 간 통신(Relay)](./11-relay.md)은 **동일 유저 내부 전용**(`getProjects(user.userId)`로 자기 프로젝트만 대상)이다. 크로스유저는 별도 모델(`/api/relay/external`)로 구현되며, **유저 경계를 넘는 통신은 오직 accepted 연결에서만** 허용된다.

계기: Sean이 만든 프로젝트를 다른 유저(예: 이창호 대표) 계정의 에이전트에게 넘겨 이어서 개발하게 하는 인수인계(핸드오프) 시나리오.

## 핵심 개념

| 개념 | 설명 |
|------|------|
| **핸들** | 유저의 전역 주소. `@{username}` 형식. 내부 프로젝트 ID가 아니라 핸들로 주소를 지정한다. 디렉토리 비노출 — 핸들을 아는 사람만 신청 가능. |
| **연결(친구)** | 두 유저(사람) 사이의 대칭 관계 1행. 상태: `pending` / `accepted` / `blocked`. 사람이 수락/거절/차단. |
| **양방향 수신함** | 각 당사자가 **자기가 받을 프로젝트(inbox)와 스코프**를 독립적으로 정한다. 신청자(requester)와 수락자(addressee)가 각각. |
| **스코프** | 수신 프로젝트, 자동실행(auto_execute), 첨부 허용, 하루 한도(rate_per_day). **받는 사람이 정한다.** |
| **제안(proposal)** | 받는 쪽 `auto_execute=false`일 때, 메시지가 곧바로 실행되지 않고 "제안"으로 도착 → 사람이 승인해야 실행. |

## 데이터 모델

### `agent_connections`

대칭 1행. 신청자/수락자가 각각 자기 수신 스코프를 가진다.

```sql
CREATE TABLE agent_connections (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  requester_id  integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  addressee_id  integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status        text    NOT NULL DEFAULT 'pending',   -- pending|accepted|blocked
  note          text,                                 -- 신청 메모
  -- addressee(수락자)의 수신 스코프
  inbox_project           text,
  allow_attachments       boolean NOT NULL DEFAULT false,
  auto_execute            boolean NOT NULL DEFAULT false,
  rate_per_day            integer NOT NULL DEFAULT 50,
  -- requester(신청자)의 수신 스코프  (마이그레이션 20260618140000)
  requester_inbox_project     text,
  requester_allow_attachments boolean NOT NULL DEFAULT false,
  requester_auto_execute      boolean NOT NULL DEFAULT false,
  requester_rate_per_day      integer NOT NULL DEFAULT 50,
  created_at    timestamptz NOT NULL DEFAULT now(),
  responded_at  timestamptz,
  CONSTRAINT agent_connections_no_self     CHECK (requester_id <> addressee_id),
  CONSTRAINT agent_connections_unique_pair UNIQUE (requester_id, addressee_id)
);
```

> **정수 user_id 체계**: 이 플랫폼은 자체 `users(id integer)`를 쓴다. 초기 설계 문서의 `uuid auth.users(id)`가 아니라 **정수 FK**로 통일되어 있다.

> **방향별 스코프 매핑**: 어떤 컬럼을 쓰는지는 "받는 사람"이 신청자냐 수락자냐로 결정된다.
> - 신청자→수락자 발신 → 수락자의 수신 스코프(`inbox_project` 등) 적용
> - 수락자→신청자 발신 → 신청자의 수신 스코프(`requester_*`) 적용

### `agent_messages_external`

크로스유저 메시지 감사 로그(추적/레이트리밋/차단 근거).

```sql
CREATE TABLE agent_messages_external (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  connection_id bigint REFERENCES agent_connections(id) ON DELETE SET NULL,
  from_user_id  integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  to_user_id    integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  from_project  text,   -- 발신 프로젝트(에이전트)
  to_project    text,   -- 실제 전달된 수신 프로젝트(inbox)
  kind          text NOT NULL DEFAULT 'message',  -- message|handoff|file
  proposal      boolean NOT NULL DEFAULT false,
  message_id    bigint,
  preview       text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
```

마이그레이션: `20260618120000_agent_connections.sql`(테이블), `20260618140000_agent_connections_requester_inbox.sql`(requester_* 컬럼).

## API

### 연결 관리

```
GET  /api/connections
  → { incoming[], outgoing[], accepted[], blocked[] }
  각 항목에 내 방향(direction), 상대 핸들(peerHandle),
  내가 통제하는 "내 수신 스코프"(myInboxProject/myAutoExecute/...) 포함.

POST /api/connections/request   { to_handle, note }
  핸들로 친구 신청. 에러: 형식오류400 / 없는유저404 / 자기자신400 /
  이미존재409 / 차단됨403(일반화 메시지).

POST /api/connections/:id/respond
  { action: 'accept'|'reject'|'block'|'scope', inbox_project?, auto_execute?, allow_attachments?, rate_per_day? }
  - accept/reject/block: 수신자(addressee)만. accept 시 수락자 수신 스코프 저장.
  - scope: 호출자 본인의 "내 수신 스코프"만 수정 (신청자→requester_*, 수락자→기본 컬럼). 양쪽 다 가능.

DELETE /api/connections/:id
  연결 해제 / 보낸 신청 취소 / 차단 해제. 당사자 누구든.
```

### 외부 메시지 발신/회신

```
POST /api/relay/external
  { to_handle, from_project, text, kind?, attachments? }
  인증: api_key 또는 세션.
  검사: (from,to) accepted 연결 + 발신 프로젝트가 내 것 + 레이트리밋 + 첨부 스코프.
  전달: 받는 사람 기준 inbox_project에 [외부 relay @{from} · {kind}] 태깅하여 주입.
        auto_execute=false면 "제안"(processed=true → 데몬 자동실행 안 함).
  응답: { success, message_id, to_project, proposal, remaining_today }

POST /api/relay/external/respond   { message_id, action: 'approve'|'reject' }
  외부 "제안" 메시지를 사람이 승인/거절.
  approve: 같은 수신함에 처리용 메시지를 새로 주입(데몬 수행) + 원본 metadata.external.status='approved'.
  reject:  원본 status='rejected'.
```

## 발신/수신 흐름

```
[발신측 에이전트]
  POST /api/relay/external {to_handle:@B, from_project:내프로젝트, text}
        ↓ accepted 연결 확인 + 받는 사람(B)의 수신 스코프 해석
  messages 테이블(user_id=B, project=B의 inbox)에 주입
        - subtype = "external"
        - from_user_id / from_username = 발신자
        - metadata.external = { fromHandle, fromProject, connectionId, kind, isProposal, status }
        - 본문 머리: [외부 relay @A · message]  + (제안이면 경고)  + 회신 안내문
        ↓
[수신측]
  auto_execute=true  → processed=false → 데몬이 폴링·수행 (B 에이전트가 바로 처리)
  auto_execute=false → processed=true  → 채팅에 "제안"으로만 표시, 사람 승인 대기
```

회신: 외부 메시지는 `[외부 relay @보낸사람]`으로 도착하며, 답장은 같은 `/api/relay/external`에 `to_handle="@보낸사람"`, `from_project="내 발신 프로젝트"`로 보낸다. **상대 내부 프로젝트 ID는 알 필요 없다.**

## UI 컴포넌트

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| `AgentConnectionsPanel` | 설정 페이지(`/settings`) 카드 섹션 | 받은신청/보낸신청/연결됨/차단 관리, 내 핸들 표시, 내 수신 스코프 칩, "수신 설정 필요" 안내 |
| `SendRequestModal` | 패널 내 모달 | 핸들+메모로 신청, 검증/에러 처리 |
| `ScopeModal` | 수락/스코프 편집 모달 | 수신 프로젝트·자동실행·첨부·하루한도 설정 (수락 시 / "내 수신 설정") |
| `MessageBubble`(확장) | 채팅 | `🌐 외부 · @핸들` violet 배지, 핸드오프 배지, **제안 승인/거절** 액션바, 승인됨/거절됨 상태 |

## 보안 / 남용 방지

- 통신은 **accepted 연결 한정**. 사람이 수락·차단·해제 통제.
- 기본 **권한 최소화**: 수신함 한정, `auto_execute` 기본 off(제안만), 첨부 옵트인.
- **레이트리밋**(받는 사람의 하루 한도) + 감사 로그(`agent_messages_external`) + 차단 시 즉시 단절.
- 발신자 신원은 **서버가 인증으로 채움**(`from_user_id`) — 본문 신뢰 금지(스푸핑 방지).
- 디렉토리 비노출 — 핸들을 직접 아는 사람끼리만 신청.

## 에이전트 프롬프트 규약

에이전트가 크로스유저 메시징을 쓸 수 있도록 **`_common` 프롬프트**(유저별, 모든 프로젝트 주입)에 "크로스유저 에이전트 메시징" 섹션이 포함된다. 핵심: accepted 연결 확인(`GET /api/connections`) → `/api/relay/external`로 핸들 주소 지정 발신 → 외부 메시지 수신 시 핸들로 회신. ([19. 프롬프트 관리](./19-prompts.md) 참조)

## 관련 설계 문서

- `peter-voice-web` `docs/plans/cross-user-agent-connections.md` — 백엔드/데이터모델/API 설계
- `peter-voice-web` `docs/plans/cross-user-agent-connections-ui.md` — UI 설계
