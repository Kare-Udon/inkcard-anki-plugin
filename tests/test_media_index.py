"""改一张图时，无关正文不能因媒体包物理位置改变而重写。"""

import dataclasses
import json
from types import SimpleNamespace

from inkcards_anki_bridge.anki.media_lab import Image, extract_blocks
from inkcards_anki_bridge.services.snapshot_files import SnapshotFilesService
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.storage import ProfileStore
from tests.test_snapshot_files import make_snapshot


def records(store, snapshot):
    result = {}
    rows = store._read(
        lambda db: db.execute(
            "select f.package from artifact_snapshot_files s join artifact_files f on f.profile_id=s.profile_id and f.file_id=s.file_id where s.snapshot_id=? and s.kind=?",
            (snapshot, "text"),
        ).fetchall()
    )
    for row in rows:
        raw = bytes(row[0])
        at = 6
        while at < len(raw):
            end = raw.index(b"\n", at)
            size = int(raw[at:end])
            record = json.loads(raw[end + 1 : end + 1 + size])
            at = end + size + 2
            result[record["card_id"]] = record
    return result


def test_one_image_change_does_not_rewrite_unrelated_card(tmp_path):
    media = tmp_path / "collection.media"
    media.mkdir()
    col = SimpleNamespace(media=SimpleNamespace(dir=lambda: str(media)))
    for name, color in [("a.png", "white"), ("b.png", "black")]:
        Image.new("RGB", (64, 64), color).save(media / name)
    store = ProfileStore(tmp_path / "bridge", profile_id="profile-a")
    publisher = SnapshotService(store)
    files = SnapshotFilesService(store)

    def build():
        value = make_snapshot(
            [
                {
                    "card_id": str(i),
                    "note_id": str(i + 100),
                    "ordinal": i - 1,
                    "question": "Q",
                    "answer": "A",
                }
                for i in (1, 2)
            ]
        )
        cards = []
        for card, name in zip(value.cards, ("a.png", "b.png")):
            blocks, assets = extract_blocks('<img src="' + name + '">', col)
            cards.append(
                dataclasses.replace(
                    card,
                    question_blocks=tuple(blocks),
                    answer_blocks=(),
                    media_assets=tuple(assets),
                )
            )
        return dataclasses.replace(value, cards=tuple(cards))

    first = publisher.publish(build(), scope_id="deck", generation=0)
    manifest = files._prepare_snapshot(store.get_snapshot(first.snapshot_id))
    before = records(store, first.snapshot_id)
    assert manifest["format"] == 7
    assert manifest["metadata"]["media_indexes"]
    Image.new("RGB", (64, 64), "gray").save(media / "a.png")
    second = publisher.publish(build(), scope_id="deck", generation=0)
    files._prepare_snapshot(store.get_snapshot(second.snapshot_id))
    after = records(store, second.snapshot_id)
    assert before["2"] == after["2"], "不相干图片的正文不能因包重排而改变"
    assert before["1"] != after["1"]
    assert "file" not in after["2"]["question_blocks"][0]
    store.close()
