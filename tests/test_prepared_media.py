"""旧库升级和跨进程缓存的用户路径回归。"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from inkcards_anki_bridge.anki import media_lab
from inkcards_anki_bridge.storage import ProfileStore, migrations


def legacy_database(path):
    db = sqlite3.connect(path)
    # 真实旧版 v8：图片类型尚未加入约束，保留旧版本号与迁移历史。
    for version in range(1, 9):
        for sql in getattr(migrations, f"_v{version}_statements")():
            db.execute(sql.replace(", 'media'", "").replace(",'media'", ""))
        db.execute("insert into schema_migrations values(?,1)", (version,))
    db.execute("insert into profile_meta values('profile',1,1,0)")
    db.execute(
        "insert into artifact_files values('profile',?,?,'text',1,?,1,?,1)",
        ("a" * 64, "old.bin", "a" * 64, b"x"),
    )
    db.execute("pragma user_version=8")
    db.commit()
    db.close()


def test_upgrade_real_v8_constraints_preserves_existing_data(tmp_path):
    legacy_database(tmp_path / "bridge.sqlite3")
    store = ProfileStore(tmp_path, profile_id="profile")
    with store._transaction() as db:
        db.execute(
            "insert into artifact_files values('profile',?,?,'media',1,?,1,?,1)",
            ("b" * 64, "new.bin", "b" * 64, b"y"),
        )
        db.execute(
            "insert into artifact_names values('profile','new.bin',?,'media')",
            ("b" * 64,),
        )
        assert (
            db.execute(
                "select package from artifact_files where name=?", ("old.bin",)
            ).fetchone()[0]
            == b"x"
        )
        assert db.execute("pragma foreign_key_check").fetchall() == []
        assert db.execute("pragma user_version").fetchone()[0] >= 9
    store.close()
    again = ProfileStore(tmp_path, profile_id="profile")
    again.close()


def test_restart_reuses_png_and_descriptor_without_decoding_source(tmp_path):
    folder = tmp_path / "collection.media"
    folder.mkdir()
    media_lab.Image.new("RGB", (700, 900), "black").save(folder / "drawing.png")
    col = SimpleNamespace(media=SimpleNamespace(dir=lambda: str(folder)))
    first, assets = media_lab.extract_blocks('<img src="drawing.png">', col)
    media_lab._CONVERTED.clear()  # 模拟 Anki 重启。
    with patch.object(
        media_lab, "binarize", side_effect=AssertionError("重启后不应重新转换")
    ):
        actual, _ = media_lab.extract_blocks('<img src="drawing.png">', col)
    assert actual == first
    # 落盘缓存损坏仍须重建，不能把损坏资源发送给设备。
    media_lab._CONVERTED.clear()
    Path(assets[0]["path"]).write_bytes(b"corrupt")
    actual, assets = media_lab.extract_blocks('<img src="drawing.png">', col)
    assert actual == first
    assert all(Path(a["path"]).read_bytes().startswith(b"\x89PNG") for a in assets)


def test_v9_migration_rolls_back_all_tables_on_failure(tmp_path):
    legacy_database(tmp_path / "bridge.sqlite3")
    db = sqlite3.connect(tmp_path / "bridge.sqlite3")

    class FaultAtCommitMarker:
        def execute(self, sql, args=()):
            if sql.startswith(
                "INSERT INTO schema_migrations(version, applied_at_ms) VALUES(9"
            ):
                raise sqlite3.OperationalError("injected migration failure")
            return db.execute(sql, args)

        def commit(self):
            db.commit()

        def rollback(self):
            db.rollback()

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        migrations._migrate_media_types(FaultAtCommitMarker(), 2)
    assert db.execute("pragma user_version").fetchone()[0] == 8
    assert db.execute("select package from artifact_files").fetchone()[0] == b"x"
    assert (
        db.execute(
            "select count(*)from sqlite_master where name like '%_media_v9'"
        ).fetchone()[0]
        == 0
    )
    assert (
        "'media'"
        not in db.execute(
            "select sql from sqlite_master where name='artifact_files'"
        ).fetchone()[0]
    )
    assert db.execute("pragma foreign_keys").fetchone()[0] == 1
    db.close()
