#!/usr/bin/env python3
"""电脑端导入已审阅、已批准的 adopted 恢复来源；不打开 Collection。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inkcards_anki_bridge.services.compact_application import CompactApplicationService
from inkcards_anki_bridge.storage import ProfileStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument(
        "--reviewed-basis",
        required=True,
        action="store_true",
        help="确认历史来源、当前原卡前态及事件绑定已审阅；摘要不能自证真实性",
    )
    parser.add_argument(
        "--adopted-policy-approved",
        required=True,
        action="store_true",
        help="确认用户明确批准该来源列出的 adopted 规则；不得替代原时点冻结 context",
    )
    args = parser.parse_args()
    if not args.store_root.is_dir() or not any(args.store_root.glob("*.sqlite*")):
        parser.error("必须指定现有且已停用的插件 Store；请先关闭 Anki")
    source = json.loads(args.source.read_text(encoding="utf-8"))
    store = ProfileStore(args.store_root)
    try:
        digest = CompactApplicationService(store).import_adopted_source(
            source, expected_sha256=args.expected_sha256
        )
        print("adopted_source_imported", digest)
    finally:
        store.close()


if __name__ == "__main__":
    main()
