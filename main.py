"""Command-line entry point for the read-only flomo PoC."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from config import ConfigError, FlomoConfig
from flomo_client import FlomoClient, FlomoClientError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only flomo Web API PoC")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="dotenv file path (default: .env)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("latest", help="fetch the latest memo list")

    sync_parser = subparsers.add_parser("sync", help="test cursor pagination")
    sync_parser.add_argument("--page-size", type=int, default=None)
    sync_parser.add_argument("--max-pages", type=int, default=None)

    memo_parser = subparsers.add_parser("get", help="fetch one memo by slug")
    memo_parser.add_argument("slug")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        config = FlomoConfig.from_env(args.env_file)
        client = FlomoClient(config)

        if args.command == "latest":
            memos = client.get_latest_memos(limit=5)
            print(f"最近 memo 数量={len(memos)}")
            return 0

        if args.command == "sync":
            memos = client.sync_memos(args.page_size, args.max_pages)
            print(f"同步完成 | memo数量={len(memos)}")
            return 0

        if args.command == "get":
            client.get_memo(args.slug)
            print("单条 memo 获取成功 | memo数量=1")
            return 0

        parser.print_help()
        return 2
    except ConfigError:
        print("请求失败 | 状态码=无 | 错误类型=配置错误")
        return 2
    except FlomoClientError:
        return 1
    except ValueError:
        print("请求失败 | 状态码=无 | 错误类型=参数错误")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
