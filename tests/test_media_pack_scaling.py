"""全库首次打包避免随槽位增长反复编码；保留原来的字节与分组。"""

from unittest.mock import patch

import pytest
from inkcards_anki_bridge.services.snapshot_files import (
    ArtifactError,
    SnapshotFilesService,
    _package,
)
from inkcards_anki_bridge.storage import ProfileStore


@pytest.mark.parametrize("existing", [False, True])
def test_new_records_encode_linearly_and_keep_package_bytes(tmp_path, existing):
    store = ProfileStore(tmp_path, profile_id="pack-lab")
    service = SnapshotFilesService(store)
    records = {
        str(i): {"card_id": str(i), "question": "汉字" * (i % 7 + 1)}
        for i in range(256)
    }
    entries = list(records.items())
    old = {0: list(records)[:20]} if existing else {}
    expected = service._chunk_group(entries, limit=4096)
    try:
        with (
            patch.object(service, "_old_slots", return_value=old),
            patch.object(SnapshotFilesService, "_frame", wraps=service._frame) as frame,
        ):
            files, _, _ = service._stable_files(
                scope_id="lab", kind="text", records=records, package_limit=4096
            )
            assert frame.call_count <= 4 * len(records)
        assert [f["_body"] for f in files] == [
            _package([r for _, r in chunk], limit=4096) for chunk in expected
        ]
        with (
            patch.object(service, "_old_slots", return_value={}),
            pytest.raises(ArtifactError, match="artifact_package_too_large"),
        ):
            service._stable_files(
                scope_id="lab",
                kind="text",
                records={"1": {"value": "大" * 2000}},
                package_limit=4096,
            )
    finally:
        store.close()
