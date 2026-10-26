"""音频按实际卡面过滤；不读取或发送声音文件。"""

from types import SimpleNamespace

import anki.collection
import pytest
from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from inkcards_anki_bridge.anki.media_lab import extract_blocks
from inkcards_anki_bridge.anki.operations import OperationContext, QueryOpAdapter
from inkcards_anki_bridge.anki.snapshot_jobs import SnapshotJobManager, SnapshotJobState
from PIL import Image
from tests.test_snapshot_jobs import FakeHost


@pytest.fixture
def native(tmp_path):
    col = anki.collection.Collection(str(tmp_path / "audio.anki2"))
    col.set_config("fsrs", True)
    adapter = Anki260801Adapter(
        collection_provider=lambda: col,
        profile_provider=lambda: {
            "profile_identity": "audio-lab",
            "generation": 1,
            "runtime_epoch": "audio-epoch",
        },
        profile_identity="audio-lab",
        anki_version="26.9.3",
        anki_build="29bb700b",
    )
    model = col.models.by_name("Basic")
    # 单独渲染答案，便于区分背面只有音频和 FrontSide 重复正文。
    model["tmpls"][0]["afmt"] = "{{Back}}"
    col.models.save(model)
    yield col, adapter, model
    col.close()


def add_card(col, model, front, back):
    note = col.new_note(model)
    note.fields = [front, back]
    col.add_note(note, 1)
    return note.cards()[0]


@pytest.mark.parametrize(
    "front,back,kept",
    [
        ("单词[sound:missing.mp3]", "释义[sound:missing.mp3]", True),
        ("[sound:missing.mp3]", "释义", False),
        ("单词", "[sound:missing.mp3]", False),
        ("[sound:missing.mp3]", "[sound:missing.mp3]", False),
        ('<audio src="missing.mp3">浏览器不支持</audio>', "释义", False),
        ('单词<audio><source src="missing.mp3">备用说明</audio>', "释义", True),
        ("<style>.card{color:red}</style>[sound:missing.mp3]", "释义", False),
        ("<script>有字</script>[sound:missing.mp3]", "释义", False),
        ("<template>有字</template>[sound:missing.mp3]", "释义", False),
        ('<img src="visible.png">[sound:missing.mp3]', "释义", True),
        ('<img src="missing.png">[sound:missing.mp3]', "释义", False),
    ],
)
def test_native_rendered_sides(native, front, back, kept):
    col, adapter, model = native
    Image.new("1", (10, 10), 1).save(col.media.dir() + "/visible.png")
    card = add_card(col, model, front, back)
    result, reasons = adapter._card_snapshot(card, col)
    if not kept:
        assert result is None
        assert reasons == ("audio_only_side",)
    else:
        assert result is not None
        body = str(result.to_dict())
        assert "[音频不可播放]" in body
        assert "missing.mp3" not in body
        assert "[anki:play:" not in body
        assert "备用说明" not in body
        assert not reasons


def test_raw_sound_and_native_tokens_keep_original_order_without_audio_assets(tmp_path):
    col = SimpleNamespace(media=SimpleNamespace(dir=lambda: str(tmp_path)))
    for token in ("[sound:missing.mp3]", "[anki:play:q:0]"):
        blocks, assets = extract_blocks("前文" + token + "后文", col)
        assert [b["text"].strip() for b in blocks] == ["前文", "[音频不可播放]", "后文"]
        assert not assets


def test_frontside_is_counted_as_actual_visible_content(native):
    col, adapter, model = native
    model["tmpls"][0]["afmt"] = "{{FrontSide}}<hr id=answer>{{Back}}"
    col.models.save(model)
    result, reasons = adapter._card_snapshot(
        add_card(col, model, "单词", "[sound:x.mp3]"), col
    )
    assert result is not None
    assert "单词" in result.answer
    assert "[音频不可播放]" in result.answer
    assert not reasons


@pytest.mark.parametrize("keep_count", [0, 1])
def test_automatic_full_scope_publishes_with_counted_audio_exclusions(
    native, tmp_path, keep_count
):
    from inkcards_anki_bridge.services.snapshot_files import SnapshotFilesService
    from inkcards_anki_bridge.services.snapshots import SnapshotService
    from inkcards_anki_bridge.storage import ProfileStore

    col, adapter, model = native
    keep = add_card(col, model, "单词[sound:x.mp3]", "释义") if keep_count else None
    drop = add_card(col, model, "[sound:x.mp3]", "释义")
    host = FakeHost()
    context = lambda: OperationContext("audio-lab", 1, "audio-epoch")
    publications = []
    manager = SnapshotJobManager(
        operation_adapter=QueryOpAdapter(host, context),
        exporter=adapter,
        context_provider=context,
        publish=lambda snapshot, **kw: publications.append(snapshot),
    )
    job = manager.request_snapshot(excluded_deck_ids=[])
    host.pending.pop(0)(col)
    assert job.state == SnapshotJobState.PUBLISHED, str(job.error)
    snapshot = publications[0]
    assert snapshot.manifest.card_count == keep_count + 1
    assert snapshot.manifest.included_card_count == keep_count
    assert snapshot.manifest.skip_counts == {"audio_only_side": 1}
    assert snapshot.manifest.skipped[0].card_id == str(drop.id)
    assert [c.card_id for c in snapshot.cards] == ([str(keep.id)] if keep else [])
    assert snapshot.complete
    store = ProfileStore(tmp_path / "store", profile_id="audio-lab")
    try:
        store.set_collection_generation(1)
        published = SnapshotService(store).publish(
            snapshot,
            scope_id=job.scope_id,
            generation=1,
            source_scope={"kind": "exclude", "ids": []},
        )
        root = SnapshotFilesService(store)._prepare_snapshot(
            store.get_snapshot(published.snapshot_id)
        )
        assert root["card_count"] == keep_count
        if not keep_count:
            assert root["descriptor_pages"] == []
    finally:
        store.close()
