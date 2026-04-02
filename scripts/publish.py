#!/usr/bin/env python3
"""
로컬 퍼블리싱 CLI — 에이전트(Claude Code)가 bash로 호출.

Usage:
    python3 scripts/publish.py publish <project_id> <project_dir> [--username <name>]
    python3 scripts/publish.py unpublish <project_id> [--username <name>]
    python3 scripts/publish.py rebuild <project_id>
    python3 scripts/publish.py status
"""

import sys
import os
import json
import argparse

# daemon 패키지를 import할 수 있도록 path 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from daemon.globals import config, DAEMON_DIR, CONFIG_PATH
from daemon import site_manager


def load_config():
    """데몬 config.json 로드"""
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text()))


def main():
    parser = argparse.ArgumentParser(description="PeterVoice Local Publishing")
    sub = parser.add_subparsers(dest="command", required=True)

    # publish
    p_pub = sub.add_parser("publish", help="프로젝트 퍼블리싱")
    p_pub.add_argument("project_id", help="프로젝트 ID")
    p_pub.add_argument("project_dir", help="프로젝트 디렉토리 경로")
    p_pub.add_argument("--username", default=None, help="유저명")

    # unpublish
    p_unpub = sub.add_parser("unpublish", help="사이트 중지")
    p_unpub.add_argument("project_id", help="프로젝트 ID")
    p_unpub.add_argument("--username", default=None, help="유저명")

    # rebuild
    p_rebuild = sub.add_parser("rebuild", help="재빌드 & 재시작")
    p_rebuild.add_argument("project_id", help="프로젝트 ID")

    # status
    sub.add_parser("status", help="모든 사이트 상태 조회")

    # home-portal
    p_home = sub.add_parser("home-portal", help="홈 포탈 시작")
    p_home.add_argument("--username", default=None, help="유저명")
    p_home.add_argument("--stop", action="store_true", help="홈 포탈 중지")

    args = parser.parse_args()
    load_config()

    if args.command == "home-portal":
        if args.stop:
            result = site_manager.stop_home_portal()
        else:
            result = site_manager.start_home_portal(username=args.username)
    elif args.command == "publish":
        result = site_manager.publish(
            args.project_id,
            os.path.abspath(args.project_dir),
            username=args.username,
        )
    elif args.command == "unpublish":
        result = site_manager.unpublish(
            args.project_id,
            username=args.username,
        )
    elif args.command == "rebuild":
        result = site_manager.rebuild(args.project_id)
    elif args.command == "status":
        result = site_manager.status()
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if isinstance(result, dict) and result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
