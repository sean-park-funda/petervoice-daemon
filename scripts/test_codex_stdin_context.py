#!/usr/bin/env python3
"""코덱스 지침 전달 시험 (2026-11-23) — 프로젝트 폴더 AGENTS.md 에 쓰지 않고 stdin 으로 넣는다.
 - 예전 데몬 블록은 걷어내되 유저가 쓴 내용은 그대로 둔다
 - 지침을 넣는 턴: 새 세션 / 표식 없는(AGENTS.md 시절) 세션 / 지침이 바뀐 세션(최소 간격)
실행: python3 scripts/test_codex_stdin_context.py
"""
import os
import sys
import tempfile
from pathlib import Path

import queue  # noqa: F401,E402  (scripts/daemon/queue.py 가림 방지)
import logging.handlers  # noqa: F401,E402
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon import claude_runner as cr  # noqa: E402
from daemon import sessions  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS" if ok else "FAIL"), name, detail if not ok else "")
    fails += 0 if ok else 1


S, E = cr._AGENTS_MD_MARKER_START, cr._AGENTS_MD_MARKER_END
BLOCK = f"{S}\n# 시스템 지침\n경로 C:\\AI\\x 와 \\n 리터럴\n{E}"

# ── 게이트: 쓰는 함수가 없어야 한다
check("_prepare_agents_md 없음(쓰는 쪽 제거)", not hasattr(cr, "_prepare_agents_md"))
src = (ROOT / "daemon" / "claude_runner.py").read_text(encoding="utf-8")
check("러너가 표지 블록을 만들어 쓰지 않는다", "daemon_block" not in src)

# ── 예전 블록 걷어내기
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    f = d / "AGENTS.md"
    check("파일 없음 → none", cr._strip_agents_md_block(td) == "none")

    f.write_text("# 내 규칙\n- 유저가 쓴 것\n", encoding="utf-8")
    m0 = f.stat().st_mtime_ns
    check("표지 없는 파일 → none", cr._strip_agents_md_block(td) == "none")
    check("표지 없는 파일은 건드리지 않음(내용·mtime)", f.read_text(encoding="utf-8") == "# 내 규칙\n- 유저가 쓴 것\n" and f.stat().st_mtime_ns == m0)

    f.write_text(BLOCK, encoding="utf-8")
    check("데몬 블록뿐 → removed", cr._strip_agents_md_block(td) == "removed")
    check("파일이 사라짐", not f.exists())
    check("다시 불러도 none(멱등)", cr._strip_agents_md_block(td) == "none")

    f.write_text(f"# 내 규칙\n- 유저가 쓴 것\n\n{BLOCK}", encoding="utf-8")  # 예전 데몬이 뒤에 덧붙인 모양
    check("유저 내용 + 블록 → stripped", cr._strip_agents_md_block(td) == "stripped")
    check("유저 내용은 그대로", f.read_text(encoding="utf-8") == "# 내 규칙\n- 유저가 쓴 것\n", repr(f.read_text(encoding="utf-8")))

    f.write_text(f"앞\n\n{BLOCK}\n\n뒤\n", encoding="utf-8")
    check("가운데 블록 → stripped", cr._strip_agents_md_block(td) == "stripped")
    check("앞뒤가 빈 줄 하나로 이어짐", f.read_text(encoding="utf-8") == "앞\n\n뒤\n", repr(f.read_text(encoding="utf-8")))

    _unterminated = f"앞\n\n{S}\n쓰다 끊긴 지침\n유저가 뒤에 쓴 것일 수도 있는 줄\n"
    f.write_text(_unterminated, encoding="utf-8")
    check("끝 표지 없음 → 건드리지 않음(unterminated)", cr._strip_agents_md_block(td) == "unterminated" and f.read_text(encoding="utf-8") == _unterminated)

    _mention = f"# 메모\n데몬은 `{S}` 와 `{E}` 사이에 프롬프트를 썼었다.\n- 이 줄은 남아야 한다\n"
    f.write_text(_mention, encoding="utf-8")
    check("본문에서 표지를 언급만 한 파일 → 안 건드림", cr._strip_agents_md_block(td) == "none" and f.read_text(encoding="utf-8") == _mention)

    f.write_bytes(f"# 윈도우 파일\r\n- 유저 줄\r\n\r\n{S}\r\n지침\r\n{E}".encode("utf-8"))
    check("CRLF 파일 → stripped", cr._strip_agents_md_block(td) == "stripped")
    check("CRLF 줄바꿈 보존", f.read_bytes() == "# 윈도우 파일\r\n- 유저 줄\r\n".encode("utf-8"), repr(f.read_bytes()))

    f.write_text(f"{S}\n{E}\n", encoding="utf-8")
    check("내용 없는 블록 → removed", cr._strip_agents_md_block(td) == "removed" and not f.exists())

    f.write_text(f"앞\n\n{S}\n지침 안에 `{S}` 언급\n둘째 줄\n{E}\n\n뒤\n", encoding="utf-8")
    check("블록 안에 표지 문자열이 섞여도 줄 단위 끝 표지까지", cr._strip_agents_md_block(td) == "stripped" and f.read_text(encoding="utf-8") == "앞\n\n뒤\n", repr(f.read_text(encoding="utf-8")))

    f.write_text(f"{BLOCK}\n\n가운데\n\n{BLOCK}\n", encoding="utf-8")
    check("블록 둘 → 둘 다 제거", cr._strip_agents_md_block(td) == "stripped" and f.read_text(encoding="utf-8") == "가운데\n")
    check("남은 파일에 표지 없음", S not in f.read_text(encoding="utf-8") and E not in f.read_text(encoding="utf-8"))

    if os.name != "nt":
        f.unlink()
        target = d / "real.md"
        target.write_text(BLOCK, encoding="utf-8")
        os.symlink(target, f)
        check("링크(블록뿐) → 지우지 않고 비움", cr._strip_agents_md_block(td) == "stripped" and f.is_symlink() and target.read_text(encoding="utf-8") == "")

# ── 이 턴에 지침을 넣을지
D = cr._codex_ctx_decision
H = cr._codex_ctx_hash
A, B2 = "지침 A", "지침 B"
now = 1_000_000.0
check("지침 없음 → 안 넣음(empty)", D("", None, {}, now) == (False, "empty", "") and D("  \n", "sid", {}, now)[1] == "empty")
check("새 세션 → 넣음(new)", D(A, None, {}, now) == (True, "new", H(A)))
check("표식 없는 재개(AGENTS.md 시절) → 넣음(legacy)", D(A, "sid", {"ctx_hash": None, "ctx_fed_at": None}, now) == (True, "legacy", H(A)))
check("같은 지침 재개 → 안 넣음(same)", D(A, "sid", {"ctx_hash": H(A), "ctx_fed_at": now - 10}, now) == (False, "same", H(A)))
check("지침 바뀜 + 6시간 지남 → 넣음(refresh)", D(B2, "sid", {"ctx_hash": H(A), "ctx_fed_at": now - 6 * 3600}, now) == (True, "refresh", H(B2)))
check("지침 바뀜 + 방금 넣음 → 미룸(throttled)", D(B2, "sid", {"ctx_hash": H(A), "ctx_fed_at": now - 60}, now) == (False, "throttled", H(B2)))
check("최소 간격 0 → 바뀌면 바로", D(B2, "sid", {"ctx_hash": H(A), "ctx_fed_at": now - 1}, now, 0)[1] == "refresh")
check("넣은 시각 기록 없음 → 넣음", D(B2, "sid", {"ctx_hash": H(A)}, now)[1] == "refresh")
check("해시는 16자·결정적", len(H(A)) == 16 and H(A) == H(A) and H(A) != H(B2))
check("러너: 지침 본문이 비어도 세션 맥락이 있으면 새 세션에 넣는다", "_codex_ctx_decision(\n        ctx_stable or combined, sid," in src)
check("러너: stdin 은 finally 에서 닫는다(인코딩 예외여도 EOF)", src.count('data.encode("utf-8", errors="replace")') == 2 and src.count("p.stdin.close()") == 2
      and 'p.stdin.write(data.encode("utf-8"))' not in src)
check("러너: 표식은 정상 응답이면 남긴다(turn.completed 없는 버전 대비)", "turn_completed or (got_message and not saw_error and proc.returncode == 0)" in src)

# ── stdin 덩어리
p = cr._codex_ctx_payload("역할: 회계 담당", "안녕?", "new")
check("새 세션 덩어리: 태그·지침·유저 글 순서", p.index(cr._CODEX_CTX_OPEN) < p.index("역할: 회계 담당") < p.index(cr._CODEX_CTX_CLOSE) < p.index("안녕?") and p.endswith("안녕?"), p)
check("새 세션 머리말", "이 대화 전체에 적용" in p.split("\n", 1)[0] and "대체" not in p.split("\n", 1)[0])
p2 = cr._codex_ctx_payload("역할: 회계 담당", "안녕?", "legacy")
check("재개 덩어리 머리말은 「대체」", "대체" in p2.split("\n", 1)[0] and p2.endswith("안녕?"))
check("refresh 도 같은 머리말", cr._codex_ctx_payload("x", "y", "refresh").split("\n", 1)[0] == p2.split("\n", 1)[0])

# ── 세션 표식 저장
with tempfile.TemporaryDirectory() as td:
    sessions.CODEX_SESSIONS_PATH = Path(td) / "codex_sessions.json"
    sessions._codex_sessions = {}
    check("없는 세션의 표식은 비어 있음", sessions.get_codex_session_ctx("p") == {"ctx_hash": None, "ctx_fed_at": None})
    sessions.update_codex_session("p", "sid-1")  # 예전 호출 모양(표식 없이)
    check("표식 없이 저장 → legacy 로 판정될 상태", sessions.get_codex_session_id("p") == "sid-1" and sessions.get_codex_session_ctx("p")["ctx_hash"] is None)
    sessions.update_codex_session("p", "sid-1", ctx_hash="abc")
    c = sessions.get_codex_session_ctx("p")
    check("표식 저장", c["ctx_hash"] == "abc" and isinstance(c["ctx_fed_at"], float))
    t0 = c["ctx_fed_at"]
    sessions.update_codex_session("p", "sid-1")
    c = sessions.get_codex_session_ctx("p")
    check("표식 없는 턴은 기존 표식을 지우지 않음", c["ctx_hash"] == "abc" and c["ctx_fed_at"] == t0)
    check("파일에도 남음", '"ctx_hash": "abc"' in sessions.CODEX_SESSIONS_PATH.read_text(encoding="utf-8"))
    sessions.reset_codex_session("p", "test")
    check("리셋하면 표식도 사라짐(다음 턴 new)", sessions.get_codex_session_id("p") is None and sessions.get_codex_session_ctx("p")["ctx_hash"] is None)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
