"""외부 relay 가 실제 답 없이 끝났을 때 보낸 쪽에 알리는 자동 안내 (2026-11-23, marketer 이관).

무슨 구멍인가: 다른 유저의 에이전트가 보낸 글(`[외부 relay @핸들 · kind]`)이 받는 쪽 데몬의 고정 문구
(로그인 만료 · 계정 한도 · 미연결 · 월 턴 한도 등)로 끝나면, 그 문구는 받는 사람 채팅에만 남는다. 보낸 쪽 담당은
「전달됨(201)」만 받았으므로 답을 계속 기다린다 (실례 11/19: 받는 쪽 클라우드 데몬이 3초 만에 「클로드 로그인이
만료됐어요」로 끝났고, 보낸 담당은 40시간 넘게 기다렸다).

하는 일: 그 턴이 고정 문구로 끝났으면 보낸 핸들에게 `/api/relay/external` 로 안내 한 통을 돌려보낸다.
- 사유는 밝히지 않는다 — 로그인·한도 같은 계정 상태는 받는 사람의 것이다.
- 보낸 사람은 본문이 아니라 **서버가 채운 필드**(subtype="external" · from_username)로 정한다. 본문은 보낸 쪽이 쓰는
  글이라, 머리(`[외부 relay @…]`)나 묶음 칸막이를 흉내 내면 남의 핸들로 안내가 나갈 수 있다(11/23 코드 리뷰).
- 자동 안내에는 자동 안내로 답하지 않는다(NOTICE_MARK) — 양쪽이 다 막혀 있어도 서로 주고받지 않는다.
- 같은 (받는 사람, 보낸 핸들) 쌍에는 THROTTLE_SEC 에 한 번, 24시간에 DAILY_MAX 번까지 — 상대 비서가 안내에 다시 답해 와도
  몇 바퀴로 끝나고, 연결당 하루 한도(기본 50통, 양방향 합산)를 안내가 갉아먹지 않는다. 데몬 메모리에만 둔다.

맥 데몬(daemon/worker.py · daemon/claude_runner.py)과 클라우드 데몬(cloud_daemon.py)이 같이 쓴다.
usage_api.py 처럼 scripts/ 바로 아래에 두고 표준 라이브러리만 쓴다(cloud_daemon 은 daemon 패키지를 import 하지 않는다).
시험: python3 scripts/test_external_notice.py
"""
import re
import threading
import time

# 서버(/api/relay/external)가 붙이는 머리: `[외부 relay @핸들 · message]` · `… · handoff · 제안]` — 앞부분을 뽑을 때 벗길 뿐,
# 보낸 사람을 정하는 데는 쓰지 않는다
HEAD_RE = re.compile(r"\[외부 relay @([^\s\]]+)[^\]\n]*\]")
# 데몬 재시작 복구 머리말(dispatch.RESUME_NOTICE) — 외부 relay 머리 앞에 붙어 올 수 있다
_RESUME_RE = re.compile(r"^\s*\[안내:[^\]]*\]\s*")

NOTICE_MARK = "[자동 안내 · 회신 불요]"
# 이 머리로 시작하는 본문은 데몬이 보낸 글이다 — 여기에 다시 안내를 보내면 데몬끼리 왕복한다
_NO_NOTICE_BODY = ("[자동 안내", "[자가진단 결과 회신]")

THROTTLE_SEC = 3600   # 같은 쌍에 한 시간에 한 번
DAILY_MAX = 3         # 같은 쌍에 24시간에 세 번까지
_DAY_SEC = 86400
PREVIEW_CHARS = 60

_sent_at: dict[tuple, list[float]] = {}   # (받는 사람, 보낸 핸들) → 최근 24시간에 보낸 시각들
_lock = threading.Lock()
_tls = threading.local()


def _preview(body: str) -> str:
    """보낸 글의 첫 줄 앞부분 — 보낸 쪽이 어느 글인지 알아보게 한다(보낸 사람 자신의 글이라 새는 것이 없다)."""
    for line in (body or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line if len(line) <= PREVIEW_CHARS else line[:PREVIEW_CHARS] + "…"
    return ""


def _body(text: str) -> str:
    """복구 머리말과 서버 머리를 벗긴 본문."""
    t = _RESUME_RE.sub("", text or "", count=1).lstrip()
    m = HEAD_RE.match(t)
    return (t[m.end():] if m else t).lstrip()


def turn_parts(msg: dict) -> list[dict]:
    """이 턴이 소비한 메시지들을 판별에 필요한 칸만 복사해 돌려준다.
    묶음 턴(merged_ids)이면 디스패처가 남긴 칸별 원본(merged_parts)만 쓴다 — 합친 메시지는 첫 칸의
    subtype·from_username 을 물려받고 본문은 여러 사람 글이 섞여 있어 그대로 믿으면 안 된다.
    merged_parts 가 없는 묶음(옛 코드가 대기열에 넣어 둔 것)은 빈 목록 → 안내 없음."""
    if not isinstance(msg, dict):
        return []
    if msg.get("merged_ids"):
        parts = msg.get("merged_parts")
        src = parts if isinstance(parts, list) else []
    else:
        src = [msg]
    return [{"text": m.get("text"), "subtype": m.get("subtype"), "from_username": m.get("from_username")}
            for m in src if isinstance(m, dict)]


def external_senders(msgs) -> list[tuple[str, str]]:
    """이 턴의 메시지들 가운데 외부 relay 를 [(보낸 핸들, 보낸 글 앞부분)] 으로. 데몬이 보낸 자동 글은 뺀다.
    보낸 사람은 서버가 채운 subtype="external" · from_username 으로만 정한다(본문은 믿지 않는다).
    같은 핸들은 첫 글 하나만 남긴다."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in msgs or []:
        if not isinstance(m, dict) or m.get("subtype") != "external":
            continue
        handle = str(m.get("from_username") or "").strip().lstrip("@")
        if not handle:
            continue
        body = _body(m.get("text") or "")
        if body.startswith(_NO_NOTICE_BODY):
            continue
        if handle.lower() in seen:
            continue
        seen.add(handle.lower())
        out.append((handle, _preview(body)))
    return out


def notice_text(preview: str = "") -> str:
    lines = [
        f"{NOTICE_MARK} 보내신 글이 전달은 됐지만, 상대 비서가 지금 답할 수 없는 상태입니다. 급하면 상대에게 직접 연락해 주세요.",
        "- 이 글은 나중에 자동으로 다시 처리되지 않습니다. 필요하면 다시 보내 주세요.",
    ]
    if preview:
        lines.append(f"- 보낸 글 앞부분: 「{preview}」 — 보낸 담당이 이 대화가 아니면 그 담당에게 알려 주세요.")
    lines.append("- 피터보이스가 자동으로 보낸 안내입니다. 이 안내에는 답장하지 마세요(지금은 상대 비서가 읽지 못합니다). "
                 f"같은 상대에 대해서는 {THROTTLE_SEC // 60}분에 한 번, 하루 {DAILY_MAX}번까지만 갑니다.")
    return "\n".join(lines)


def _allow(owner, handle: str, now: float | None = None) -> bool:
    """(받는 사람, 보낸 핸들) 쌍에 THROTTLE_SEC 에 한 번, 24시간에 DAILY_MAX 번까지. 통과하면 지금 시각을 적는다."""
    now = time.time() if now is None else now
    key = (owner, handle.lower())
    with _lock:
        if len(_sent_at) > 500:   # 오래된 쌍 치우기
            for k in [k for k, v in _sent_at.items() if not v or now - v[-1] >= _DAY_SEC]:
                del _sent_at[k]
        recent = [t for t in _sent_at.get(key, []) if now - t < _DAY_SEC]
        if recent and now - recent[-1] < THROTTLE_SEC:
            _sent_at[key] = recent
            return False
        if len(recent) >= DAILY_MAX:
            _sent_at[key] = recent
            return False
        recent.append(now)
        _sent_at[key] = recent
        return True


def _release(owner, handle: str):
    """방금 적은 시각을 지운다(보내기 실패)."""
    with _lock:
        times = _sent_at.get((owner, handle.lower()))
        if times:
            times.pop()


def notify_dead_end(msgs, owner, send, now: float | None = None) -> list[tuple[str, bool]]:
    """고정 문구로 끝난 턴의 메시지들(turn_parts 결과) 가운데 외부 relay 가 있으면 보낸 쪽에 안내를 보낸다.
    owner: 받는 사람(클라우드는 user_id, 맥 데몬은 한 사람이라 아무 고정값).
    send(handle, notice) -> 성공 여부(참·거짓). 돌려주는 것: 실제로 보내려 한 [(핸들, 성공)] — 간격 제한에 걸린 핸들은 빠진다.
    보내기가 실패하면 간격 제한을 풀어 다음 글에서 다시 시도한다."""
    results: list[tuple[str, bool]] = []
    for handle, preview in external_senders(msgs):
        if not _allow(owner, handle, now):
            continue
        try:
            ok = bool(send(handle, notice_text(preview)))
        except Exception:
            ok = False
        if not ok:
            _release(owner, handle)
        results.append((handle, ok))
    return results


# ── 맥 데몬용: 이 스레드의 턴이 고정 문구로 끝났다는 표시 ────────────────────────
# run_claude 는 고정 문구를 보통 답과 같은 문자열로 돌려줘 워커가 구분하지 못한다. 러너가 그 자리에서 표시를 남기고
# 워커가 턴 뒤에 꺼낸다. 스레드별이라 같은 담당의 정찰·매니저 턴이 남긴 표시가 섞이지 않는다
# (run_claude 는 부른 스레드에서 끝까지 돌고, 재시도 재귀도 같은 스레드다).

def reset_dead_end():
    _tls.kind = None


def mark_dead_end(kind: str):
    _tls.kind = kind


def take_dead_end() -> str | None:
    kind = getattr(_tls, "kind", None)
    _tls.kind = None
    return kind
