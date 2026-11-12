"""턴 디스패처 — 워커 풀의 칸(max_concurrent)은 '실제로 도는 턴'만 쓴다 (2026-11-03).

배경: 전에는 폴링한 메시지를 전부 ThreadPoolExecutor 에 넣고, 스레드 안에서 담당(project) 잠금을 잡았다.
그래서 같은 담당에게 메시지가 몰리면 줄 선 메시지들이 칸을 하나씩 쥔 채 잠금을 기다렸다.
실측 11/03 12:14 (Sean 맥, 12칸): 실제 실행 3개 · 잠금 대기 9개(그중 6개가 한 담당에 몰린 자동 릴레이) ·
칸도 못 받은 대기 11개 → 사람 메시지가 16분을 기다렸다. 하루 실제 동시 실행은 평균 3.2개, 최대 7개였다.

규칙:
- 담당(project 키)마다 한 번에 한 턴. 그 담당의 다음 메시지는 스레드를 쥐지 않고 이 대기열에 남는다.
- 사람 메시지가 자동 메시지(릴레이·하트비트·stall-check 등)보다 먼저 간다 — 담당이 달라도, 같은 담당 안에서도.
  같은 종류끼리는 도착 순.
- 사람 예비 칸(reserve): 자동 턴은 동시에 (max_workers - reserve)개까지만 돈다. 사람 턴은 max_workers 까지.
  자동 턴이 칸을 다 채워도 사람 메시지는 바로 시작한다.

쌓인 자동 메시지 합치기 · 대체 (2026-11-12, Sean 「둘 다 해」 — revenue-lab 이관):
- 배경: 담당당 한 턴이라 「A 가 맡아」 뒤에 「A 말고 B」 를 보내도 A 는 첫 알림만 들고 한 턴을 다 쓴 뒤에야
  둘째를 봤다(11/09 부산진 중복 답 1건, 11/09~12 넘김 9건). 바로잡는 메시지는 구조상 늘 늦었다.
- 합치기: 자동 턴을 시작할 때 같은 담당에게 대기 중인 **합칠 수 있는 자동 메시지**(릴레이·알림 — MERGE_PREFIXES)가
  더 있으면 도착순으로 한 통에 묶어 한 턴으로 돈다(구분선 + 각 통의 #id·시각). 「맡김」과 「취소」를 같은 턴에 보고
  턴 수도 준다. 하트비트는 「할 일 없음」 조용한 회차 판정이 입력 1건 기준이라 묶지 않고, [매니저] 는 자기 1문1답
  흐름이 있어 묶지 않는다. 첨부·댓글(reply_to)·암호화 메시지도 따로 돈다. 사람 메시지는 절대 묶지 않는다.
  config `merge_auto_messages`(기본 true) · `merge_auto_max`(기본 10통).
- 대체: 릴레이 metadata.supersede 키(예: "room:241574666")가 붙은 메시지가 들어오면, 같은 담당 대기열에 있는
  같은 키의 옛 메시지는 빼고 on_superseded 콜백(워커: 처리완료 마킹)으로 넘긴다. 서버(/api/relay/message)도
  아직 안 집어간 옛 메시지를 같은 규칙으로 닫으므로, 데몬은 '이미 받아 와 줄 서 있는' 것만 맡는다.
  이미 시작된 턴은 어느 쪽도 막지 못한다 — 보내는 쪽 관문(revenue-lab 「담당 확인」)과 같이 쓴다.
"""
import itertools
import threading
import time
from datetime import datetime, timedelta, timezone

# 사람이 친 메시지가 아닌 것 — 본문 머리말로 판별한다 (서버가 종류 필드를 따로 주지 않는다).
AUTO_PREFIXES = ("[relay from:", "[heartbeat]", "[stall-check", "[외부 relay", "[자료수신", "[매니저]")
# 한 턴에 묶을 수 있는 자동 메시지 — 릴레이·알림만 (하트비트·매니저 제외, 위 모듈 설명 참조)
MERGE_PREFIXES = ("[relay from:", "[외부 relay", "[자료수신", "[stall-check")
MERGE_MAX_DEFAULT = 10
MERGE_MAX_CHARS = 60_000   # 묶음 본문 상한 — 넘치면 나머지는 다음 턴
_KST = timezone(timedelta(hours=9))


def is_automatic(msg: dict) -> bool:
    text = (msg.get("text") or "").lstrip()
    return text.startswith(AUTO_PREFIXES)


def is_mergeable(msg: dict) -> bool:
    """같은 담당의 다른 자동 메시지와 한 턴에 묶어도 되는 메시지인가."""
    text = (msg.get("text") or "").lstrip()
    if not text.startswith(MERGE_PREFIXES):
        return False
    if msg.get("files") or msg.get("reply_to") or msg.get("encrypted"):
        return False
    return True


def supersede_key(msg: dict) -> str | None:
    """릴레이 metadata.supersede — 같은 담당·같은 키의 옛 대기 메시지를 대체한다."""
    meta = msg.get("metadata")
    if not isinstance(meta, dict):
        return None
    key = meta.get("supersede")
    return key.strip() if isinstance(key, str) and key.strip() else None


def turn_message_ids(msg: dict) -> list:
    """이 턴이 소비하는 메시지 id 전부 — 묶음이면 merged_ids, 아니면 [id]."""
    ids = msg.get("merged_ids")
    return list(ids) if ids else [msg.get("id")]


def _fmt_kst(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_KST).strftime("%m/%d %H:%M")
    except ValueError:
        return str(iso)[:16]


def merge_messages(msgs: list[dict]) -> dict:
    """도착순 자동 메시지 2건 이상을 한 턴용 메시지 하나로 합친다.
    id·project 등은 첫 메시지의 것을 쓰고, merged_ids 에 전부를 둔다(워커가 처리완료·reply_to 에 쓴다)."""
    n = len(msgs)
    parts = [
        f"[자동 메시지 {n}건 묶음] 같은 담당에게 쌓인 자동 메시지(릴레이·알림)를 도착순으로 한 턴에 넣었습니다. "
        f"뒤의 것이 앞의 것을 바꾸거나 거둘 수 있으니 {n}건을 전부 읽은 뒤 처리하고, 답은 한 번에 합니다."
    ]
    for i, m in enumerate(msgs, 1):
        when = _fmt_kst(m.get("created_at"))
        head = f"━━━ {i}/{n} · #{m.get('id')}" + (f" · {when}" if when else "") + " ━━━"
        parts.append(f"{head}\n{(m.get('text') or '').strip()}")
    merged = dict(msgs[0])
    merged["text"] = "\n\n".join(parts)
    merged["merged_ids"] = [m.get("id") for m in msgs]
    merged["merged_count"] = n
    return merged


class _Item:
    __slots__ = ("seq", "msg", "project", "auto", "enqueued_at")

    def __init__(self, seq, msg, project, auto):
        self.seq = seq
        self.msg = msg
        self.project = project
        self.auto = auto
        self.enqueued_at = time.monotonic()


class TurnDispatcher:
    def __init__(self, max_workers: int, reserve: int, run_turn, submit, logger=None, shutdown_event=None,
                 merge_enabled: bool = True, merge_max: int = MERGE_MAX_DEFAULT, on_superseded=None):
        self.max_workers = max(1, int(max_workers))
        # 자동 턴 칸은 최소 1개는 남긴다 (reserve 가 커도 하트비트·릴레이가 영영 안 도는 일은 없게)
        self.reserve = max(0, min(int(reserve or 0), self.max_workers - 1))
        self._run_turn = run_turn      # 턴 본체 (worker._process_message_safe)
        self._submit = submit          # 실행기 submit (ThreadPoolExecutor.submit)
        self._log = logger
        self._shutdown = shutdown_event or threading.Event()
        self.merge_enabled = bool(merge_enabled)
        self.merge_max = max(1, int(merge_max or MERGE_MAX_DEFAULT))
        self._on_superseded = on_superseded   # (old_msg, new_msg) — 워커가 처리완료 마킹
        self._cv = threading.Condition()
        self._pending: list[_Item] = []
        self._running_projects: set[str] = set()
        self._running = 0
        self._auto_running = 0
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._merged_turns = 0
        self._merged_msgs = 0
        self._superseded = 0

    # ── 바깥 API ──
    def enqueue(self, msg: dict, project: str | None = None):
        project = project or msg.get("project") or "general"
        item = _Item(next(self._seq), msg, project, is_automatic(msg))
        key = supersede_key(msg) if item.auto else None
        dropped: list[_Item] = []
        with self._cv:
            if key:
                # 같은 담당 대기열의 같은 키 옛 메시지는 빼낸다 (이미 도는 턴은 못 막는다)
                for it in list(self._pending):
                    if it.project == project and it.auto and supersede_key(it.msg) == key:
                        self._pending.remove(it)
                        dropped.append(it)
                self._superseded += len(dropped)
            self._pending.append(item)
            self._cv.notify()
        for it in dropped:
            if self._log:
                self._log.info(f"[queue] msg #{it.msg.get('id')} → {project} dropped: superseded by "
                               f"#{msg.get('id')} (key {key})")
            if self._on_superseded:
                try:
                    self._on_superseded(it.msg, msg)
                except Exception as e:  # 콜백 실패가 대기열을 멈추면 안 된다
                    if self._log:
                        self._log.error(f"[queue] on_superseded failed for #{it.msg.get('id')}: {e}")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="dispatch")
            self._thread.start()

    def stop(self):
        with self._cv:
            self._cv.notify_all()

    def stats(self) -> dict:
        with self._cv:
            return {
                "running": self._running,
                "auto_running": self._auto_running,
                "pending": len(self._pending),
                "pending_projects": len({it.project for it in self._pending}),
                "max_workers": self.max_workers,
                "reserve": self.reserve,
                "merged_turns": self._merged_turns,
                "merged_msgs": self._merged_msgs,
                "superseded": self._superseded,
            }

    # ── 내부 ──
    def _pick(self):
        """cv 를 쥔 채 호출. 지금 시작할 수 있는 것 중 (사람 먼저, 그 다음 도착 순) 하나를 고른다."""
        if self._running >= self.max_workers:
            return None
        auto_cap = self.max_workers - self.reserve
        best = None
        for it in self._pending:
            if it.project in self._running_projects:
                continue
            if it.auto and self._auto_running >= auto_cap:
                continue
            key = (1 if it.auto else 0, it.seq)
            if best is None or key < best[0]:
                best = (key, it)
        return best[1] if best else None

    def _collect_merge(self, item: _Item) -> list[_Item]:
        """cv 를 쥔 채 호출. item(자동·합침 가능)과 같은 담당의 합칠 수 있는 대기 메시지를 도착순으로 뽑아 대기열에서 뺀다.
        _pick 이 자동 메시지를 골랐다는 것은 그 담당에 대기 중인 사람 메시지가 없다는 뜻이라(사람 먼저), 사람을 앞지르지 않는다."""
        if not (self.merge_enabled and item.auto and is_mergeable(item.msg)):
            return []
        picked: list[_Item] = []
        total = len(item.msg.get("text") or "")
        for it in sorted(self._pending, key=lambda x: x.seq):
            if len(picked) + 1 >= self.merge_max:
                break
            if it.project != item.project or not it.auto or not is_mergeable(it.msg):
                continue
            total += len(it.msg.get("text") or "")
            if total > MERGE_MAX_CHARS:
                break
            picked.append(it)
        for it in picked:
            self._pending.remove(it)
        return picked

    def _loop(self):
        while not self._shutdown.is_set():
            with self._cv:
                item = self._pick()
                if item is None:
                    self._cv.wait(timeout=1.0)
                    continue
                self._pending.remove(item)
                extra = self._collect_merge(item)
                if extra:
                    item.msg = merge_messages([item.msg] + [it.msg for it in extra])
                    self._merged_turns += 1
                    self._merged_msgs += 1 + len(extra)
                self._running_projects.add(item.project)
                self._running += 1
                if item.auto:
                    self._auto_running += 1
                running, auto_running, pending = self._running, self._auto_running, len(self._pending)
            waited = time.monotonic() - item.enqueued_at
            if self._log and extra:
                ids = ",".join(f"#{i}" for i in item.msg.get("merged_ids") or [])
                self._log.info(f"[queue] merged {1 + len(extra)} auto msgs → {item.project}: {ids} "
                               f"(one turn; pending {pending})")
            if self._log and waited > 5:
                self._log.info(
                    f"[queue] msg #{item.msg.get('id')} → {item.project} started after {waited:.0f}s wait "
                    f"(running {running}/{self.max_workers}, auto {auto_running}, pending {pending})")
            try:
                self._submit(self._run, item)
            except RuntimeError:
                # 실행기가 이미 닫힘(종료 중) — 메시지는 서버에 미처리로 남아 재시작 뒤 다시 온다
                self._release(item)
                break

    def _run(self, item: _Item):
        try:
            self._run_turn(item.msg)
        finally:
            self._release(item)

    def _release(self, item: _Item):
        with self._cv:
            self._running_projects.discard(item.project)
            self._running -= 1
            if item.auto:
                self._auto_running -= 1
            self._cv.notify()
