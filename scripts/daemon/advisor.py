"""`/advisor` 를 담당(프로젝트·브랜치) 단위로 가둔다 (2026-11-23, evolution 성장 적용 2단).

Claude Code 의 `/advisor <모델>` 은 `-p` 에서도 동작하고, 값을 `~/.claude/settings.json` 의
`advisorModel` 에 저장한다. 그 파일은 그 기기의 모든 세션이 읽으므로 채팅 한 줄이 하트비트를 포함한
전 담당의 턴에 조언 모델을 붙인다(조언 모델이 대화를 읽는 비용은 캐시되지 않는다).

그래서 데몬이 설정·해제를 가로채 담당별 값으로 두고(`~/.claude-daemon/advisor.json`),
그 담당의 턴에만 `--advisor <모델>` 플래그로 넘긴다(플래그는 그 세션에만 적용된다).
인자 없는 조회(`/advisor`)도 데몬이 답한다 — CLI 로 보내면 새 세션·브랜치 첫 턴에서는 맥락이 앞에 붙어
슬래시 명령으로 읽히지 않는다. CLI 로 가는 것은 하나뿐: 기기 설정에 남은 값을 끄는 `/advisor off`(worker 참고).

CLI 실측(2.1.330):
- `--advisor <fable|opus|sonnet>` 만 받는다. `off` 나 모르는 값을 플래그로 주면 CLI 가 rc=1 로 죽는다
  → 저장 전에 엄격히 검증하고, `off` 는 「값 지움」으로만 처리한다.
- 조언 모델이 본 모델보다 낮으면 CLI 가 경고만 하고 쓰지 않는다(치명 아님).
"""

import json
import os
import re
import threading
from datetime import datetime

from daemon.globals import DAEMON_DIR, logger

ADVISOR_PATH = DAEMON_DIR / "advisor.json"
ADVISOR_MODELS = ("fable", "opus", "sonnet")
_OFF_WORDS = ("off", "none", "끄기", "해제")

_lock = threading.Lock()
_CMD_RE = re.compile(r"^/advisor(?:\s+(.*))?$", re.DOTALL | re.IGNORECASE)


def parse_advisor_command(text: str):
    """채팅 텍스트가 `/advisor` 명령이면 (kind, value) 를, 아니면 None 을 돌려준다.

    kind: "query"(인자 없음) | "set"(value=모델) | "off" | "invalid"(value=받은 인자).
    값을 주는 꼴은 CLI 로 보내지 않는다 — CLI 가 받아들이는 값이면 settings.json 에 저장되기 때문이다.
    """
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return None
    arg = (m.group(1) or "").strip()
    if not arg:
        return ("query", None)
    low = arg.lower()
    if low in ADVISOR_MODELS:
        return ("set", low)
    if low in _OFF_WORDS:
        return ("off", None)
    return ("invalid", arg)


def _load() -> dict:
    try:
        if ADVISOR_PATH.exists():
            data = json.loads(ADVISOR_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"[advisor] failed to read {ADVISOR_PATH}: {e}")
    return {}


def _save(data: dict) -> bool:
    try:
        tmp = ADVISOR_PATH.with_name(ADVISOR_PATH.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, ADVISOR_PATH)
        return True
    except Exception as e:
        logger.warning(f"[advisor] failed to write {ADVISOR_PATH}: {e}")
        return False


def get_advisor(project: str) -> str | None:
    """이 담당(프로젝트 id 또는 `branch:N`)에 켜 둔 조언 모델. 없거나 값이 이상하면 None.
    상속은 없다 — 켠 그 대화에만 붙는다(비용이 퍼지지 않게 하는 것이 이 장치의 목적)."""
    with _lock:
        entry = _load().get(project)
    model = entry.get("model") if isinstance(entry, dict) else None
    # 파일을 손으로 고쳤거나 낡은 값이 남았을 때도 CLI 를 죽이지 않도록 한 번 더 거른다
    return model if model in ADVISOR_MODELS else None


def set_advisor(project: str, model: str) -> bool:
    if model not in ADVISOR_MODELS:
        return False
    with _lock:
        data = _load()
        data[project] = {"model": model, "set_at": datetime.now().isoformat(timespec="seconds")}
        ok = _save(data)
    if ok:
        logger.info(f"[advisor] {project}: advisor set to {model}")
    return ok


def clear_advisor(project: str, reason: str = "") -> bool:
    """값을 지운다. 지울 값이 있었으면 True."""
    with _lock:
        data = _load()
        if project not in data:
            return False
        data.pop(project, None)
        _save(data)
    logger.info(f"[advisor] {project}: advisor cleared" + (f" ({reason})" if reason else ""))
    return True


def device_advisor_model(config_dir: str | None = None) -> str | None:
    """이 기기 설정(settings.json)에 CLI 가 직접 저장해 둔 조언 모델. 없으면 None. 읽기만 한다.

    이 장치가 들어오기 전에 채팅으로 `/advisor <모델>` 을 쳤던 기기에는 값이 남아 있을 수 있다.
    그 값은 CLI 만 지울 수 있으므로, 그런 기기에서 `/advisor off` 는 CLI 로 보내야 한다(worker 가 판단).
    """
    dirs = []
    for d in (config_dir, os.environ.get("CLAUDE_CONFIG_DIR"), os.path.join(os.path.expanduser("~"), ".claude")):
        if d and os.path.expanduser(d) not in dirs:
            dirs.append(os.path.expanduser(d))
    for d in dirs:
        try:
            with open(os.path.join(d, "settings.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            val = data.get("advisorModel") if isinstance(data, dict) else None
            if val and str(val).lower() != "off":
                return str(val)
        except Exception:
            continue
    return None


def stderr_blames_advisor(stderr_text: str) -> bool:
    """CLI 가 `--advisor` 때문에 죽었는지(모르는 플래그·쓸 수 없는 모델). 러너의 안전망이 쓴다."""
    # 실측 문구: `Error: The model "x" cannot be used as an advisor.` / 구형 CLI `error: unknown option '--advisor'`.
    # 「낮은 모델이라 안 쓴다」 경고(`cannot advise`)는 치명 오류가 아니므로 여기 걸리면 안 된다.
    # 넓게 잡으면 인증 만료·한도 같은 다른 실패에서 값을 지우고 헛재시도하게 된다 — 이 두 문구만 본다.
    low = (stderr_text or "").lower()
    return "cannot be used as an advisor" in low or ("unknown option" in low and "--advisor" in low)


def handle_command(kind: str, value: str | None, project: str, device_model: str | None = None) -> str:
    """query / set / off / invalid 에 대한 채팅 답. device_model = 기기 설정에 남아 있는 값(있으면 알려 준다)."""
    _usage = f"`/advisor <{'|'.join(ADVISOR_MODELS)}|off>`"
    if kind == "query":
        cur = get_advisor(project)
        lines = [f"이 대화의 조언 모델: {cur} (이 대화에만 적용). 끄려면 `/advisor off`." if cur
                 else f"이 대화에는 조언 모델이 꺼져 있습니다. 켜려면 {_usage}."]
        if device_model:
            lines.append(f"주의: 이 기기 전체 설정에 조언 모델 {device_model} 이 켜져 있어 모든 대화에 적용되고 있습니다. "
                         "`/advisor off` 를 보내면 끕니다.")
        return "\n".join(lines)
    if kind == "set":
        if not set_advisor(project, value):
            return "조언 모델 설정을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요."
        return (
            f"조언 모델을 {value} 로 켰습니다. 이 대화(담당)에만 적용되고 다른 프로젝트·브랜치에는 영향이 없습니다.\n"
            "- Claude 엔진의 이 대화 턴에 붙습니다(코덱스·제미나이 엔진, 팀 세션에는 붙지 않습니다).\n"
            "- 조언 모델은 본 모델과 같거나 더 높아야 쓰입니다(낮으면 Claude 가 무시합니다).\n"
            "- 조언 모델이 대화를 읽는 비용은 캐시되지 않아 턴당 사용량이 늘 수 있습니다.\n"
            "끄려면 `/advisor off`, 지금 값은 `/advisor` 로 확인합니다."
        )
    if kind == "off":
        had = clear_advisor(project, "user /advisor off")
        return "조언 모델을 껐습니다." if had else "이 대화에는 켜 둔 조언 모델이 없습니다."
    return f"`/advisor {value}` 는 쓸 수 없는 값입니다. 사용법: {_usage} (이 대화에만 적용됩니다)."
