---
name: slack
pv_version: "1.2.0"
version: 1.2.0
description: Read and send Slack messages — list channels, read/summarize conversations, post messages, DMs, reactions. Uses the connected Slack bot token.
---

# Slack

피터보이스에 연결된 슬랙 워크스페이스를 다룬다. 채널/DM을 읽고, 요약하고, 메시지를 보낸다.

## 인증

봇 토큰은 환경변수 `SLACK_BOT_TOKEN`(xoxb-...)에 있다. 피터보이스가 OAuth 연결 시 자동 주입한다.

**중요**: 토큰을 출력/로그에 노출하지 말 것. `Authorization: Bearer` 헤더에만 사용.

```python
# bash $VAR 확장이 안 될 수 있으므로 python으로 읽어 사용
TOKEN=$(python3 -c "import os; print(os.environ['SLACK_BOT_TOKEN'])")
```

연결 확인:
```bash
TOKEN=$(python3 -c "import os; print(os.environ['SLACK_BOT_TOKEN'])")
curl -s "https://slack.com/api/auth.test" -H "Authorization: Bearer $TOKEN"
# ok:true 이면 정상. team(워크스페이스), user(봇 이름) 확인 가능
```

`SLACK_BOT_TOKEN`이 없으면 슬랙이 연결되지 않은 것 → 유저에게 "설정 → 외부 서비스 연결 → Slack 연결하기" 안내.

### 여러 워크스페이스가 연결된 경우 (멀티 워크스페이스)
- `SLACK_WORKSPACES` = `"T024...:박태준 만화회사,T08...:BA엔터"` 처럼 **연결된 워크스페이스 목록**(team_id:이름).
- 워크스페이스별 토큰: `SLACK_BOT_TOKEN__<TEAM_ID>` (예: `SLACK_BOT_TOKEN__T024Q2WPCMC`). `SLACK_BOT_TOKEN` 은 가장 먼저 연결한 워크스페이스(primary)의 별칭.
- 유저가 "BA엔터 슬랙에 올려줘"처럼 회사를 지정하면 `SLACK_WORKSPACES` 에서 이름으로 team_id 를 찾아 그 토큰을 쓸 것. 지정이 없고 워크스페이스가 2개 이상이면 **어느 워크스페이스인지 되물을 것**.
- 발신 API(`/api/slack/send`)는 body 에 `"team_id":"T..."` 를 넣어 워크스페이스를 고른다(생략 시 primary).

## 핵심 동작

### 채널 목록
```bash
curl -s "https://slack.com/api/conversations.list?types=public_channel,private_channel&limit=1000" \
  -H "Authorization: Bearer $TOKEN"
# 각 채널: id, name, is_private, is_member
```
> 비공개 채널(`private_channel`)을 포함하려면 `groups:read` 스코프 필요. 없으면 `missing_scope` 에러.

### 대화 읽기 (요약용)
```bash
curl -s "https://slack.com/api/conversations.history?channel=CHANNEL_ID&limit=30" \
  -H "Authorization: Bearer $TOKEN"
```
- 사용자 ID(`user`)는 `users.info`로 이름 변환:
```bash
curl -s "https://slack.com/api/users.info?user=U123" -H "Authorization: Bearer $TOKEN"
# profile.real_name 사용
```

### 메시지 보내기

**봇 DM으로 사람(유저)에게 말을 걸 때는 반드시 피터보이스 발신 API를 쓸 것.**
봇 DM은 여러 에이전트가 공유하는 창구라, 직접 보내면 상대가 답장해도 그 답이 **당신에게 오지 않는다**.
이 API를 거치면 서버가 "이 메시지는 내가 보냈다"를 기록해, 스레드 답장이 당신 세션으로 되돌아온다.

```bash
API_URL=$(python3 -c "import json; c=json.load(open('$HOME/.claude-daemon/config.json')); print(c.get('api_url','https://www.peter-voice.site'))")
API_KEY=$(python3 -c "import json; print(json.load(open('$HOME/.claude-daemon/config.json'))['api_key'])")

curl -s -X POST "$API_URL/api/slack/send" \
  -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"channel":"CHANNEL_ID","text":"메시지 내용","from_project":"내_프로젝트ID","from_branch_id":123}'
# 응답: {"success":true,"ts":"...","channel":"...","thread_ts":"..."}
```
- `from_project` **필수** (회신 라우팅 키). 브랜치 세션이면 `from_branch_id`도 함께.
- 워크스페이스가 여럿이면 `"team_id":"T..."` 로 지정 (생략 시 primary).
- 스레드 답글: `"thread_ts":"<원본 ts>"` 추가 — 같은 스레드는 계속 같은 세션으로 묶인다.
- 유저에게는 "**스레드로 답장**해 달라"고 안내할 것. 최상위 답장은 채널 기본 담당에게 간다.

채널 공지 등 **회신을 받을 필요가 없는 발신**은 슬랙 API 직접 호출도 된다:
```bash
curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"channel":"CHANNEL_ID_or_#name","text":"메시지 내용"}'
```

### DM / 그룹 DM
```bash
# 봇이 속한 DM/그룹DM 목록
curl -s "https://slack.com/api/conversations.list?types=im,mpim&limit=200" \
  -H "Authorization: Bearer $TOKEN"
# 읽기/쓰기는 위 history/postMessage에 그 channel id 사용
```

### 이모지 반응
```bash
curl -s -X POST "https://slack.com/api/reactions.add" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"channel":"C123","timestamp":"<ts>","name":"thumbsup"}'
```

## 게이트차 (자주 막히는 지점)

- **`not_in_channel` (쓰기 실패)**: 봇이 그 채널의 멤버가 아님 → 슬랙에서 `/invite @봇이름`으로 초대.
- **비공개 채널이 안 보임/`missing_scope`**: `groups:read`, `groups:history` 스코프 필요 + 봇 초대. 스코프 추가 후 재연결(재설치) 필요.
- **`channel_not_found`**: 채널 id를 conversations.list로 먼저 확인. 이름(#name) 대신 id(C...) 권장.
- 봇은 **초대된 채널/DM**만 읽고 쓸 수 있다.

## 예시 지시 → 동작
- "OO 채널 오늘 대화 요약해줘" → conversations.list로 id 찾기 → history 읽기 → 사용자명 변환 → 요약
- "OO 채널에 공지 올려줘" → chat.postMessage (회신 불필요한 발신)
- "담당자한테 물어봐줘/자료 요청해줘" → **/api/slack/send** (답장을 받아야 하므로)
- "안 본 DM 있어?" → conversations.list(im) → 각 history 확인
