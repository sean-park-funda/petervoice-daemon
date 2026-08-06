# 번들 스킬 버전 규칙 (필수)

SkillsSyncer는 고객 머신에 이미 설치된 스킬을 **번들 `pv_version` > 로컬 `pv_version`일 때만** 덮어쓴다.
`pv_version`이 없으면 (0,0,0)으로 취급되어 **업데이트가 영원히 전파되지 않는다** (2026-08-07 Joory 맥 구버전 browser-handoff 사고).

## 규칙
1. 모든 번들 스킬의 SKILL.md 프론트매터에 `pv_version: "X.Y.Z"` 필수
2. **스킬 내용을 수정하면 반드시 pv_version을 올려서 커밋** — 안 올리면 기존 설치자에게 전파 안 됨
3. Sean 맥의 개발 원본(~/.claude/skills/)과 번들의 pv_version을 **같게** 유지 — 번들이 더 높으면 데몬 재시작 시 개발 원본이 번들로 덮어써진다
4. 덮어쓰기는 디렉토리 전체 교체 — prompts/·scripts/ 하위 파일 변경도 pv_version 범프로 전파된다
