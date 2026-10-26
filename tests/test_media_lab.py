"""隔离图像实验：引用顺序、编码和二进制包回归。"""

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.anki.media_lab import (
    bind_media,
    extract_blocks,
    media_packages,
)
from PIL import Image


class MediaLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "collection.media"
        self.media.mkdir()
        self.col = SimpleNamespace(media=SimpleNamespace(dir=lambda: str(self.media)))
        Image.new("RGBA", (100, 40), (0, 0, 0, 0)).save(self.media / "图 x.png")

    def tearDown(self):
        self.temp.cleanup()

    def test_repeated_reference_reuses_conversion_but_returns_independent_blocks(self):
        import inkcards_anki_bridge.anki.media_lab as module
        with patch.object(module, 'binarize', wraps=module.binarize) as convert:
            first, _ = extract_blocks('<img src="图 x.png">', self.col)
            calls = convert.call_count
            first[0]['detail']['tiles'][0]['x'] = 999
            again, _ = extract_blocks('<img src="图 x.png">', self.col)
            self.assertEqual(convert.call_count, calls, '同源同尺寸不重复二值化编码')
            self.assertEqual(again[0]['detail']['tiles'][0]['x'], 0)

    def test_changed_source_same_stat_is_not_stale(self):
        import os
        path = self.media / '图 x.png'
        initial, _ = extract_blocks('<img src="图 x.png">', self.col)
        before = path.stat()
        Image.new('RGB', (100, 40), 'black').save(path)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed, _ = extract_blocks('<img src="图 x.png">', self.col)
        self.assertNotEqual(initial[0]['id'], changed[0]['id'])
        small, _ = extract_blocks('<img src="图 x.png" width="15">', self.col)
        self.assertEqual(small[0]['width'], 15)

    def test_deleted_cache_is_regenerated(self):
        _, assets = extract_blocks('<img src="图 x.png">', self.col)
        for asset in assets:
            Path(asset['path']).unlink(missing_ok=True)
        _, again = extract_blocks('<img src="图 x.png">', self.col)
        self.assertTrue(all(Path(a['path']).is_file() for a in again))

    def test_order_alpha_and_requested_size(self):
        blocks, assets = extract_blocks(
            '前文<br><img src="图%20x.png" width="15px">后文', self.col
        )
        self.assertEqual([b["type"] for b in blocks], ["text", "image", "text"])
        self.assertEqual(blocks[0]["text"], "前文\n")
        self.assertEqual(blocks[1]["display_width"], 15)
        self.assertEqual(blocks[1]["width"], 15, "先按模板尺寸缩小，再分片")
        raw = Path(assets[0]["path"]).read_bytes()
        self.assertEqual(raw[24:26], bytes([1, 0]), "1-bit 灰度 PNG，无调色板网点")
        with Image.open(io.BytesIO(raw)) as im:
            self.assertEqual(im.convert("L").getextrema(), (255, 255))

    def test_html_source_indentation_collapses_but_explicit_breaks_and_pre_survive(self):
        blocks, _ = extract_blocks('\n  前文\n \t<b>后文</b> \r\n尾文<br>下一行', self.col)
        self.assertEqual(blocks[0]['text'].strip(), '前文 后文 尾文\n下一行')
        blocks, _ = extract_blocks('<pre>第一行\n  第二行</pre>', self.col)
        self.assertEqual(blocks[0]['text'], '第一行\n  第二行')

    def test_pure_image_and_dedup_binary_offsets(self):
        blocks, assets = extract_blocks(
            '<img src="图 x.png"><img src="图 x.png">', self.col
        )
        self.assertEqual(len(blocks), 2)
        packs, locations = media_packages(assets)
        self.assertEqual(sum(p["record_count"] for p in packs), 1)
        loc = locations[blocks[0]["id"]]
        raw = packs[0]["_body"]
        self.assertEqual(
            raw[loc["offset"] - 1 : loc["offset"] - 1 + loc["length"]],
            Path(assets[0]["path"]).read_bytes(),
        )

    def test_long_image_tiles_retain_full_height(self):
        Image.new("RGB", (200, 900), "gray").save(self.media / "long.png")
        blocks, _ = extract_blocks('<img src="long.png">', self.col)
        self.assertEqual(sum(b["height"] for b in blocks), 900)
        self.assertTrue(all(b["height"] <= 280 for b in blocks))

    def test_invalid_image_is_placeholder_in_original_order(self):
        for src in ['https://example.com/a.png', '../secret.png', 'missing.png',
                    'blob:ankifile://editor/expired', 'http://[', '\x00bad.png']:
            with self.subTest(src=src):
                blocks, assets = extract_blocks(f'前文<img src="{src}">后文<img src="图 x.png">', self.col)
                self.assertEqual([b['type'] for b in blocks], ['text', 'text', 'text', 'image'])
                self.assertIn('图片不可用', blocks[1]['text'])
                self.assertEqual(blocks[2]['text'], '后文')
                self.assertTrue(assets)

    def test_unreadable_and_animated_source_do_not_block_card(self):
        (self.media / 'bad.png').write_bytes(b'not-a-png')
        frames = [Image.new('RGB', (10, 10), c) for c in ('white', 'black')]
        frames[0].save(self.media / 'animated.gif', save_all=True, append_images=frames[1:])
        for name in ['bad.png', 'animated.gif']:
            blocks, assets = extract_blocks(f'<img src="{name}">', self.col)
            self.assertEqual(blocks[0]['type'], 'text')
            self.assertIn('图片不可用', blocks[0]['text'])
            self.assertFalse(assets)


    def test_unsupported_media_stays_rejected(self):
        with self.assertRaises(ValueError):
            extract_blocks('<video src="x.mp4"></video>', self.col)

    def test_pack_hash_checks_cache_content(self):
        _, assets = extract_blocks('<img src="图 x.png">', self.col)
        Path(assets[0]["path"]).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            media_packages(assets)

    def test_native_detail_keeps_source_pixels_and_covers_edges(self):
        image = Image.new("L", (837, 661), 255)
        for x in range(837):
            image.putpixel((x, x % 661), 70)
        image.save(self.media / "writing.png")
        blocks, assets = extract_blocks('<img src="writing.png">', self.col)
        self.assertEqual(len(blocks), 1)
        detail = blocks[0]["detail"]
        self.assertEqual((detail["width"], detail["height"]), image.size)
        by_id = {a["id"]: a for a in assets}
        coverage = Image.new("1", image.size)
        for tile in detail["tiles"]:
            self.assertLessEqual(tile["width"], 416)
            self.assertLessEqual(tile["height"], 560)
            x, y = tile["x"], tile["y"]
            with Image.open(by_id[tile["id"]]["path"]) as decoded:
                expected = image.crop(
                    (x, y, x + tile["width"], y + tile["height"])
                ).point(lambda p: 255 if p >= 160 else 0, mode="1")
                self.assertEqual(
                    decoded.tobytes(), expected.tobytes(), "详情只裁切，不缩放"
                )
            coverage.paste(1, (x, y, x + tile["width"], y + tile["height"]))
        self.assertEqual(coverage.getextrema(), (1, 1), "包含源图右下边缘，无缺口")
        self.assertTrue(all("detail" not in tile for tile in detail["tiles"]))

    def test_no_dither_uses_fixed_handwriting_threshold(self):
        image = Image.new("L", (40, 10), 180)
        image.paste(70, (0, 0, 10, 10))
        image.save(self.media / "ink.png")
        blocks, assets = extract_blocks('<img src="ink.png">', self.col)
        raw = next(
            Path(a["path"]).read_bytes() for a in assets if a["id"] == blocks[0]["id"]
        )
        with Image.open(io.BytesIO(raw)) as decoded:
            self.assertEqual(
                decoded.convert("L").crop((10, 0, 40, 10)).getextrema(), (255, 255)
            )

    def test_pale_ink_not_erased(self):
        image = Image.new("L", (40, 40), 255)
        image.paste(235, (5, 5, 35, 35))
        image.save(self.media / "pale.png")
        blocks, assets = extract_blocks('<img src="pale.png">', self.col)
        raw = next(
            Path(a["path"]).read_bytes() for a in assets if a["id"] == blocks[0]["id"]
        )
        with Image.open(io.BytesIO(raw)) as decoded:
            self.assertEqual(decoded.convert("L").getextrema(), (0, 255))

    def test_detail_references_are_bound_to_transferred_packages(self):
        blocks, assets = extract_blocks('<img src="图 x.png">', self.col)
        packs, locations = media_packages(assets)
        bound = bind_media(blocks[0], locations)
        for tile in [bound] + bound["detail"]["tiles"]:
            raw = next(p["_body"] for p in packs if p["name"] == tile["file"])
            png = raw[tile["offset"] - 1 : tile["offset"] - 1 + tile["length"]]
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(png[24:26], bytes([1, 0]))
            self.assertNotIn("path", tile)
