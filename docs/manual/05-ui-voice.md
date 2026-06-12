# 05. 웹 UI — 음성모드

## 구성 요소

| 컴포넌트/훅 | 파일 | 역할 |
|-------------|------|------|
| VoiceModeOverlay | `components/VoiceModeOverlay.tsx` | 전체화면 음성 UI |
| VoiceModeToggle | `components/VoiceModeToggle.tsx` | 마이크 토글 버튼 |
| VoiceSelector | `components/VoiceSelector.tsx` | TTS 음성 선택 |
| useAudioCapture | `hooks/useAudioCapture.ts` | Deepgram STT 스트리밍 |
| useSpeechSynthesis | `hooks/useSpeechSynthesis.ts` | TTS 엔진 |
| useSpeechRecognition | `hooks/useSpeechRecognition.ts` | Web Speech API 폴백 |
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

### Deepgram Nova-2 (기본)

```
마이크 캡처 → MediaRecorder (250ms 청크)
  → WebSocket (wss://api.deepgram.com/v1/listen)
  → 실시간 전사 (한국어, nova-2 모델)
  → 세그먼트 기반 표시 (진행 중 + 완료)
```

- 토큰: `POST /api/stt/token` → Deepgram API 키
- 오디오 버퍼링: WebSocket 연결 중 최대 2초 버퍼
- VAD (음성 활동 감지): utterance_end 1.5초 침묵
- 볼륨 모니터링: AnalyserNode RMS 계산 → 파형 시각화

### Web Speech API (폴백)

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
