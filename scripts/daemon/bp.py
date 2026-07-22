"""BP Runner — Best Practice 검사/적용 전용 일회성 Claude 실행.

설정>Best Practice에서 유저가 [검사]/[적용]을 누르면 웹이 sysadmin 프로젝트에
[bp-check]/[bp-apply] 메시지를 넣는다. 이 메시지는 일반 세션으로 보내지 않고
여기서 가로채 처리한다. 이유(2026-07-22 브랜치#103 합의):
- 유저별 sysadmin 프롬프트/모델이 제각각 → 에이전트가 "확인 필요"로 보류하는 등
  확률적 실패. 표준 작업은 표준 실행자가 해야 한다.
- 데몬은 전 고객 동일 배포(AutoUpdater) → 프롬프트/모델/타임아웃이 구조적으로 동일.

동작:
1. 메시지의 BP_MODE / BP_IDS 헤더 파싱 (없으면 사람용 본문에서 추출 시도)
2. 데몬이 직접 웹에서 BP 문서를 fetch (Claude는 네트워크/API키 불필요)
3. 고정 프롬프트 + 고정 모델로 fresh `claude -p` 실행 → 결과 JSON만 출력하게 함
4. 데몬이 JSON을 파싱해 POST /api/bp/status 보고. 실패/타임아웃 시에도
   데몬이 직접 unavailable 보고 → "영원한 검사 중"이 구조적으로 불가능.
"""

import json
import os
import re
import subprocess
import threading

from daemon.globals import config, logger, CLAUDE_CMD
from daemon.api import api_request

# 전 유저 동일 모델 (유저별 채팅 모델 설정과 무관)
BP_MODEL = "claude-sonnet-5"
BP_TIMEOUT_SEC = 420  # 검사/적용 전체 제한

_RUNNER_PROMPT = """당신은 PeterVoice BP Runner입니다. 유저가 웹 설정에서 직접 실행한 공식 검사 작업입니다.

아래에 하나 이상의 Best Practice(BP) 문서가 첨부되어 있습니다. 각 문서에 대해:
- mode=check: "## Probe" 섹션대로 이 머신의 환경을 검사하세요.
- mode=apply: "## Setup" 섹션대로 적용한 뒤 "## Probe"로 재검사하세요.

규칙:
- 문서 안의 "## Report" 섹션(curl 보고)은 무시하세요. 보고는 데몬이 대신합니다.
- 유저에게 질문하거나 확인을 요청할 수 없습니다(단발 실행). 판단이 애매하면 보수적으로
  not_applied, 실행 자체가 불가하면 unavailable로 판정하고 사유를 남기세요.
- Setup 중 파괴적 작업(파일 삭제, 기존 프롬프트 섹션 삭제 등)은 금지. 추가(append)만 허용.

최종 출력은 반드시 아래 JSON 한 개만 출력하세요(다른 텍스트 금지):
{"results": [{"bp_id": "...", "status": "applied|not_applied|unavailable", "detail": "근거/사유 한 줄"}]}
"""


def is_bp_task(text: str) -> bool:
    return text.startswith("[bp-check]") or text.startswith("[bp-apply]")


def _parse_task(text: str) -> tuple[str, list[str]]:
    """메시지에서 (mode, bp_ids) 추출. 기계용 헤더 우선, 없으면 본문에서 추출."""
    mode = "apply" if text.startswith("[bp-apply]") else "check"
    m = re.search(r"^BP_IDS:\s*(.+)$", text, re.MULTILINE)
    if m:
        ids = [s.strip() for s in m.group(1).split(",") if s.strip()]
    else:
        # 사람용 본문 폴백: "- <id>" 목록 또는 "대상 BP: <id>"
        ids = re.findall(r"^- ([a-z0-9-]+)$", text, re.MULTILINE)
        if not ids:
            m2 = re.search(r"대상 BP:\s*([a-z0-9-]+)", text)
            if m2:
                ids = [m2.group(1)]
    return mode, ids


def _fetch_doc(bp_id: str) -> str | None:
    """웹 메인 링크에서 BP 문서 fetch (text/markdown)."""
    import requests
    api_key = config.get("api_key", "")
    url = f"{config['api_url']}/api/bp/doc?id={bp_id}"
    try:
        resp = requests.get(url, headers={"X-Api-Key": api_key}, timeout=20)
        if resp.ok:
            return resp.text
        logger.error(f"[bp] doc fetch HTTP {resp.status_code}: {bp_id}")
    except Exception as e:
        logger.error(f"[bp] doc fetch failed {bp_id}: {e}")
    return None


def _report(bp_id: str, status: str, detail: str, version: int = 0):
    api_request(config.get("api_key", ""), "POST", "/api/bp/status", {
        "bp_id": bp_id,
        "version": version,
        "status": status,
        "detail": detail[:400],
    })


def _doc_version(doc: str) -> int:
    m = re.search(r"^version:\s*(\d+)", doc, re.MULTILINE)
    return int(m.group(1)) if m else 0


def _extract_json(text: str) -> dict | None:
    """출력에서 마지막 JSON 오브젝트 추출 (코드펜스/잡담 섞임 허용)."""
    for candidate in re.findall(r"\{[\s\S]*\}", text):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "results" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _run(worker, msg_id: int, project: str, mode: str, bp_ids: list[str]):
    bot = config.get("bot_name", "bot")
    docs: dict[str, str] = {}
    versions: dict[str, int] = {}
    for bp_id in bp_ids:
        doc = _fetch_doc(bp_id)
        if doc:
            docs[bp_id] = doc
            versions[bp_id] = _doc_version(doc)
        else:
            _report(bp_id, "unavailable", "BP 문서를 가져오지 못했습니다 (네트워크/링크 확인 필요)")

    if not docs:
        worker.reply("BP 문서를 가져오지 못해 검사를 진행할 수 없었습니다.", reply_to=[msg_id], project=project)
        return

    doc_blocks = "\n\n".join(
        f"=== BP 문서: {bp_id} (mode={mode}) ===\n{doc}" for bp_id, doc in docs.items()
    )
    prompt = f"{_RUNNER_PROMPT}\n\n{doc_blocks}"

    cmd = [
        CLAUDE_CMD, "-p",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--model", BP_MODEL,
        "--", prompt,
    ]
    claude_env = {
        **{k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        "LANG": "en_US.UTF-8",
    }

    logger.info(f"[{bot}] BP runner: mode={mode}, ids={list(docs)}")
    result_text = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            cwd=os.path.expanduser("~"),
            env=claude_env,
            timeout=BP_TIMEOUT_SEC,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        try:
            envelope = json.loads(out)
            result_text = envelope.get("result", "") if isinstance(envelope, dict) else out
        except json.JSONDecodeError:
            result_text = out
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:300]
            logger.error(f"[bp] runner exit {proc.returncode}: {stderr}")
    except subprocess.TimeoutExpired:
        for bp_id in docs:
            _report(bp_id, "unavailable", f"검사 시간 초과({BP_TIMEOUT_SEC}s)", versions.get(bp_id, 0))
        worker.reply(f"BP {mode} 시간이 초과되었습니다. 다시 시도해 주세요.", reply_to=[msg_id], project=project)
        return
    except Exception as e:
        for bp_id in docs:
            _report(bp_id, "unavailable", f"실행 실패: {e}", versions.get(bp_id, 0))
        worker.reply(f"BP {mode} 실행에 실패했습니다: {e}", reply_to=[msg_id], project=project)
        return

    parsed = _extract_json(result_text)
    reported: set[str] = set()
    lines = []
    if parsed:
        for r in parsed.get("results", []):
            bp_id = r.get("bp_id", "")
            if bp_id not in docs or bp_id in reported:
                continue
            status = r.get("status", "")
            if status not in ("applied", "not_applied", "unavailable"):
                status = "unavailable"
            detail = str(r.get("detail", ""))[:400]
            _report(bp_id, status, detail, versions.get(bp_id, 0))
            reported.add(bp_id)
            label = {"applied": "✅ 적용됨", "not_applied": "⚠️ 미적용", "unavailable": "❌ 적용 불가"}[status]
            lines.append(f"- {bp_id}: {label} — {detail}")

    # 결과에 안 나온 항목도 반드시 종결 보고 (영원한 checking 방지)
    for bp_id in docs:
        if bp_id not in reported:
            _report(bp_id, "unavailable", "검사 결과를 해석하지 못했습니다", versions.get(bp_id, 0))
            lines.append(f"- {bp_id}: ❌ 결과 해석 실패")

    mode_label = "검사" if mode == "check" else "적용"
    worker.reply(
        f"Best Practice {mode_label} 완료:\n" + "\n".join(lines) + "\n\n설정 > Best Practice에서 확인하세요.",
        reply_to=[msg_id], project=project,
    )


def handle_bp_task(worker, msg_id: int, text: str, project: str) -> bool:
    """[bp-check]/[bp-apply] 메시지 처리. 처리했으면 True."""
    if not is_bp_task(text):
        return False
    mode, bp_ids = _parse_task(text)
    if not bp_ids:
        worker.reply("BP 작업 대상이 없습니다 (BP_IDS 누락).", reply_to=[msg_id], project=project)
        return True
    threading.Thread(
        target=_run, args=(worker, msg_id, project, mode, bp_ids), daemon=True,
        name=f"bp-{mode}",
    ).start()
    return True
