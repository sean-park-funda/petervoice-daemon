"""Claude 재로그인 셀프서비스 엔진 (macOS 전용).

토큰 만료/로그아웃으로 데몬이 401을 맞으면, 채팅 파이프는 살아있다는 점을 이용해
`claude auth login`을 데몬 자식 프로세스(pty)로 띄우고, 인증 URL을 채팅으로 안내한 뒤
유저가 붙여넣은 코드를 로그인 프로세스 stdin에 주입한다.

핵심 원칙 (과거 사고 반영):
- 기존 credentials/Keychain 을 읽거나 지우거나 고치지 않는다. 정식 `claude auth login`
  플로우만 실행한다 (2026-06-20 사고 원칙: daemon-never-touch-credentials).
- URL/코드는 제어문자를 정제한다 (과거 \r 사고).
- 코드는 검증 즉시 메모리에서 소거하고, 로그에 절대 남기지 않는다.

의존성: 표준 라이브러리 pty만 사용 (pexpect 불필요). macOS 전용 1차 릴리스.
Windows(NSSM/jenn)는 pty 미지원 → 기존 수동 방식 유지.

상태머신: idle → pending_url → waiting_code → verifying → done/failed
"""

import os
import re
import pty
import time
import select
import signal
import threading

import daemon.globals as g
from daemon.globals import config, logger, CLAUDE_CMD
from daemon.api import api_request


# ── 상수 ────────────────────────────────────────────────────────
SESSION_TTL_SEC = 600            # 10분 내 코드 미수신 시 폐기
URL_CAPTURE_TIMEOUT_SEC = 45     # login 프로세스 기동 후 URL 캡처 대기
VERIFY_TIMEOUT_SEC = 60          # 코드 주입 후 성공/실패 판정 대기

# OAuth 인증 URL: claude.ai / claude.com / console.anthropic.com 계열 모두 허용
_URL_RE = re.compile(r"https?://[^\s\x00-\x1f\"']+")
# 유저가 붙여넣는 코드: <code>#<state> (state는 URL의 state와 대조)
CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{20,}#[A-Za-z0-9_\-]{8,}$")
# URL 안의 state 파라미터
_STATE_RE = re.compile(r"[?&]state=([^&\s]+)")

# 온보딩(테마 선택 등) 화면으로 추정되는 마커 — URL 전에 나오면 Enter로 진행
_ONBOARD_MARKERS = ("choose", "theme", "select", "text style", "press enter", "dark mode", "light mode")


def _norm_dir(d) -> str:
    """CLAUDE_CONFIG_DIR 비교용 정규화 ("" = 기본 ~/.claude)."""
    return os.path.expanduser(d).rstrip("/") if d else ""


def expected_email(config_dir: str = None) -> str | None:
    """이 폴더(자리)에 로그인돼야 할 계정 이메일. config.json 의 claude_seats 에 {"config_dir", "email"} 이 있는
    머신(Sean 맥미니)만 값이 있고, 고객 머신은 늘 None 이라 동작이 그대로다.
    2026-10-30: 「재로그인」이 지금 자리(claude2)에 로그인할 때 승인 브라우저가 sean@vibement.kr 이라 claude2 자리가
    조용히 다른 계정이 됐다 → 로그인 화면에 맞는 계정을 미리 채우고(--email), 끝나면 대조한다."""
    try:
        from daemon.limits import _live_seat_settings
        _, seats = _live_seat_settings()
    except Exception:
        return None
    want = _norm_dir(config_dir)
    for spec in (seats or {}).values():
        if isinstance(spec, dict) and spec.get("email") and _norm_dir(spec.get("config_dir")) == want:
            return spec["email"]
    return None


def _logged_in_email(config_dir: str = None) -> str | None:
    """이 폴더에 지금 로그인된 계정 이메일 (`claude auth status` JSON → 실패 시 .claude.json). 자격증명은 읽지 않는다."""
    import json
    import subprocess
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CONFIG_DIR")}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    try:
        r = subprocess.run([CLAUDE_CMD, "auth", "status"], capture_output=True, text=True, timeout=15, env=env)
        email = (json.loads(r.stdout or "{}") or {}).get("email")
        if email:
            return email
    except Exception:
        pass
    path = os.path.join(_norm_dir(config_dir), ".claude.json") if config_dir else os.path.expanduser("~/.claude.json")
    try:
        with open(path, encoding="utf-8") as f:
            return ((json.load(f) or {}).get("oauthAccount") or {}).get("emailAddress")
    except Exception:
        return None


def _sanitize(s: str) -> str:
    """제어문자(\r \x1b 등) 제거. URL/코드 정제 필수 (과거 \r 사고)."""
    # OSC 시퀀스(터미널 하이퍼링크 ESC]8;;URL ST …)를 먼저 통째로 제거한다.
    # 아래 CSI 제거만 하면 ESC 만 빠지고 "]8;;<URL>" 이 본문에 남아 보이는 URL 과
    # 붙어버린다 → 링크가 두 번 이어진 깨진 URL 이 고객에게 그대로 나갔다
    # (2026-09-21 신세호 님: 이 링크로 재로그인 7회 연속 실패).
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", s)
    # 아직 끝나지 않은(잘려 들어온) OSC 꼬리도 버린다 — pty 는 조각으로 읽힌다
    s = re.sub(r"\x1b\][^\x07\x1b]*$", "", s)
    # ANSI escape 시퀀스 제거
    s = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)
    # 그 외 제어문자 제거
    s = "".join(ch for ch in s if ch == "\n" or (ord(ch) >= 0x20 and ord(ch) != 0x7f))
    return s.strip()


def _trim_url(cand: str) -> str:
    """캡처한 URL 에서 뒤에 붙은 잔재를 잘라낸다 (2차 방어).

    _sanitize 가 놓친 OSC 잔재(`]8;;`)나, 같은 URL 이 한 번 더 이어붙은 경우를
    자른다. 고객은 이 문자열을 그대로 브라우저에 붙여넣으므로 한 글자라도 더
    붙으면 인증이 실패한다."""
    for marker in ("\x1b]", "]8;;", "]8;"):
        i = cand.find(marker)
        if i > 0:
            cand = cand[:i]
    # 같은 URL 이 한 번 더 이어붙은 경우만 자른다. "http" 문자열이 아니라 스킴
    # 패턴으로 찾아야 한다 — redirect_uri 안의 https%3A%2F%2F 는 잘라내면 안 된다.
    m2 = re.search(r"https?://", cand[4:])
    if m2:
        cand = cand[:4 + m2.start()]
    return cand.rstrip(").,")


# 로그인 트리거 — 공백·대소문자·문장부호를 무시하고 매칭 (처음 쓰는 유저가 "재로그인"을
# 떠올리기 어려워 "로그인", "클로드 로그인", "클로드로그인" 등 자연스러운 표현을 모두 받는다)
_LOGIN_TRIGGERS = {
    "재로그인", "리로그인", "로그인", "relogin", "login", "/relogin", "/login",
    "클로드로그인", "클로드재로그인", "클로드연결", "클로드계정연결", "계정연결", "연결",
    "클로드로그인해줘", "로그인해줘", "재로그인해줘", "로그인하기", "클로드인증", "인증",
    "claude로그인", "claudelogin",
}


def is_login_trigger(text: str) -> bool:
    """유저 입력이 클로드 로그인 시작 요청인지. 공백/대소문자/끝문장부호 무시."""
    if not text:
        return False
    s = re.sub(r"[\s​]+", "", text).strip().lower()
    s = s.rstrip(".!?~,")
    return s in _LOGIN_TRIGGERS


def extract_code(text: str) -> str | None:
    """붙여넣은 텍스트에서 재로그인 코드(<code>#<state>)만 추출.

    Claude 인증 코드 화면이 코드 뒤에 OAuth URL 을 바로 붙여서 보여주므로
    (예: `abc#state` + `https://claude.com/...`) 유저가 통째로 붙여넣기 쉽다.
    https:// 이후를 잘라내고 코드만 뽑는다. 코드/state 문자는 모두 영숫자라
    URL 의 `://` (콜론) 을 유일한 경계로 삼는다. 못 찾으면 None."""
    if not text:
        return None
    s = _sanitize(text)
    m = re.search(r"https?://", s)
    if m:
        s = s[:m.start()].strip()
    if CODE_RE.match(s):
        return s
    # 코드 앞뒤에 다른 텍스트가 섞여도 패턴을 중간에서 찾아본다
    m2 = re.search(r"[A-Za-z0-9_\-]{20,}#[A-Za-z0-9_\-]{8,}", s)
    return m2.group(0) if m2 else None


class ReloginSession:
    """유저당 1개의 재로그인 세션. 데몬은 단일 유저를 서비스하므로 모듈 싱글턴."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.state = "idle"            # idle|pending_url|waiting_code|verifying|done|failed
        self.project = None            # 안내/결과를 보낼 채팅 프로젝트
        self.url = None                # 캡처한 인증 URL
        self.oauth_state = None        # URL의 state 파라미터 (코드 대조용)
        self.started_at = 0.0
        self.error = None
        self._pid = None               # login 프로세스 pid
        self._fd = None                # pty master fd
        self._thread = None
        self._code_event = threading.Event()   # submit_code 가 set
        self._code_value = None                 # 주입할 코드 (일시 보관, 사용 후 즉시 소거)
        self._on_event = None                   # 설정 UI 모드 콜백 (None=채팅 모드)
        self._config_dir = None                 # 로그인 대상 계정의 CLAUDE_CONFIG_DIR (default=None)
        self._expected_email = None             # 그 자리에 로그인돼야 할 계정 (claude_seats 가 있는 머신만)

    # ── 채팅 안내 ────────────────────────────────────────────
    def _reply(self, text: str, project: str = None):
        api_key = config.get("api_key", "")
        if not api_key:
            return
        try:
            api_request(api_key, "POST", "/api/bot/reply", {
                "text": text,
                "project": project or self.project or "general",
                "is_final": True,
            })
        except Exception as e:
            logger.warning(f"[relogin] reply failed: {e}")

    # ── 상태 조회 ────────────────────────────────────────────
    def is_pending(self) -> bool:
        """코드 입력을 기다리는 중인가? (worker 가 코드 가로채기 여부 판단)"""
        with self.lock:
            if self.state != "waiting_code":
                return False
            if time.time() - self.started_at > SESSION_TTL_SEC:
                return False
            return True

    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "url": self.url, "project": self.project, "error": self.error}

    # ── 이벤트 발행 (채팅 vs 설정UI 라우팅) ────────────────────
    # on_event 콜백이 설정되면 설정 UI 모드(URL/결과를 콜백으로) — 채팅에는 안 보냄.
    # 없으면 기존 채팅 모드(카나리 검증 완료) 그대로.
    def _publish_url(self):
        if self._on_event:
            try:
                self._on_event({"type": "url_ready", "url": self.url})
            except Exception as e:
                logger.warning(f"[relogin] on_event(url) failed: {e}")
        else:
            self._reply(self._url_message())

    def _publish_result(self, ok: bool, warning: str = None):
        if ok:
            # 재로그인 직후 배너를 새 계정 값으로 즉시 갱신하고, 한도 쿨다운도 재판정한다
            # (다른 계정으로 갈아탔을 때 "배너는 새 계정인데 채팅은 막힘" 방지). API 프로브라 1초 내.
            def _refresh():
                try:
                    from daemon.heartbeat import collect_and_store_usage  # 순환 import 회피
                    collect_and_store_usage()
                except Exception as e:
                    logger.warning(f"[relogin] usage refresh after login failed: {e}")
            threading.Thread(target=_refresh, daemon=True, name="usage-after-login").start()
        if self._on_event:
            try:
                self._on_event({"type": "done" if ok else "failed", **({"warning": warning} if warning else {})})
            except Exception as e:
                logger.warning(f"[relogin] on_event(result) failed: {e}")
            if warning:   # 설정 화면은 결과 상태만 받는다 — 계정이 엇갈린 건 채팅으로도 알린다
                self._reply(warning)
        else:
            self._reply(
                warning if ok and warning else
                "✅ 재로그인 완료! 이제 정상적으로 사용하실 수 있어요." if ok else
                "❌ 재로그인에 실패했어요. 코드가 정확한지 확인 후, '클로드 로그인' 이라고 입력해 다시 시도해 주세요."
            )

    # ── 시작 ─────────────────────────────────────────────────
    def start(self, project: str, config_dir: str = None, on_event=None) -> dict:
        """재로그인 플로우 시작. 이미 진행 중이면 현재 상태를 반환(재안내).
        on_event: 설정 UI 모드용 콜백. 지정 시 URL/결과를 채팅 대신 콜백으로 전달."""
        with self.lock:
            active = self.state in ("pending_url", "waiting_code", "verifying")
            fresh = time.time() - self.started_at <= SESSION_TTL_SEC
            if active and fresh:
                # 이미 진행 중 — 채팅 모드면 URL 재안내, 설정 모드면 상태만 반환
                if not on_event:
                    if self.state == "waiting_code" and self.url:
                        self._reply(self._url_message(), project)
                    else:
                        self._reply("재로그인을 준비하고 있어요. 잠시만요...", project)
                return {"state": self.state, "already": True, "url": self.url}

            # 새 세션 시작
            self.reset()
            self.state = "pending_url"
            self.project = project
            self._config_dir = config_dir
            self._expected_email = expected_email(config_dir)
            self._on_event = on_event
            self.started_at = time.time()
            self._thread = threading.Thread(
                target=self._run_flow, args=(config_dir,), daemon=True, name="relogin-flow"
            )
            self._thread.start()
            return {"state": self.state, "started": True}

    # ── 코드 제출 ────────────────────────────────────────────
    def submit_code(self, code: str) -> dict:
        """유저가 붙여넣은 코드를 검증 후 로그인 프로세스에 주입.
        코드 뒤에 붙는 OAuth URL 은 extract_code 가 잘라낸다."""
        code = extract_code(code)
        with self.lock:
            if self.state != "waiting_code":
                return {"ok": False, "reason": "no_pending"}
            if time.time() - self.started_at > SESSION_TTL_SEC:
                self._fail_locked("timeout")
                return {"ok": False, "reason": "timeout"}
            if not code or not CODE_RE.match(code):
                return {"ok": False, "reason": "bad_format"}
            # 참고: 코드의 `#뒷부분`은 URL 의 state= 파라미터와 값이 달라서
            # (Claude 코드 화면이 돌려주는 값 ≠ authorize URL state) 사전 대조는 하지 않는다.
            # 실제 state/PKCE 검증은 claude auth login 프로세스가 서버측에서 수행하므로,
            # 잘못된 코드는 로그인 자체가 실패로 판정된다. (2026-07-08 오탐 제거)
            if self.oauth_state and "#" in code:
                code_state = code.rsplit("#", 1)[1]
                if code_state != self.oauth_state:
                    logger.info("[relogin] code state differs from URL state (expected; letting CLI verify)")
            # 코드는 로그에 남기지 않는다
            self._code_value = code
            self.state = "verifying"
            self._code_event.set()
            return {"ok": True}

    # ── 내부: 로그인 플로우 ─────────────────────────────────
    def _url_message(self) -> str:
        who = (f"**{self._expected_email}** 계정으로 로그인해 주세요 (이 칸은 그 계정용입니다. "
               "브라우저가 다른 계정으로 로그인돼 있으면 먼저 바꿔 주세요):\n") if self._expected_email else \
            "본인 Claude 계정으로 로그인해 주세요:\n"
        return (
            "⚠️ Claude 인증이 만료되었습니다. 재로그인이 필요해요.\n\n"
            "1) 아래 링크를 눌러 " + who +
            f"{self.url}\n\n"
            "2) 로그인 후 나오는 코드를 이 채팅에 그대로 붙여넣어 주세요.\n"
            "(10분 안에 붙여넣지 않으면 만료됩니다. 다시 하려면 '클로드 로그인' 이라고 입력하세요.)"
        )

    def _run_flow(self, config_dir: str = None):
        try:
            self._spawn_and_capture_url(config_dir)
        except Exception as e:
            logger.error(f"[relogin] flow error: {e}", exc_info=True)
            with self.lock:
                self._fail_locked(f"internal: {e}")

    def _spawn_and_capture_url(self, config_dir: str = None):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["LANG"] = "en_US.UTF-8"
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)

        pid, fd = pty.fork()
        if pid == 0:
            # child: exec claude auth login
            try:
                os.execvpe(CLAUDE_CMD, [CLAUDE_CMD, "auth", "login", "--claudeai",
                                        *(["--email", self._expected_email] if self._expected_email else [])], env)
            except Exception:
                os._exit(127)

        # parent
        with self.lock:
            self._pid = pid
            self._fd = fd

        buf = ""
        url = None
        enter_sent = 0
        last_data = time.time()
        deadline = time.time() + URL_CAPTURE_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            got_data = eof = False
            if fd in r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    buf += chunk.decode("utf-8", errors="replace")
                    got_data, last_data = True, time.time()
                else:
                    eof = True
            clean = _sanitize(buf)
            # URL 캡처. pty 는 조각으로 읽힌다(CLI 가 1024바이트씩 쓴다) — 버퍼 끝에서 끝나는 URL 은 잘린 조각일 수 있어
            # 뒤에 글자가 붙거나, 1.5초 조용하거나, 출력이 끝날 때까지 기다린다. (2026-10-30: --email 로 URL 이 길어지자
            # 첫 조각이 login_hint=claude2% 에서 끊겨 깨진 링크를 잡았다. 그전엔 URL 이 첫 조각에 겨우 들어가 드러나지 않았다)
            for m in _URL_RE.finditer(clean):
                cand = _trim_url(m.group(0))
                low = cand.lower()
                if ("oauth" in low or "authorize" in low or "/login" in low
                        or "code=" in low or "claude.ai" in low or "claude.com" in low):
                    if m.end() < len(clean) or eof or time.time() - last_data >= 1.5:
                        url = cand
                    break
            if url or eof:
                break
            # 온보딩 화면이면 Enter 진행 (URL 나올 때까지 최대 2회)
            low = clean.lower()
            if got_data and enter_sent < 2 and any(mk in low for mk in _ONBOARD_MARKERS):
                try:
                    os.write(fd, b"\r")
                except OSError:
                    pass
                enter_sent += 1
                time.sleep(0.5)

        if not url:
            logger.warning("[relogin] URL capture timed out")
            self._kill_proc()
            with self.lock:
                self._fail_locked("url_capture_timeout")
            self._publish_result(False)
            return

        oauth_state = None
        sm = _STATE_RE.search(url)
        if sm:
            oauth_state = sm.group(1)

        with self.lock:
            self.url = url
            self.oauth_state = oauth_state
            self.state = "waiting_code"
            self.started_at = time.time()   # 코드 대기 TTL 리셋
        logger.info("[relogin] URL captured, waiting for code")
        self._publish_url()

        # 코드 대기 → 주입 → 검증
        self._wait_and_verify(fd)

    def _wait_and_verify(self, fd: int):
        got = self._code_event.wait(timeout=SESSION_TTL_SEC)
        if not got:
            logger.info("[relogin] code wait timed out")
            self._kill_proc()
            with self.lock:
                self._fail_locked("code_timeout")
            self._publish_result(False)
            return

        # 코드 주입 (사용 즉시 소거)
        with self.lock:
            code = self._code_value
            self._code_value = None
        if not code:
            with self.lock:
                self._fail_locked("no_code")
            return
        try:
            os.write(fd, code.encode("utf-8") + b"\r")
        except OSError as e:
            logger.warning(f"[relogin] code inject failed: {e}")
            self._kill_proc()
            with self.lock:
                self._fail_locked("inject_failed")
            self._publish_result(False)
            return
        finally:
            code = None   # 지역 변수 소거

        # 성공/실패 판정
        buf = ""
        success = False
        deadline = time.time() + VERIFY_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                r, _, _ = select.select([fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if fd in r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += _sanitize(chunk.decode("utf-8", errors="replace")).lower()
                if ("login successful" in buf or "logged in" in buf
                        or "successfully" in buf or "you are logged in" in buf):
                    success = True
                    break
                if "invalid" in buf or "error" in buf or "failed" in buf or "expired" in buf:
                    break

        # 프로세스 종료 대기
        exit_ok = self._wait_proc(timeout=10)
        # claude auth status 로 최종 확인 (거짓보고 가능성 있으나 성공쪽 참고용)
        if not success and exit_ok:
            success = self._verify_status()

        if success:
            with self.lock:
                self.state = "done"
            logger.info("[relogin] login successful")
            self._publish_result(True, self._account_warning())
        else:
            with self.lock:
                self._fail_locked("verify_failed")
            logger.warning("[relogin] login verification failed")
            self._publish_result(False)

    def _account_warning(self) -> str | None:
        """기대 계정이 있는 자리면 들어온 계정을 대조해, 다르면 경고문. 토큰은 이미 저장됐으므로(자격증명은 건드리지 않는다)
        성공 대신 경고로 알린다 — 이 자리로 도는 대화가 다른 계정 몫을 쓰게 되기 때문."""
        want = self._expected_email
        if not want:
            return None
        got = _logged_in_email(self._config_dir)
        if not got or got.lower() == want.lower():
            return None
        logger.warning(f"[relogin] account mismatch: seat expects {want}, got {got}")
        return (f"⚠️ 로그인은 됐지만 **{got}** 계정이 들어왔어요. 이 칸은 **{want}** 용이라, 지금부터 이 칸으로 "
                f"도는 대화가 {got} 의 사용량을 씁니다.\n"
                f"브라우저를 {want} 로 바꾼 뒤 '재로그인' 이라고 입력해 다시 해 주세요.")

    def _verify_status(self) -> bool:
        import subprocess
        try:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            # 로그인을 띄운 계정(config_dir)과 같은 곳을 확인해야 한다.
            # (없으면 default 계정 상태를 보고 오판정 — 멀티계정 버그)
            cfg = getattr(self, "_config_dir", None)
            if cfg:
                env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(cfg)
            r = subprocess.run(
                [CLAUDE_CMD, "auth", "status"],
                capture_output=True, text=True, timeout=15, env=env,
            )
            out = (r.stdout + r.stderr).lower()
            return "logged in" in out or "loggedin" in out or "authenticated" in out
        except Exception:
            return False

    # ── 프로세스 정리 ────────────────────────────────────────
    def _kill_proc(self):
        with self.lock:
            pid, fd = self._pid, self._fd
            self._pid, self._fd = None, None
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _wait_proc(self, timeout: int = 10) -> bool:
        with self.lock:
            pid, fd = self._pid, self._fd
        if not pid:
            return False
        deadline = time.time() + timeout
        rc = None
        while time.time() < deadline:
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                rc = 0
                break
            if wpid == pid:
                rc = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
                break
            time.sleep(0.3)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        with self.lock:
            self._pid, self._fd = None, None
        return rc == 0

    def _fail_locked(self, reason: str):
        """lock 보유 상태에서 호출. 세션을 failed 로 표시하고 코드 소거."""
        self.state = "failed"
        self.error = reason
        self._code_value = None
        self._code_event.set()


# ── 모듈 싱글턴 + 헬퍼 ──────────────────────────────────────
_session = ReloginSession()


def start(project: str, config_dir: str = None, on_event=None) -> dict:
    return _session.start(project, config_dir, on_event=on_event)


def is_pending() -> bool:
    return _session.is_pending()


def submit_code(code: str) -> dict:
    return _session.submit_code(code)


def status() -> dict:
    return _session.status()


def looks_like_code(text: str) -> bool:
    return extract_code(text) is not None
