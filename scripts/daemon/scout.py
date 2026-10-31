"""하트비트 정찰 턴(scout) — 빈 하트비트를 본 세션 대신 가벼운 새 세션에서 먼저 판정한다.

2026-10-31 Sean 승인(「계획대로 실행해」, login-manager #1 이관) — docs/plans/2026-10-31-session-hygiene-implementation.md.

왜: 하트비트는 그 담당자의 본 세션(`--resume`)에서 돌아 매 회 대화 전체를 프리필한다. 10/30~31 실측으로 데몬 턴의 44%가
「할 일 없음」 회차였고, 문맥 45만 토큰짜리 세션은 그 한 줄에 API 환산 $5 였다. 정찰은 resume 없는 **새 세션**(역할 프롬프트 +
HEARTBEAT 파일 + 최근 회차 결과)에서 「지금 처리할 일이 있나」만 판정한다 — 기록 파일 첫 줄 확인·점검 스크립트 실행 같은
가벼운 확인은 여기서 하고, 있을 때만 본 세션을 깨운다. 본 세션은 손대지 않으므로 지식은 그대로다.

맹점과 보호: 상태를 본 세션 안에만 두는 담당(규칙과 달리 흔하다)에게 정찰이 계속 「없음」을 내면 일이 영영 안 돈다.
  ① `force_every` 회차마다 한 번은 정찰 없이 본 세션을 그대로 깨운다(기본 4회마다)
  ② 판정 줄을 못 읽거나 시간 초과·오류면 「있음」으로 본다 — 비용으로 때우고 일은 놓치지 않는다
  ③ HEARTBEAT 파일이 없거나 비어 있으면 정찰하지 않는다(기존 건너뛰기 규칙 그대로)

config.json `heartbeat_scout` (전부 선택):
  {"enabled": true, "model": "claude-sonnet-5-5", "effort": "medium", "max_turns": 10,
   "timeout_sec": 300, "force_every": 4, "max_concurrent": 2, "transcript_keep_days": 7}
기록: ~/.claude-daemon/heartbeat-scout.jsonl — 회차별 판정·이유·토큰. 주간 효과 점검(peter-voice scripts/session-hygiene-weekly.py)이 읽는다.
정찰 세션 전사(.jsonl)는 다시 쓸 일이 없으므로 `transcript_keep_days` 뒤에 지운다(세션 폴더가 하루 수백 개씩 불어나는 것을 막는다).
"""

import glob
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from daemon.globals import config, logger, CLAUDE_CMD, DAEMON_DIR, PROMPTS_DIR, IS_WINDOWS
from daemon.limits import build_claude_env, seat_config_dir, account_cooldown_notice
from daemon.supabase import get_project_dir, _fetch_project_settings, _fetch_recent_conversation, fetch_prompt_from_supabase
from daemon.utils import _strip_ansi

LEDGER_PATH = DAEMON_DIR / "heartbeat-scout.jsonl"
_CLEANUP_STAMP = DAEMON_DIR / "heartbeat-scout.cleanup"

DEFAULTS = {
    "enabled": True,
    "model": "claude-sonnet-5-5",
    "effort": "medium",
    "max_turns": 10,
    "timeout_sec": 300,
    "force_every": 4,
    "max_concurrent": 2,
    "transcript_keep_days": 7,
    "role_prompt_max_chars": 8000,
}

# 판정 줄: 마지막에 `SCOUT: {"work": true|false, "reason": "..."}` 한 줄
VERDICT_RE = re.compile(r"SCOUT\s*:\s*(\{.*?\})\s*$", re.S | re.M)

_ledger_lock = threading.Lock()


def settings() -> dict:
    raw = config.get("heartbeat_scout")
    merged = dict(DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if v is not None})
    elif isinstance(raw, bool):
        merged["enabled"] = raw
    return merged


def enabled() -> bool:
    return bool(settings().get("enabled", True))


def should_force_main(run_count: int, force_every: int) -> bool:
    """이번 회차(run_count+1)가 정찰 없이 본 세션을 깨워야 하는 회차인가. force_every<=0 이면 강제 없음."""
    try:
        n = int(force_every)
    except (TypeError, ValueError):
        n = DEFAULTS["force_every"]
    if n <= 0:
        return False
    return (int(run_count or 0) + 1) % n == 0


def parse_verdict(text: str) -> tuple[bool | None, str]:
    """정찰 응답에서 (work, reason) 을 읽는다. 못 읽으면 (None, '') — 호출자는 '있음'으로 다룬다."""
    if not text:
        return None, ""
    m = None
    for m in VERDICT_RE.finditer(text):
        pass  # 마지막 판정 줄
    if m:
        try:
            data = json.loads(m.group(1))
            work = data.get("work")
            if isinstance(work, str):
                work = work.strip().lower() in ("true", "yes", "있음", "y", "1")
            if isinstance(work, bool):
                return work, str(data.get("reason") or "")[:300]
        except Exception:
            pass
    # 판정 줄이 없을 때의 보수적 추정: 첫 줄이 「할 일 없음」이면 없음, 아니면 모름
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if first.startswith("할 일 없음"):
        return False, first[:300]
    return None, ""


def account_for(project: str) -> tuple[str, str | None]:
    """프로젝트/브랜치가 쓰는 계정 라벨과 CLAUDE_CONFIG_DIR (본 턴과 같은 해석)."""
    settings_project = project
    try:
        if project.startswith("branch:"):
            from daemon.branches import fetch_branch
            br = fetch_branch(int(project.split(":")[1])) or {}
            settings_project = br.get("project_id") or project
        account = _fetch_project_settings(settings_project).get("account") or "default"
    except Exception:
        account = "default"
    return account, seat_config_dir(account)


def account_in_cooldown(project: str) -> bool:
    try:
        account, config_dir = account_for(project)
        return account_cooldown_notice(account, config_dir) is not None
    except Exception:
        return False


def _role_prompt(project: str, max_chars: int) -> str:
    """담당자의 역할 프롬프트(프로젝트 프롬프트 + 브랜치 프롬프트). 전역 가이드·공통 프롬프트는 넣지 않는다 — 정찰은 판정만 한다."""
    parts = []
    try:
        if project.startswith("branch:"):
            from daemon.branches import fetch_branch
            br = fetch_branch(int(project.split(":")[1])) or {}
            parent = br.get("project_id") or ""
            if parent:
                pp = fetch_prompt_from_supabase(parent) or ""
                if pp.strip():
                    parts.append(f"## 상위 프로젝트 프롬프트 ({parent})\n{pp.strip()}")
            bp = (br.get("prompt") or "").strip()
            title = br.get("title") or ""
            if bp or title:
                parts.append(f"## 이 브랜치 ({title})\n{bp}".rstrip())
        else:
            pp = fetch_prompt_from_supabase(project) or ""
            if pp.strip():
                parts.append(f"## 프로젝트 프롬프트 ({project})\n{pp.strip()}")
    except Exception as e:
        logger.warning(f"[scout] {project} role prompt fetch failed: {e}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…[역할 프롬프트 {len(text):,}자 중 앞 {max_chars:,}자만]"
    return text


def _system_prompt(project: str, project_dir: str, role: str, hb_name: str = "", hb_text: str = "") -> str:
    """정찰 시스템 프롬프트(붙임 파일). **HEARTBEAT 본문은 여기에 싣는다.**
    시스템 프롬프트는 user 메시지와 별개의 캐시 접두어다 — 10/31 실측: user 메시지만 바꾼 2회차에서 붙임 1.3만 토큰이
    cache_read 로 잡혔다(CLI 기본 1.1만 + 붙임). 본문이 user 메시지에 있던 동안은 본 세션이 한 번 답해 「최근 회차 결과」가
    바뀌면 본문 10만 토큰까지 통째로 다시 썼다(990: 12:48 w=101k → 13:17 w=99k, 1123 처럼 아무것도 안 바뀐 경우만 적중).
    그래서 바뀌지 않는 것(지시·체크리스트·역할)은 여기, 회차마다 바뀌는 것(최근 발췌)만 user 메시지에 둔다."""
    text = (
        "# 하트비트 정찰 턴\n"
        f"당신은 담당자 「{project}」의 하트비트를 대신 **점검만** 하는 정찰 세션입니다. 본 세션(긴 대화)은 따로 있고, "
        "당신이 「있음」이라고 판정할 때만 깨어나 일합니다. 작업 폴더: `" + project_dir + "`\n\n"
        "## 할 일\n"
        "HEARTBEAT 파일의 체크리스트를 읽고 **지금 이 회차에 처리할 일이 있는지**만 판정합니다.\n"
        "- 각 항목의 자체 주기·기록 파일 첫 줄·날짜 조건을 실제로 확인하세요 (Read·Glob·Grep, 가벼운 Bash: head/ls/date/wc/grep, "
        "그리고 「통과하면 조용히 스킵」으로 적힌 점검 스크립트 실행). 통과 기록을 파일에 한 줄 남기라고 돼 있으면 남기세요.\n"
        "- 그 이상의 일(코드·문서 수정, 배포, 보고, 릴레이, 외부 발송, 결정 카드, 긴 조사)은 **하지 마세요** — 본 세션 몫입니다. "
        "발견한 사실을 reason 에 적으면 본 세션이 그대로 이어받습니다.\n"
        "- 체크리스트가 「이전 단계가 끝났는지」처럼 본 세션의 진행 상태를 묻는데 파일로 확인할 수 없으면 「있음」입니다.\n"
        "- 애매하면 「있음」. 「있음」이 틀리면 본 세션이 한 번 더 도는 비용뿐이지만, 「없음」이 틀리면 일이 미뤄집니다.\n"
        "- 도구 호출은 10회 안에서. 점검 스크립트 출력이 길면 tail 로 끝만 보세요.\n\n"
        "## 답 형식\n"
        "짧은 점검 요약 뒤, **마지막 줄에 반드시** 다음 한 줄만 쓰세요(다른 말 붙이지 않기):\n"
        'SCOUT: {"work": true, "reason": "무엇이 생겼는지 한 줄"}   또는   SCOUT: {"work": false, "reason": "무엇을 확인했는지 한 줄"}\n'
    )
    if hb_text and hb_text.strip():
        text += (
            f"\n\n# 점검 대상 체크리스트 — `docs/{hb_name}` 원문\n"
            "아래는 그 파일의 내용 그대로다(다시 Read 할 필요 없다). 항목이 가리키는 주기·기록 파일·날짜 조건은 실제 파일로 확인한다.\n\n"
            f"{hb_text.strip()}"
        )
    if role:
        text += f"\n\n# 담당자 역할 (참고용)\n{role}"
    return text


def _user_prompt(hb_name: str, recent: str) -> str:
    """정찰 첫 user 메시지 — **회차마다 바뀌는 것(최근 회차 발췌)만** 싣는다. HEARTBEAT 본문은 시스템 프롬프트(_system_prompt)에.
    **시각·회차 번호를 넣지 않는다**(d74119a) — 10/31 실측: 시각이 앞에 있던 동안 정찰 19회 전부 첫 호출에서 붙임+이 메시지를
    매번 다시 썼다. 같은 프롬프트로 두 번째 프로세스를 돌리면 쓰기 0·읽기 전부, 비용 1/27.
    이 메시지가 바뀌면 Claude Code 가 첫 user 턴에 붙이는 주입 문맥(CLAUDE.md·메모리·스킬 목록, 1만~3만 토큰)도 같이 다시 쓴다 —
    그래도 본문(3만~17만)보다는 작다. 오늘 날짜는 Claude Code 시스템 프롬프트("Today's date")에 있고, 시각이 필요하면 `date` 로 본다."""
    return (
        f"[하트비트 정찰] 시스템 프롬프트에 실린 `docs/{hb_name}` 체크리스트(파일 원문)를 보고 이번 회차에 처리할 일이 있는지 판정해주세요. "
        "오늘 날짜는 시스템 프롬프트의 Today's date, 현재 시각이 필요하면 Bash `date` 로 확인하세요.\n\n"
        f"## 최근 회차 결과 (본 세션의 최근 메시지 발췌, 잘렸을 수 있음)\n{recent.strip() or '(없음)'}"
    )


def _append_ledger(entry: dict):
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _ledger_lock:
            with LEDGER_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 5MB 넘으면 뒤 20,000줄만 남긴다
            if LEDGER_PATH.stat().st_size > 5 * 1024 * 1024:
                lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()[-20000:]
                LEDGER_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning(f"[scout] ledger write failed: {e}")


def run_scout(project: str, task: dict, hb_name: str, hb_text: str, forced: bool = False) -> dict:
    """정찰 1회. 반환: {"work": bool, "reason": str, "error": str|None, "usage": dict, "duration_sec": float, "session_id": str}.
    오류·시간 초과·판정 불가는 전부 work=True (본 세션을 깨운다)."""
    st = settings()
    t0 = time.time()
    run_no = int(task.get("run_count") or 0) + 1
    out = {"work": True, "reason": "", "error": None, "usage": {}, "duration_sec": 0.0, "session_id": "", "cost_usd": None}
    project_dir = ""
    try:
        project_dir = get_project_dir(project)
        account, config_dir = account_for(project)

        role = _role_prompt(project, int(st["role_prompt_max_chars"]))
        recent = _fetch_recent_conversation(project, limit=6, max_chars=600)
        sys_text = _system_prompt(project, project_dir, role, hb_name, hb_text)
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        sys_file = PROMPTS_DIR / f"_scout_{project.replace(':', '_')}.md"
        sys_file.write_text(sys_text, encoding="utf-8")

        cmd = [CLAUDE_CMD, "-p", "--output-format", "stream-json", "--verbose",
               "--model", str(st["model"]), "--max-turns", str(int(st["max_turns"])),
               "--allowedTools", "Read,Glob,Grep,Bash",
               "--append-system-prompt-file", str(sys_file)]
        if st.get("effort"):
            cmd.extend(["--effort", str(st["effort"])])
        cmd.extend(["--", _user_prompt(hb_name, recent)])
        env = build_claude_env(config, config_dir)

        final_text, texts = "", []
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                cwd=project_dir or None, env=env, shell=IS_WINDOWS)
        deadline = time.time() + float(st["timeout_sec"])
        try:
            for raw in proc.stdout:
                if time.time() > deadline:
                    raise subprocess.TimeoutExpired(cmd, st["timeout_sec"])
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                et = ev.get("type")
                if et == "system" and ev.get("session_id"):
                    out["session_id"] = ev["session_id"]
                elif et == "assistant":
                    for b in (ev.get("message") or {}).get("content", []) or []:
                        if isinstance(b, dict) and b.get("type") == "text":
                            texts.append(b.get("text", ""))
                elif et == "result":
                    final_text = ev.get("result") or ""
                    out["usage"] = ev.get("usage") or {}
                    out["cost_usd"] = ev.get("total_cost_usd")
                    if ev.get("session_id"):
                        out["session_id"] = ev["session_id"]
                    if ev.get("subtype") and ev["subtype"] != "success":
                        out["error"] = str(ev["subtype"])
            proc.wait(timeout=max(5.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            proc.kill()
            out["error"] = "timeout"
        if not final_text:
            final_text = "\n".join(texts)
        final_text = _strip_ansi(final_text)
        work, reason = parse_verdict(final_text)
        if work is None:
            out["work"], out["reason"] = True, (reason or "판정 줄 없음 → 본 세션으로")
            out["error"] = out["error"] or "no_verdict"
        else:
            out["work"], out["reason"] = work, reason
        if out["error"] == "timeout":
            out["work"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        out["work"] = True
    out["duration_sec"] = round(time.time() - t0, 1)
    u = out.get("usage") or {}
    _append_ledger({
        "ts": datetime.now().isoformat(timespec="seconds"), "project": project, "run": run_no, "forced": forced,
        "work": out["work"], "reason": out["reason"], "error": out["error"], "model": st["model"],
        "input": u.get("input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
        "cache_read": u.get("cache_read_input_tokens"), "output": u.get("output_tokens"),
        "cost_usd": out["cost_usd"], "duration_sec": out["duration_sec"], "session_id": out["session_id"],
    })
    logger.info(f"[scout] {project} run#{run_no} → {'WORK' if out['work'] else 'quiet'}"
                f"{' (' + out['error'] + ')' if out['error'] else ''} {out['duration_sec']}s"
                f" in={u.get('input_tokens')} cw={u.get('cache_creation_input_tokens')} cr={u.get('cache_read_input_tokens')}"
                f" out={u.get('output_tokens')} — {out['reason'][:120]}")
    return out


def maybe_cleanup_transcripts(now: float | None = None) -> int:
    """하루 1회: 보존 기간이 지난 정찰 세션 전사(.jsonl)를 지운다. 지운 파일 수 반환.
    자리(seat) 폴더들이 projects/ 를 하드링크로 공유하므로 모든 자리 폴더에서 같은 session_id 를 찾아 지운다."""
    now = now or time.time()
    try:
        if _CLEANUP_STAMP.exists() and now - _CLEANUP_STAMP.stat().st_mtime < 24 * 3600:
            return 0
        _CLEANUP_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _CLEANUP_STAMP.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        keep_days = float(settings().get("transcript_keep_days") or 7)
        if keep_days <= 0 or not LEDGER_PATH.exists():
            return 0
        cutoff = now - keep_days * 86400
        sids = []
        for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["ts"]).timestamp()
                if ts < cutoff and e.get("session_id"):
                    sids.append(e["session_id"])
            except Exception:
                continue
        if not sids:
            return 0
        roots = [str(Path.home() / ".claude" / "projects")] + glob.glob(str(Path.home() / ".claude-seats" / "*" / "projects"))
        removed = 0
        for sid in set(sids):
            for root in roots:
                for p in glob.glob(os.path.join(root, "*", f"{sid}.jsonl")):
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass
        if removed:
            logger.info(f"[scout] cleaned {removed} old scout transcript file(s) (> {keep_days:g} days)")
        return removed
    except Exception as e:
        logger.warning(f"[scout] transcript cleanup failed: {e}")
        return 0
