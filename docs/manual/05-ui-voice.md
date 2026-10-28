# 05. 웹 UI — 음성모드

## 구성 요소

| 컴포넌트/훅 | 파일 | 역할 |
|-------------|------|------|
| VoiceModeOverlay | `components/VoiceModeOverlay.tsx` | 전체화면 음성 UI |
| VoiceModeToggle | `components/VoiceModeToggle.tsx` | 마이크 토글 버튼 |
| VoiceSelector | `components/VoiceSelector.tsx` | TTS 음성 선택 |
| useAudioCapture | `hooks/useAudioCapture.ts` | Soniox STT 스트리밍 (2026-09-21 정정) |
| useSpeechSynthesis | `hooks/useSpeechSynthesis.ts` | TTS 엔진 |
| useSpeechRecognition | `hooks/useSpeechRecognition.ts` | Web Speech API 폴백 (⚠️ 2026-09-21 확인: `useVoiceSession` 은 `useAudioCapture` 만 사용, 이 훅은 정의만 있어 실제 폴백으로 동작하지 않음) |
| useWakeLock | `hooks/useWakeLock.ts` | 화면 꺼짐 방지 |

## 음성모드 상태 머신

```
┌──────────┐    말하기 시작    ┌───────────┐
│   IDLE   │ ──────────────→ │ LISTENING │
│ (대기 중) │                  │ (듣는 중)  │
└──────────┘                  └─────┬─────┘
     ▲                              │ 전송
     │                              ▼
     │                        ┌───────────┐
     │ TTS 완료               │  SENDING  │
     │                        │ (처리 중)  │
     │                        └─────┬─────┘
     │                              │ 응답 수신
     │                              ▼
     │                        ┌───────────┐
     └─────────────────────── │ SPEAKING  │
                              │ (응답 중)  │
                              └───────────┘
```

## STT (음성→텍스트)

### Soniox 실시간 스트리밍 (현행 — 2026-09-21 정정, 이전 서술의 Deepgram 은 낡음)

```
마이크 캡처 → 브라우저가 POST /api/stt/token 으로 임시 키 발급
  → Soniox 실시간 WebSocket 에 직접 스트리밍
  → 실시간 전사 (진행 중 + 완료 세그먼트 표시)
```

- 토큰: `POST /api/stt/token` (`app/api/stt/token/route.ts`)
- 자가복구(`hooks/useAudioCapture.ts`): 오류 시 백오프 재연결 1.5→3→6→10초, 토큰 요청 6초 타임아웃, 시작 10초 내 미연결 시 강제 재시작, 트랙 종료·mute·10초 무음 감시, 재연결 시 주인 화자 리셋
- 권한 거부는 자동 재시도로 풀리지 않음 → "마이크 권한을 허용한 뒤 음성 초기화(↺)" 안내
- 유저용 증상별 대응은 헬프 시트 09 — 공통 검색 `GET /api/help/search?q=마이크` 또는 `GET /api/help/topics/help%2F09-voice-microphone` (웹 레포 `content/help-topics/`)

### Web Speech API (폴백) — ⚠️ 2026-09-21 확인: `useVoiceSession` 은 `useAudioCapture` 만 사용하고 `useSpeechRecognition` 은 정의만 있어 실제 폴백으로 동작하지 않음

- 브라우저 내장 음성인식 (`webkitSpeechRecognition`)
- continuous + interimResults 모드
- 네트워크 에러 시 최대 3회 재시도

## TTS (텍스트→음성)

### Edge TTS

```
봇 응답 텍스트
  → cleanTextForSpeech() (마크다운, URL, 이모지 제거)
  → splitIntoChunks() (문장 단위 분할, 최대 150자)
  → POST /api/tts/generate (ko-KR-InJoonNeural)
  → MP3 오디오 스트림 → 재생
```

### 음성 설정

- 기본 음성: 한국어 남성 (InJoon, HyunSu, MinSeok, GiHun 순서)
- 속도: 1.1x
- 피치: 0.85
- localStorage에 선호 음성 저장

### iOS 오디오 해결

- 싱글톤 Audio 엘리먼트로 스피커 라우팅 유지
- 첫 사용자 제스처에 무음 MP3 재생 (오디오 언락)
- 15초 Chrome 버그 우회: 문장 단위 분할 재생

## 음성 명령

음성 인식 중 특정 단어를 3번 반복하면 실행:

| 명령 | 동작 |
|------|------|
| "삭제" × 3 | 현재 세그먼트 또는 마지막 발화 삭제 |
| "고" × 3 | 현재 내용 강제 전송 (3초 윈도우) |
| "컴팩트" × 3 | 컨텍스트 압축 트리거 |

## 음성모드 UI

### LISTENING 상태

```
┌─────────────────────────────┐
│                             │
│   (실시간 파형 시각화)       │
│   "듣고 있습니다..."         │
│                             │
│   [인식된 텍스트 표시]       │
│   (최대 3줄)                 │
│                             │
│   [🎤 전송] [❌ 종료]        │
└─────────────────────────────┘
```

### SENDING 상태

```
┌─────────────────────────────┐
│   [내가 보낸 요청 - 3줄 제한] │
│                             │
│   ⏳ 처리 중...              │
│   🔧 3개 도구 사용 중        │
│                             │
│   [⏹ 끊고 말하기] [❌ 종료]  │
└─────────────────────────────┘
```

### SPEAKING 상태

```
┌─────────────────────────────┐
│                             │
│   [봇 응답 텍스트]           │
│   (TTS 동시 재생)            │
│                             │
│   [⏹ 끊고 말하기] [❌ 종료]  │
└─────────────────────────────┘
```

## 회의 모드 — 회의록 / 라이브회의

음성모드에서 회의 모드로 들어가면 시작 버튼이 둘이다 (웹 `components/meeting/MeetingMode.tsx`).

| 버튼 | 하는 일 |
|---|---|
| **회의록** | 회의 내내 실시간 자막만 받아 적고, 끝나면 그 전사로 지금 담당이 `docs/meetings/` 에 회의록을 쓴다 |
| **라이브회의** (2026-10-28) | 회의 **중에** 서기가 안건을 골라 조사원들에게 맡기고, 결과가 회의 화면 상황판에 바로 뜬다 |

### 라이브회의 흐름
1. 시작하면 지금 담당(프로젝트 또는 브랜치) 아래에 **서기** 브랜치가 생긴다 — Haiku, 역할 프롬프트 자동, 회의 폴더 `docs/meetings/<날짜-시각>-라이브회의/`
2. 브라우저가 확정된 자막을 **단락**으로 묶어 서기에게 유저 메시지 `[라이브회의 단락 N · 시각]` 로 보낸다. 직전 전송 후 60초가 지났고
   서기가 직전 단락에 답했을 때만 보낸다(3분 넘게 답이 없으면 그냥). 한 세션은 메시지를 하나씩 처리하므로 줄이 서지 않게 하는 장치다.
3. 서기는 `live-notes.md` 에 요지를 적고, 조사가 필요한 안건(사실 확인·숫자·비교·사례·규정)이나 "알아봐줘" 같은 명시 요청이면
   `POST /api/live-meeting/topic` 으로 **조사원** 브랜치(Sonnet)를 서기 아래에 만든다. 회의당 8명까지(초과 429). 답은 한 줄(`📌 안건 → 조사원 #N` / `할 일 없음`).
4. 조사원은 `topics/<NN>-<주제>.md` 에 결과를 쓰고 자기 채팅에 결론 3~5줄로 답한다 — 이 답이 작전실 카드의 결론이 된다. 서기에게 회신하지 않는다.
   - **작전실 화면**(웹 `components/meeting/WarRoom.tsx`, 10/28): 라이브회의를 시작하면 전체화면으로 뜬다. 서기 카드(듣는 중·차례 대기·정리 중 + 배정 줄) 아래에
     조사원이 생길 때마다 카드가 붙고, 카드마다 대기 / 조사 중(지금 하는 일 — "🔍 웹 검색 · 검색어", "✍️ 문서 작성" — 과 경과 시간) / 완료(결론 3줄·걸린 시간).
     누르면 전체 답과 조사원 채팅 링크. "지금 하는 일"은 `user_status.streaming_text`(세션별 JSON)의 도구 줄을 `lib/meeting/activity.ts` 가 사람 말로 바꾼 것.
5. 회의를 끝내면 회의록 경로 그대로 원래 담당이 회의록을 쓰고, 회의록 요청에 「조사 결과」 절을 넣으라는 안내가 붙는다(`liveRoom` → 포탈 `live_room`).
6. 회의 뒤 서기·조사원 브랜치는 **그대로 남는다** — 유저가 그 채팅에서 이어서 의논하거나 "더 파봐"라고 시킨다. 정리·삭제는 유저가 담당에게 시킨다(삭제 규칙대로 확인 후).

알아둘 것
- 턴을 쓴다: 1시간 회의면 서기 20~40턴 + 안건 수만큼 조사원 턴. 턴 상한이 있는 클라우드 요금제는 한 달 턴이 준다.
- 서기 단락(`[라이브회의 단락 …]`·`[라이브회의 종료]`)은 데몬이 **전용 줄**(`live_meeting_lanes`, 기본 2)에서 처리한다 — 일반 줄이 다른 담당들로 차 있어도 기다리지 않는다(10/28 실측: 12칸 포화로 단락이 97초 줄 섬).
  조사원은 일반 동시 실행 수(`max_concurrent`, 맥 기본 3)를 같이 쓴다 — 넘치면 차례를 기다린다.
- 서기는 effort low, 조사원은 medium 으로 브랜치에 박힌다 (유저 전역 설정 max 를 물려받으면 첫 단락 76초·조사 9분이었다).
- 서기를 만들지 못하면(권한 없는 대화 등) 회의는 일반 회의록으로 계속된다.
- API: `POST /api/live-meeting`(시작) · `GET /api/live-meeting?clerk=<id>`(상황판) · `POST /api/live-meeting/topic`(조사원 배정·추가 맥락, API 키). 프롬프트 템플릿: 웹 `lib/meeting/liveRoom.ts`.

## 상태 복원

음성모드를 나갔다 다시 들어올 때:
- 봇이 아직 작업 중이면 → SENDING 상태로 복원
- 스트리밍 텍스트가 있으면 → 표시
- 대기 중이면 → IDLE 상태

## 음성 관련 유틸리티 (`lib/voice-utils.ts`)

### checkVoiceSupport()

브라우저 음성 기능 지원 확인:
- STT: `webkitSpeechRecognition` 또는 `SpeechRecognition`
- TTS: `speechSynthesis`
- Wake Lock: `navigator.wakeLock`

### cleanTextForSpeech(text)

TTS 전 텍스트 정리:
- 코드 블록 제거
- 마크다운 문법 제거 (##, **, \`\`, [], () 등)
- URL 제거
- 이모지 제거
- 연속 공백 정리

### splitIntoChunks(text, maxLen=150)

문장 단위 분할:
- 한국어/영어 구두점으로 분할 (。.!? 등)
- 최대 150자/청크
- 긴 문장은 단어 경계에서 분할
- 15초 Chrome 오디오 버그 방지
