"""설정 UI 재로그인 브리지 (채팅 플로우와 같은 relogin.py 엔진 공유).

웹 설정 페이지 ↔ user_status DB ↔ 데몬을 이어준다. force_restart 폴링에 편승해
주기적으로 user_status.relogin_state 를 확인하고, 채팅 대신 DB 로 URL/결과를 주고받는다.

DB relogin_state 생명주기:
  (null/idle) → requested → starting → url_ready → code_submitted → verifying → done/failed

- requested: 유저가 [재로그인 시작] 클릭 → 데몬이 감지, 엔진 기동 → starting
- url_ready: 엔진이 URL 캡처 → DB 에 relogin_url + url_ready 기록 (유저가 링크/코드 입력)
- code_submitted: 유저가 코드 제출(relogin_code 세팅) → 데몬이 주입, 코드 즉시 소거 → verifying
- done/failed: 결과. 유저가 다시 시작하면 requested 로 리셋됨.

보안: relogin_code 는 주입 즉시 DB 에서 소거(None PATCH), 로그에 남기지 않는다.
"""

import daemon.globals as g
from daemon.globals import logger
from daemon import relogin
from daemon.supabase import get_relogin_status, patch_relogin


# 재진입/중복기동 방지용 로컬 플래그 (같은 프로세스 내)
_starting = False


def _on_event(ev: dict):
    """relogin 엔진 → DB 반영 콜백 (설정 모드)."""
    t = ev.get("type")
    if t == "url_ready":
        patch_relogin({"relogin_url": ev.get("url"), "relogin_state": "url_ready"})
        logger.info("[relogin-settings] url_ready -> DB")
    elif t == "done":
        patch_relogin({"relogin_state": "done", "relogin_code": None})
        logger.info("[relogin-settings] done -> DB")
    elif t == "failed":
        patch_relogin({"relogin_state": "failed", "relogin_code": None})
        logger.info("[relogin-settings] failed -> DB")


def poll_relogin(user_id: int):
    """메인 루프에서 주기 호출. user_status.relogin_state 를 보고 엔진을 구동한다."""
    global _starting
    if user_id is None:
        return
    try:
        st = get_relogin_status(user_id)
    except Exception as e:
        logger.warning(f"[relogin-settings] poll error: {e}")
        return

    state = st.get("relogin_state")
    if not state or state in ("idle", "done", "failed", "url_ready", "verifying", "starting"):
        # 대기/전이/종료 상태 — 다만 아래 code_submitted 만 별도 처리
        if state != "code_submitted":
            if state in ("done", "failed", "idle", None):
                _starting = False
            return

    if state == "requested":
        if _starting or relogin.status().get("state") in ("pending_url", "waiting_code", "verifying"):
            return
        _starting = True
        # 채팅이 아닌 DB 로 URL/결과 전달 (on_event) — 기본 계정으로 로그인
        patch_relogin({"relogin_state": "starting", "relogin_url": None})
        relogin.start("general", config_dir=None, on_event=_on_event)
        logger.info("[relogin-settings] engine started (requested->starting)")
        return

    if state == "code_submitted":
        code = st.get("relogin_code")
        if not code:
            return
        res = relogin.submit_code(code)
        code = None
        # 코드는 결과와 무관하게 즉시 DB 에서 소거
        if res.get("ok"):
            patch_relogin({"relogin_code": None, "relogin_state": "verifying"})
            logger.info("[relogin-settings] code injected (code_submitted->verifying)")
        else:
            patch_relogin({"relogin_code": None, "relogin_state": "failed"})
            logger.warning(f"[relogin-settings] code rejected: {res.get('reason')}")
        return
