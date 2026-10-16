#!/usr/bin/env python3
"""电脑端导入已人工核验的历史恢复来源；不打开 Anki Collection。"""
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
    parser.add_argument("--historical-source-reviewed", required=True, action="store_true",
                        help="确认来源为经核验的历史备份，非当前设置重建；SHA 不自证真实性")
    parser.add_argument("--ordinary-policy-approved", required=True, action="store_true",
                        help="显式批准原上下文下固定普通扰动策略；这不是严格历史队列回放")
    args = parser.parse_args()
    if not args.store_root.is_dir() or not any(args.store_root.glob("*.sqlite*")):
        parser.error("必须指定现有且已停用的插件 Store；请先关闭 Anki")
    source = json.loads(args.source.read_text(encoding="utf-8"))
    store = ProfileStore(args.store_root)
    try:
        digest = CompactApplicationService(store).import_legacy_source(
            source, expected_sha256=args.expected_sha256)
        print("legacy_source_imported", digest)
    finally:
        store.close()


if __name__ == "__main__":
    main()
