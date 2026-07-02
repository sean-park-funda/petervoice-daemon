# 백그라운드 서브에이전트 후속 결과가 채팅에 유실됨

- 날짜: 2026-07-02
- 영향: 전 고객 (구조적 버그, 발생 조건 충족 시 항상)
- 심각도: 중 (데이터/응답 유실, 무음 실패 — 에러 로그 없음)
- 수정 커밋: `7fc14b7` (`scripts/daemon/claude_runner.py`)

## 증상

- 봇: "조사 에이전트 돌리는 중입니다. 결과 나오면 바로 정리해드릴게요." 라고만 답하고 끝남.
- 이후 실제 조사 결과 메시지가 **채팅에 영영 안 뜸.**
- 유저가 다시 물으면 봇이 "이전에 다음 메시지로 올렸는데 못 보신 것 같다"며 결과를 다시 제시 →
  **봇(세션) 관점엔 보냈고, 유저(채팅 DB) 관점엔 없음** — 서로 모순돼 보임.

## 근본 원인

한 번의 `claude -p` 실행에서 에이전트가 `run_in_background` 서브에이전트를 띄우면,
**`result` 이벤트가 한 턴에 2번 이상** 발생한다:

1. 즉시 응답("돌리는 중")에서 첫 `result`
2. 백그라운드 작업 완료로 턴이 재개되어 실제 결과에서 두 번째 `result`

`claude_runner.py`의 응답 수집 코드가 이랬음:

```python
if event.get("result") and not response_text.strip():   # ← 버그
    response_text = event["result"]
```

`not response_text.strip()` 가드는 **response_text가 비어있을 때만** 담는다. 그래서
**첫 result만 남고, 백그라운드 완료로 온 두 번째 result(=실제 결과)는 조용히 버려졌다.**

부가 취약점: 같은 함수가 완성형 `assistant` 메시지의 `text` 블록은 무시하고(`tool_use`만 처리),
본문을 오직 `result` 이벤트(및 델타)에만 의존한다. 그래서 result 하나를 놓치면 곧바로 본문 유실.
(현재 명령엔 `--include-partial-messages`가 없어 `content_block_delta`도 거의 안 옴)

## 재현 (실측)

```
claude -p --output-format stream-json --verbose --dangerously-skip-permissions \
  '백그라운드 에이전트로 2+2 계산시키고, 너는 즉시 "계산 에이전트 돌리는 중"만 답하고 끝내라.
   백그라운드가 끝나면 "결과: N" 형식으로 알려라.'
```

stdout 이벤트 시퀀스:

```
[assistant.text] '계산 에이전트 돌리는 중'
[RESULT #1]  result='계산 에이전트 돌리는 중'      ← 즉시 응답
[system] init                                    ← 백그라운드 완료로 턴 재개
[assistant.text] '결과: 4'
[RESULT #2]  result='결과: 4'                     ← 실제 결과
```

→ 한 프로세스에 success `result` 이벤트 2개 확정. 구버전 로직은 `'계산 에이전트 돌리는 중'`만,
신버전 로직은 `'계산 에이전트 돌리는 중\n\n결과: 4'` 둘 다 캡처됨을 실측 검증.

## 수정

에러가 아닌 **모든 `result` 텍스트를 이어붙임** (첫 것만 남기지 않음):

```python
_rtxt = event.get("result")
if _rtxt and not event.get("is_error"):
    if response_text.strip():
        if not response_text.endswith("\n\n"):
            response_text += "\n\n"
        response_text += _rtxt
    else:
        response_text = _rtxt
```

- 일반 단일 턴: 첫 분기로 그대로 처리 → 회귀 없음.
- 백그라운드 후속: 최종 메시지에 이어붙여 전달됨.
- 반환 시그니처 유지 → worker/team.py 등 호출자 7곳 모두 자동 적용.

## 남은 개선 여지 (미적용)

- **실시간 전달**: 현재는 프로세스 종료 시점에 합쳐 한 번에 전달(백그라운드 완료가 곧 프로세스 종료라
  타이밍은 대체로 무난). 완결 result마다 즉시 개별 버블로 flush하려면 시그니처 확장 필요.
- **캡처 이중화**: `assistant` 메시지의 `text` 블록도 (result와 중복 제거해) 수집하면 단일 실패점 제거.

## 교훈

- "한 프로세스 = 응답 하나"라는 가정이 `run_in_background`에서 깨진다. 스트림 종료까지 여러 완결 응답이 올 수 있다.
- 무음 실패(에러 로그 없이 응답만 사라짐)는 원인 추적이 어렵다. 세션 transcript(SDK)와 채팅 DB를
  대조하면 "생성됐지만 전달 안 됨"을 빠르게 판별할 수 있다.
