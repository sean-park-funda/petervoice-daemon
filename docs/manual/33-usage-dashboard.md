# 33. 사용량 대시보드 (Usage Dashboard)

## 개요

Claude/Codex API 호출의 토큰 사용량과 비용을 분석하는 대시보드입니다.

## 접근 방법

`/usage` 페이지 (채팅 헤더 또는 사이드바에서 접근)

## 조회 기간

상단 탭으로 전환:
- **일** (오늘)
- **주** (최근 7일)
- **월** (최근 30일)

## 표시 지표

### 요약 카드

| 지표 | 설명 |
|------|------|
| 총 비용 | 기간 내 전체 API 비용 (USD) |
| 입력 비용 | input token 비용 |
| 출력 비용 | output token 비용 |
| 캐시 읽기 비용 | prompt cache read 비용 |
| 캐시 쓰기 비용 | prompt cache write 비용 |
| 캐시 읽기 토큰 | 캐시에서 읽은 토큰 수 |
| 캐시 쓰기 토큰 | 캐시에 쓴 토큰 수 |
| 출력 토큰 | 생성된 output 토큰 수 |
| 총 호출 수 | API 호출 횟수 |

### 모델별 분석

각 Claude/Codex 모델의 호출 수 및 비용을 비교합니다.

### 프로젝트별 분석

어떤 프로젝트가 가장 많은 비용을 사용하는지 확인합니다.

### 일별 막대 그래프

기간 내 날짜별 비용 추이를 막대 그래프로 표시합니다.

## API

```
GET /api/usage?period=day|week|month
Authorization: Bearer {api_key}
```

응답:
```json
{
  "period": "week",
  "total_cost_usd": 1.23,
  "total_input_cost_usd": 0.45,
  "total_output_cost_usd": 0.67,
  "total_cache_read_cost_usd": 0.05,
  "total_cache_write_cost_usd": 0.06,
  "total_cache_read_tokens": 50000,
  "total_cache_write_tokens": 10000,
  "total_output_tokens": 30000,
  "total_calls": 120,
  "by_model": [...],
  "by_project": [...],
  "by_day": [...]
}
```

## 데이터 수집

데몬이 Claude/Codex CLI 응답에서 usage 정보를 파싱하여 DB에 기록합니다:

- Claude: JSONL `result` 이벤트의 `usage` 필드
- Codex: 마찬가지로 result 이벤트에서 토큰 집계

캐시 읽기/쓰기 비용도 분리 추적되어 프롬프트 캐싱 효율을 확인할 수 있습니다.

## 활용

- 월별 API 비용 예측
- 비용이 많이 드는 프로젝트 파악
- 프롬프트 캐시 효율 확인 (캐시 읽기 비율이 높을수록 비용 절감)
