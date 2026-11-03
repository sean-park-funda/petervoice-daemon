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
"""
import itertools
import threading
import time

# 사람이 친 메시지가 아닌 것 — 본문 머리말로 판별한다 (서버가 종류 필드를 따로 주지 않는다).
AUTO_PREFIXES = ("[relay from:", "[heartbeat]", "[stall-check", "[외부 relay", "[자료수신", "[매니저]")


def is_automatic(msg: dict) -> bool:
    text = (msg.get("text") or "").lstrip()
    return text.startswith(AUTO_PREFIXES)


class _Item:
    __slots__ = ("seq", "msg", "project", "auto", "enqueued_at")

    def __init__(self, seq, msg, project, auto):
        self.seq = seq
        self.msg = msg
        self.project = project
        self.auto = auto
        self.enqueued_at = time.monotonic()


class TurnDispatcher:
    def __init__(self, max_workers: int, reserve: int, run_turn, submit, logger=None, shutdown_event=None):
        self.max_workers = max(1, int(max_workers))
        # 자동 턴 칸은 최소 1개는 남긴다 (reserve 가 커도 하트비트·릴레이가 영영 안 도는 일은 없게)
        self.reserve = max(0, min(int(reserve or 0), self.max_workers - 1))
        self._run_turn = run_turn      # 턴 본체 (worker._process_message_safe)
        self._submit = submit          # 실행기 submit (ThreadPoolExecutor.submit)
        self._log = logger
        self._shutdown = shutdown_event or threading.Event()
        self._cv = threading.Condition()
        self._pending: list[_Item] = []
        self._running_projects: set[str] = set()
        self._running = 0
        self._auto_running = 0
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None

    # ── 바깥 API ──
    def enqueue(self, msg: dict, project: str | None = None):
        project = project or msg.get("project") or "general"
        item = _Item(next(self._seq), msg, project, is_automatic(msg))
        with self._cv:
            self._pending.append(item)
            self._cv.notify()

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

    def _loop(self):
        while not self._shutdown.is_set():
            with self._cv:
                item = self._pick()
                if item is None:
                    self._cv.wait(timeout=1.0)
                    continue
                self._pending.remove(item)
                self._running_projects.add(item.project)
                self._running += 1
                if item.auto:
                    self._auto_running += 1
                running, auto_running, pending = self._running, self._auto_running, len(self._pending)
            waited = time.monotonic() - item.enqueued_at
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
