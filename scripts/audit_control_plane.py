#!/usr/bin/env python3
"""Run the account binding integrity audit without changing any rows."""
from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser(description="审计账号身份、远端绑定和 assignment")
    parser.add_argument("--database-url", default=None, help="SQLModel DATABASE_URL")
    parser.add_argument("--fail-on-issues", action="store_true", help="发现重复或歧义时返回 1")
    args = parser.parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    from core.db import engine
    from services.control_plane_audit import run_binding_audit

    try:
        report = run_binding_audit(engine)
    except Exception as exc:
        # Keep the command read-only and make an uninitialised database
        # actionable instead of emitting a long SQLAlchemy traceback.
        message = str(exc).lower()
        if "no such table" in message or "does not exist" in message or "undefinedtable" in message:
            parser.error("数据库尚未初始化控制面表，请先启动一次后端完成迁移")
        raise
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.fail_on_issues and int(report.get("issue_count") or 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
