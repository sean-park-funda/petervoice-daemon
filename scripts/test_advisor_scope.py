#!/usr/bin/env python3
"""`/advisor` 담당 한정 시험 (2026-11-23) — 조회·설정·해제를 데몬이 받아 담당별 값으로만 둔다(CLI 로 가는 것은 기기 설정을 끄는 off 뿐).
실행: python3 scripts/test_advisor_scope.py
"""
import sys
import tempfile
from pathlib import Path

import queue  # noqa: F401,E402  (scripts/daemon/queue.py 가림 방지)
import logging.handlers  # noqa: F401,E402
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "daemon"))

from daemon import advisor  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS" if ok else "FAIL"), name, detail if not ok else "")
    fails += 0 if ok else 1


P = advisor.parse_advisor_command

# ── 명령 해석: 인자가 하나라도 있으면 CLI 로 가면 안 된다(가면 settings.json 에 저장된다)
check("인자 없음 → query", P("/advisor") == ("query", None))
check("대문자 명령도 잡는다(/Advisor 가 CLI 로 새지 않게)", P("/Advisor opus") == ("set", "opus") and P("/ADVISOR") == ("query", None))
check("앞뒤 공백 → query", P("  /advisor  \n") == ("query", None))
check("opus → set", P("/advisor opus") == ("set", "opus"))
check("대소문자·공백 → set", P("/advisor   Sonnet ") == ("set", "sonnet"))
check("fable → set", P("/advisor fable") == ("set", "fable"))
check("off → off", P("/advisor off") == ("off", None))
check("OFF → off", P("/advisor OFF") == ("off", None))
check("모르는 값 → invalid", P("/advisor bogus") == ("invalid", "bogus"))
check("전체 모델 id → invalid (CLI 는 받아 저장하므로 넘기면 안 된다)", P("/advisor claude-opus-5-5") == ("invalid", "claude-opus-5-5"))
check("haiku → invalid", P("/advisor haiku")[0] == "invalid")
check("인자 둘 → invalid", P("/advisor opus now")[0] == "invalid")
check("다른 명령 /advisors → None", P("/advisors opus") is None)
check("문장 중간 → None", P("이거 /advisor opus 로 해줘") is None)
check("릴레이 머리 → None", P("[relay from:evolution] /advisor opus") is None)
check("빈 문자열 → None", P("") is None and P(None) is None)

# ── 저장: 담당별로 따로, 상속 없음
with tempfile.TemporaryDirectory() as td:
    advisor.ADVISOR_PATH = Path(td) / "advisor.json"
    check("처음엔 없음", advisor.get_advisor("proj-a") is None)
    check("set 성공", advisor.set_advisor("proj-a", "sonnet") is True)
    check("그 담당에만", advisor.get_advisor("proj-a") == "sonnet")
    check("다른 프로젝트엔 없음", advisor.get_advisor("proj-b") is None)
    check("같은 프로젝트의 브랜치에도 없음(상속 없음)", advisor.get_advisor("branch:12") is None)
    advisor.set_advisor("branch:12", "opus")
    check("브랜치 값은 따로", advisor.get_advisor("branch:12") == "opus" and advisor.get_advisor("proj-a") == "sonnet")
    check("모르는 모델은 저장 거부", advisor.set_advisor("proj-a", "off") is False and advisor.get_advisor("proj-a") == "sonnet")
    check("clear → True", advisor.clear_advisor("proj-a", "test") is True)
    check("지운 뒤 없음", advisor.get_advisor("proj-a") is None)
    check("없는 것 clear → False", advisor.clear_advisor("proj-a") is False)
    check("남의 값은 그대로", advisor.get_advisor("branch:12") == "opus")
    # 손으로 고친 이상한 값이 CLI 까지 가면 그 담당의 모든 턴이 rc=1 로 죽는다 → 읽을 때 한 번 더 거른다
    advisor.ADVISOR_PATH.write_text('{"proj-x": {"model": "off"}, "proj-y": "opus", "proj-z": {"model": "bogus"}}', encoding="utf-8")
    check("저장된 off/형식 다름/모르는 값 → None", advisor.get_advisor("proj-x") is None
          and advisor.get_advisor("proj-y") is None and advisor.get_advisor("proj-z") is None)
    advisor.ADVISOR_PATH.write_text("{ not json", encoding="utf-8")
    check("깨진 파일 → None (예외 없음)", advisor.get_advisor("proj-a") is None)
    check("깨진 파일 위에 set 은 새로 씀", advisor.set_advisor("proj-a", "fable") and advisor.get_advisor("proj-a") == "fable")

    # ── 채팅 답
    r = advisor.handle_command("query", None, "proj-c")
    check("query 답(꺼짐): 켜는 법", "꺼져 있습니다" in r and "fable|opus|sonnet|off" in r and "주의" not in r, r)
    r = advisor.handle_command("set", "opus", "proj-c")
    check("set 답: 이 대화에만·끄는 법·비용·붙지 않는 곳", "opus" in r and "이 대화" in r and "/advisor off" in r and "캐시" in r
          and "코덱스" in r and "팀 세션" in r, r)
    r = advisor.handle_command("query", None, "proj-c")
    check("query 답(켜짐): 값", "이 대화의 조언 모델: opus" in r, r)
    r = advisor.handle_command("query", None, "proj-c", device_model="sonnet")
    check("query 답: 기기 설정에 남은 값이 있으면 알린다", "기기 전체 설정" in r and "sonnet" in r and "/advisor off" in r, r)
    check("query 는 값을 바꾸지 않는다", advisor.get_advisor("proj-c") == "opus")
    check("set 답 뒤 저장됨", advisor.get_advisor("proj-c") == "opus")
    check("off 답(있었음)", advisor.handle_command("off", None, "proj-c") == "조언 모델을 껐습니다." and advisor.get_advisor("proj-c") is None)
    check("off 답(없었음)", "없습니다" in advisor.handle_command("off", None, "proj-c"))
    r = advisor.handle_command("invalid", "bogus", "proj-c")
    check("invalid 답: 사용법, 저장 안 함", "fable|opus|sonnet|off" in r and advisor.get_advisor("proj-c") is None, r)

# ── 기기 설정에 남은 값 찾기 (읽기만) — 있으면 워커가 `/advisor off` 를 CLI 로 보낸다
import os  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    _home = os.environ.get("HOME")
    _ccd = os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ["HOME"] = td  # ~/.claude 가 실제 설정을 가리키지 않게
    try:
        cfg = Path(td) / "seat"
        cfg.mkdir()
        check("설정 파일 없음 → None", advisor.device_advisor_model(str(cfg)) is None)
        (cfg / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
        check("advisorModel 없음 → None", advisor.device_advisor_model(str(cfg)) is None)
        (cfg / "settings.json").write_text('{"advisorModel": "opus"}', encoding="utf-8")
        before_bytes = (cfg / "settings.json").read_bytes()
        check("advisorModel 있음 → 그 값", advisor.device_advisor_model(str(cfg)) == "opus")
        check("읽기만 한다(파일 불변)", (cfg / "settings.json").read_bytes() == before_bytes)
        (cfg / "settings.json").write_text('{"advisorModel": "off"}', encoding="utf-8")
        check("값이 off → None", advisor.device_advisor_model(str(cfg)) is None)
        (cfg / "settings.json").write_text("{ broken", encoding="utf-8")
        check("깨진 설정 → None (예외 없음)", advisor.device_advisor_model(str(cfg)) is None)
        home_claude = Path(td) / ".claude"
        home_claude.mkdir()
        (home_claude / "settings.json").write_text('{"advisorModel": "sonnet"}', encoding="utf-8")
        check("계정 폴더에 없으면 기본 ~/.claude 도 본다", advisor.device_advisor_model(str(cfg)) == "sonnet")
        check("폴더 인자 없이도 ~/.claude 를 본다", advisor.device_advisor_model(None) == "sonnet")
    finally:
        os.environ["HOME"] = _home
        if _ccd is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = _ccd

# ── 안전망 판정 (CLI 2.1.330 실측 문구)
B = advisor.stderr_blames_advisor
check("치명: 쓸 수 없는 모델", B('Error: The model "bogus-model" cannot be used as an advisor.'))
check("치명: 구형 CLI 가 플래그를 모름", B("error: unknown option '--advisor'"))
check("비치명 경고(낮은 모델)는 안 걸림", not B('"sonnet" cannot advise "claude-opus-5-5" (the advisor must be at least as capable as the main model). The advisor will not be used for the main model.'))
check("무관한 오류는 안 걸림", not B("Error: No conversation found with session ID abc") and not B(""))
check("--advisor 를 언급만 하는 다른 실패는 안 걸림(인증·한도 분기를 건너뛰지 않게)",
      not B("warning: --advisor sonnet ignored\nError: OAuth token has expired") and not B("You've hit your limit (ran with --advisor opus)"))

# ── 게이트: 러너가 플래그를 붙이는 자리는 한 곳, 폴백·팀·데모 턴에는 안 붙는다
src = (ROOT / "daemon" / "claude_runner.py").read_text(encoding="utf-8")
check('러너에 "--advisor" 인자 한 곳', src.count('cmd.extend(["--advisor", advisor_model])') == 1)
check("붙이지 않는 조건이 그대로 있다",
      "if not (_skip_advisor or model_override or session_key_override or prompt_override is not None or is_demo):" in src)
wsrc = (ROOT / "daemon" / "worker.py").read_text(encoding="utf-8")
_blk = wsrc[wsrc.index("parse_advisor_command(text)"):wsrc.index("# Special commands")]
check("CLI 로 흘리는 예외는 「기기 설정에 값이 있을 때의 off」 하나", _blk.count('if _adv[0] == "off" and _dev:') == 1
      and _blk.count("return") == 1 and _blk.index('if _adv[0] == "off" and _dev:') < _blk.index("else:") < _blk.index("return"))
check("나머지는 답하고 끝낸다(dequeue + return)", "handle_command(_adv[0], _adv[1], project, device_model=_dev)" in _blk and "dequeue_message(msg_id)" in _blk)
check("안전망은 종료 센티널을 그대로 돌려준다", "if _r[0] == SHUTDOWN_INTERRUPTED:" in src)
check("가로채기가 Special commands 보다 앞", wsrc.index("parse_advisor_command(text)") < wsrc.index("# Special commands"))

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
