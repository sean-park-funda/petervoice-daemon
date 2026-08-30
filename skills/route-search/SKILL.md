---
name: route-search
description: 이동 경로 위 맛집·휴게소·주유소를 네이버 실시간 경로와 좌표 검색 근거로 찾고 도착시각·이탈시간·가격까지 계산. 트리거: "가는 길에 OO 찾아줘", "몇 시쯤 어디서 먹지", "휴게소에서 자고 갈까", "경부로 가면 어때"
pv_version: "2.0.0"
---
# Route Search — 경로 기반 장소·휴게소·경유 계산 (근거 기반)

유저의 이동 중 요청("가는 길에 OO 찾아줘", "몇 시쯤 어디서 먹지", "휴게소에서 자고 갈까", "경부로 가면 어때")을
**네이버 지도 API + 좌표 검색 데이터만으로** 답하기 위한 스킬. 2026-08-30 재정비.
(마켓 동기화 보호: 위 frontmatter 의 `pv_version` 이 마켓보다 높아야 로컬이 유지된다. 고칠 때마다 올릴 것.)

## ⛔ 절대 규칙
1. **소요시간·거리·이탈시간은 네이버 Directions(실시간)만.** OSRM·"내비 기준 추정"·감으로 줄이기 금지.
   네이버 API 는 정상 동작한다 (`NAVER_MAP_CLIENT_ID/SECRET`). 옛 문서의 "403 미신청"은 해결된 옛 정보.
2. **"없다"·"뿐이다"는 경로 좌표 스캔(`route_scan.py`) 결과로만 말한다.** "맥도날드 상주점" 같은 도시명 키워드 검색이
   빈 결과인 것은 근거가 아니다.
3. **휴게소 방향·위치는 `rest_areas_on_route.py`(도로공사 공식+네이버) 로 확인**한다. 상식으로 "OO휴게소 서울방향" 말하지 않는다.
4. 특정 도로를 타는 경로("경부로 가면?")는 **도로 위 좌표를 경유점으로 강제**해서 계산한다. 도시 좌표를 찍으면 이탈이 섞여
   부풀려진다(08-30 실수: +17분이라 했으나 실제 +7분). `route_with_via.py` 로 도로명 기준 앵커를 뽑거나, 이전 경로 path 에서
   해당 도로 section 의 좌표를 쓴다.
5. "1시간 반 더 가면 OO" 류는 거리로 어림잡지 말고 **구간별 네이버 계산**으로 말한다.
6. 답변에는 반드시 **method 한 줄**(출력 JSON 의 `method`)을 근거로 붙인다. 추정이 섞였으면 "추정"이라고 쓴다.
7. 유저 위치는 메시지 끝 `(현재 위치: lat, lng)` 를 쓴다. 안 붙어 오면 마지막 위치+경과시간 기준임을 명시한다.
8. 반려동물·차량 등 유저 조건이 프롬프트에 있으면 편의시설 필드에서 확인되는 것만 표기한다.

## 준비
- 환경변수 `NAVER_MAP_CLIENT_ID`, `NAVER_MAP_CLIENT_SECRET` (네이버 클라우드 Maps API — Directions 5/15, Geocoding). 설정 > 환경변수에 등록.
- 스크립트는 표준 라이브러리만 사용 — 시스템 `python3` 로 실행 (별도 venv·pip 불필요).
- 파일: `route_scan.py` `rest_areas_on_route.py` `route_with_via.py` `data/ex_rest_areas.json` (이 스킬 폴더에 함께 배포됨)

## 스크립트

### 1) `route_scan.py` — 경로 위 장소 스캔 (주력)
```bash
cd ~/.claude/skills/route-search && python3 route_scan.py \
  --start "lng,lat" --goal "lng,lat" --query "맥도날드" --depart 20:30 \
  [--force-via "lng,lat"] [--window 60-180] [--step 12] [--max-off 1.0] [--top 8] \
  [--map /tmp/state.json --title "제목"]
```
- 경로를 `--step` km 간격으로 샘플링 → 각 지점 좌표로 네이버 플레이스 검색 → 경로 `--max-off` km 이내만
- 후보별: 네이버 경유 재계산 → `arrive`(도착시각) `extra_min`(이탈) `after_min`(이후 잔여) + 영업시간 `hours` + 메뉴가격 `menu` + 평점/리뷰수
- `--window` 로 "출발 후 90~150분 사이" 같은 시간대 필터
- `--map` 이면 지도 상태 파일 생성(정체색 segments + eta 핀). 업로드 후 마커로:
  ```bash
  curl -s -X POST "$API_URL/api/files/upload" -H "X-Api-Key: $API_KEY" -F "file=@/tmp/state.json"  # → url
  ```
  응답에 `[[map/show:URL]]`. 지도 스키마·기본 구성은 매뉴얼 04장 '지도 패널' 절(정체색 segments, eta 핀, mylocation 은 이동 중일 때만).
- 쿼리 예: 맥도날드 / 갈비탕 / 사우나 / 주유소 / 휴게소 / 한우 / 찜닭. 좌표 검색이라 브랜드·업종 모두 됨.

### 2) `rest_areas_on_route.py` — 휴게소(방향 포함)
```bash
python3 rest_areas_on_route.py --start "lng,lat" --goal "lng,lat" [--via "lng,lat"] [--fuel]
```
출처 EX(도로공사 공식)/NAVER 표기. `--via` 로 특정 고속도로 강제. 취침·주유 계획은 이걸로.

### 3) `route_with_via.py` — 특정 도로 경유 경로 + 구조 검증
```bash
python3 route_with_via.py --start "lng,lat" --goal "lng,lat" --via "경부고속도로"
```
기준 경로에 없는 도로를 OSM 앵커로 강제 → section 에 실제로 포함됐는지 검증. 실패는 실패로 보고.

### 4) 개별 매장 상세 (메뉴가격·영업시간·리뷰)
메뉴가격·영업시간은 `route_scan.py` 가 매장 페이지(`m.place.naver.com/restaurant/{id}/menu/list`)에서 직접 뽑는다. 유저는 **예산을 중요하게 본다** — 대표 메뉴 가격과 2인 예산을 항상 적을 것. (naver-place 스킬이 설치돼 있으면 `naver_place.py "검색어"` 로 블로그 발췌·평점 보강 가능.)

## 답변 템플릿
1. 정정할 것이 있으면 먼저 정정 (이전 수치와 왜 달라졌는지)
2. method 한 줄
3. 표: 매장 | 경로상 km | 도착 | 이탈 | 이후 잔여 | 영업 | 대표가격
4. 일정(출발시각 기준) + 추천 1개와 이유
5. 지도는 후보 비교(S1)/경로 안내(S2)/이동 중(S3)일 때만

## 좌표 메모
- 자주 쓰는 출발지·목적지 좌표는 **프로젝트/브랜치 프롬프트**에 적어 둔다 (이 스킬 파일은 전 유저 공용).
- 특정 고속도로 강제용 경유점은 `route_with_via.py --via "도로명"` 으로 뽑는다.

## 폐기
- 옛 `route_poi_search.py` (IC 데이터 + Playwright 검색 + OSRM): 검색이 캡차로 죽고 시간은 OSRM 이라 **쓰지 않는다.** 번들에서 제외.
