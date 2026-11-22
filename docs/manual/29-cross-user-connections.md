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
| **스코프** | 수신 프로젝트 **또는 수신 브랜치**(`branch:<내부 id>`, 2026-10-01), 자동실행(auto_execute), 첨부 허용, 하루 한도(rate_per_day). **받는 사람이 정한다.** |
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
        inbox_project 가 `branch:<id>` 면 그 브랜치 세션(담당자)이 받는다(내부 relay 의 브랜치 키와 동일). 브랜치가 삭제·비활성이면 409.
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

### 받는 쪽이 답하지 못하고 끝났을 때 — 자동 안내 (2026-11-23)

발신 API 의 `201` 은 「수신함에 넣었다」는 뜻이지 「상대 비서가 처리했다」는 뜻이 아니다. 받는 쪽 데몬이 그 글을 **고정 문구**로 끝내면(아래 표) 그 문구는 받는 사람 채팅에만 남는다. 그래서 데몬이 보낸 핸들에게 안내 한 통을 같은 API 로 돌려보낸다 (실례 11/19: 받는 쪽 클라우드 데몬이 「클로드 로그인이 만료됐어요」로 끝났고 보낸 담당은 40시간 넘게 기다렸다).

```
[외부 relay @받는사람 · message]
[자동 안내 · 회신 불요] 보내신 글이 전달은 됐지만, 상대 비서가 지금 답할 수 없는 상태입니다. 급하면 상대에게 직접 연락해 주세요.
- 이 글은 나중에 자동으로 다시 처리되지 않습니다. 필요하면 다시 보내 주세요.
- 보낸 글 앞부분: 「…」 — 보낸 담당이 이 대화가 아니면 그 담당에게 알려 주세요.
- 피터보이스가 자동으로 보낸 안내입니다. 이 안내에는 답장하지 마세요(지금은 상대 비서가 읽지 못합니다). 같은 상대에 대해서는 60분에 한 번, 하루 3번까지만 갑니다.
```

| 데몬 | 안내가 가는 경우 | 안 가는 경우 |
|---|---|---|
| 클라우드(`cloud_daemon.py`) | 로그인 만료 · 계정 한도(쿨다운 포함) · 미연결 · 월 턴 한도 · 저장 공간 한도 · 준비 실패 | 메모리·시간 초과로 턴이 죽은 경우, 「일시적 오류」 문구 |
| 맥(`daemon/worker.py`) | Claude 엔진의 로그인 만료 · 계정 한도(쿨다운 포함) | Codex · Gemini 엔진의 실패, 팀 프로젝트 턴, 데몬 자체가 꺼져 있는 경우 |
| 공통 | — | 같은 상대에게 60분 안에 이미 안내가 간 뒤의 글, 하루 3번을 채운 뒤의 글(24시간), 받는 쪽 데몬이 이 기능 이전 버전일 때 |

- **사유는 밝히지 않는다.** 로그인·한도 같은 계정 상태는 받는 사람의 것이다.
- **보낸 사람은 본문이 아니라 서버가 채운 필드로 정한다** (`subtype="external"` · `from_username`). 본문은 보낸 쪽이 쓰는 글이라 머리(`[외부 relay @…]`)나 묶음 칸막이를 흉내 낼 수 있다. 묶음 턴은 디스패처가 남긴 칸별 원본(`merged_parts`)에서 읽는다.
- **자동 글에는 자동 안내로 답하지 않는다.** 본문이 `[자동 안내` 나 `[자가진단 결과 회신]` 으로 시작하면 건너뛴다 — 양쪽이 다 막혀 있어도 왕복하지 않는다.
- **같은 (받는 사람, 보낸 핸들) 쌍에는 60분에 한 번, 24시간에 3번까지.** 안내도 연결의 하루 한도(양방향 합산)에 들어가고, 상대 비서가 안내에 답장해 오면 다시 안내가 나가므로 상한을 둔다. 데몬 메모리에만 두므로 데몬이 재시작하면 처음부터 센다.
- 안내는 보낸 사람의 **수신함**(연결 설정의 수신 프로젝트·브랜치)에 도착한다. 글을 실제로 보낸 담당이 다른 곳이면 수신함 담당이 「보낸 글 앞부분」으로 알아보고 전한다. 보낸 사람의 `auto_execute` 가 꺼져 있으면 「제안」으로 표시된다.
- 묶음 턴(자동 메시지 여러 통이 한 턴)에서는 칸마다 보낸 사람을 따로 본다. 안내는 받는 사람 채팅에 답이 나간 **뒤에** 보낸다(안내 전송이 느려도 채팅 답이 늦지 않게).
- 규칙·시험: `scripts/external_notice.py` · `python3 scripts/test_external_notice.py`. 로그: 클라우드 `[ext-notice] user=… → @핸들: sent`, 맥 `external relay ended without a real answer (auth|limited) … → notice to @핸들`.
- 에이전트가 이 안내를 받으면: 회신하지 않는다. 그 글의 답을 기다리던 일을 「상대 대기」에서 빼고, 필요하면 유저에게 「상대 비서가 답할 수 없는 상태」라고 알린다.

## UI 컴포넌트

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| `AgentConnectionsPanel` | 설정 페이지(`/settings`) 카드 섹션 | 받은신청/보낸신청/연결됨/차단 관리, 내 핸들 표시, 내 수신 스코프 칩, "수신 설정 필요" 안내 |
| `SendRequestModal` | 패널 내 모달 | 핸들+메모로 신청, 검증/에러 처리 |
| `ScopeModal` | 수락/스코프 편집 모달 | 수신 프로젝트·브랜치·자동실행·첨부·하루한도 설정 (수락 시 / "내 수신 설정") |
| `InboxTargetSelect` | ScopeModal·SendRequestModal 공용 | 수신함 선택기 — 프로젝트(이름순) 그룹 아래 활성 브랜치(번호순), 8개 넘으면 검색칸. 값 규칙은 `lib/inbox-target.ts` (검증·표시 라벨도 여기) |
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
