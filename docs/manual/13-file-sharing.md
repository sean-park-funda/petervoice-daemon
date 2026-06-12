# 13. 파일 공유

## 개요

두 가지 방향의 파일 공유를 지원:
1. **유저 → 봇**: 웹 UI에서 파일 첨부하여 메시지 전송
2. **봇 → 유저**: 로컬 파일을 웹 링크로 변환하여 공유

## 유저 → 봇: 파일 첨부

### 업로드 프로세스

```
1. 파일 선택 (버튼, 드래그, Ctrl+V)
2. POST /api/files/upload (JSON 모드)
   → Supabase Storage signed URL 발급 (2분 유효)
3. PUT signed URL (파일 직접 업로드)
   → Vercel 4.5MB 제한 우회
4. 메시지에 파일 메타데이터 포함
   → { name, url, size, type }
```

### 제한

- 최대 파일 크기: **50MB**
- 지원 형식: 모든 파일 (이미지, PDF, 문서 등)

### 데몬 처리

```
데몬이 메시지 폴링 → 파일 URL 감지
  ↓ Supabase Storage에서 다운로드
  ↓ ~/.claude-daemon/downloads/ 에 저장
  ↓ 로컬 경로를 Claude Code에 전달
```

### 스토리지 정리

데몬이 파일을 다운로드한 후 Supabase Storage에서 삭제 (스토리지 절약).

## 봇 → 유저: 파일 공유

### 업로드 API

```
POST $API_URL/api/files/upload
인증: Authorization: Bearer $API_KEY
Content-Type: multipart/form-data
Body: file (FormData)

응답:
{
  "url": "https://xxx.supabase.co/storage/v1/object/public/...",
  "name": "report.pdf",
  "size": 123456,
  "type": "application/pdf"
}
```

### 사용 방법 (봇)

`_common` 프롬프트에 정의:

```bash
API_URL=$(python3 -c "import json; c=json.load(open('/Users/sean/.claude-daemon/config.json')); print(c.get('api_url'))")
API_KEY=$(python3 -c "import json; print(json.load(open('/Users/sean/.claude-daemon/config.json'))['api_key'])")

curl -X POST "$API_URL/api/files/upload" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@/path/to/local/file.pdf"
```

→ 반환된 URL을 유저에게 텍스트로 전달

### 제한

- 최대 50MB
- 공개 URL로 생성됨 (인증 불필요)

## 미디어 업로드

오디오 파일 전용 업로드 (TTS 등):

```
POST /api/media/upload
인증: API 키
Content-Type: multipart/form-data
Body: file (audio/mpeg 등)
→ tts-audio 버킷에 저장
```

## 파일 표시 (MessageBubble)

### 이미지 파일

인라인 프리뷰 (최대 높이 240px), 클릭으로 원본 보기

### 일반 파일

```
📎 report.pdf (1.2MB)
```

클릭하면 새 탭에서 다운로드

### 자동 오픈 URL

봇이 `[[url:https://...]]` 패턴으로 보내면 자동 브라우저 오픈 + 초록 버튼 표시
