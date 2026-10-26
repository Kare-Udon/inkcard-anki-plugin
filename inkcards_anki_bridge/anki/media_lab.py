"""仅隔离候选启用的有序图文、无网点黑白 PNG 与原分辨率详情缓存。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import re
import unicodedata
from collections import OrderedDict
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from threading import RLock
from urllib.parse import unquote

try:
    from PIL import Image, ImageOps
except ModuleNotFoundError as error:
    if error.name != "PIL":
        raise
    # Anki 不带 Pillow 时只加载本插件的私有副本，不改全局搜索路径。
    import importlib.util
    import sys
    dependency = Path(__file__).resolve().parents[2] / "vendor" / "PIL"
    spec = importlib.util.spec_from_file_location(
        "PIL", dependency / "__init__.py", submodule_search_locations=[str(dependency)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("InkCards 图像依赖缺失，请重新安装配套插件") from error
    pillow = importlib.util.module_from_spec(spec)
    sys.modules["PIL"] = pillow
    spec.loader.exec_module(pillow)
    from PIL import Image, ImageOps

MAX_PACK = 98304
# 只缓存转换描述，不保留 PIL 图像；源字节摘要使同名图片更新立即失效。
_CONVERTED = OrderedDict()
_MAX_CONVERTED = 512
_CONVERTED_LOCK = RLock()
AUDIO_TOKEN_RE = re.compile(r'\[sound:[^\]]+\]|\[anki:play:[qa]:\d+\]', re.IGNORECASE)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def binarize(gray):
    threshold = 160
    low, high = gray.getextrema()
    if 160 <= low < 255 and low != high:
        # 浅色图标若直接用160会全部消失；用全局Otsu寻找前景分界。
        hist = gray.histogram()
        total = sum(hist)
        summed = sum(i * count for i, count in enumerate(hist))
        count = weighted = 0
        best = -1.0
        for i, frequency in enumerate(hist):
            count += frequency
            weighted += i * frequency
            if not count or count == total:
                continue
            score = (summed * count - weighted * total) ** 2 / (count * (total - count))
            if score > best:
                best, threshold = score, i + 1
    return gray.point(lambda p: 255 if p >= threshold else 0, mode="1")


def tile_starts(length, size):
    starts = [0]
    while starts[-1] + size < length:
        starts.append(starts[-1] + size - 32)
    return starts


def bind_media(block, locations):
    result = dict(block)
    if result.get("type") == "image":
        result.update(locations[result["id"]])
        if "detail" in result:
            result["detail"] = dict(
                result["detail"],
                tiles=[
                    bind_media(tile, locations) for tile in result["detail"]["tiles"]
                ],
            )
    return result


class Blocks(HTMLParser):
    def __init__(self, collection):
        super().__init__(convert_charrefs=True)
        self.media = Path(collection.media.dir()).resolve()
        self.cache = self.media.parent / "inkcards-media-lab-cache"
        self.blocks, self.assets, self.parts = [], {}, []
        self.ignored = 0
        self.audio_depth = 0
        self.has_audio = False
        self.pre_depth = 0

    def flush(self):
        text = "".join(self.parts)
        self.parts = []
        if text.strip():
            self.blocks.append({"type": "text", "text": text})

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template"):
            self.ignored += 1
            return
        if self.ignored:
            return
        if tag == "audio":
            if not self.audio_depth:
                self.audio_placeholder()
            self.audio_depth += 1
            return
        if self.audio_depth:
            return
        if tag in ("pre", "textarea"):
            self.pre_depth += 1
        if tag in ("video", "object", "embed", "source"):
            raise ValueError("unsupported_media")
        if tag in ("br", "p", "div", "li", "hr"):
            self.parts.append("\n")
        if tag != "img":
            return
        self.flush()
        name = unicodedata.normalize("NFC", unquote(a.get("src", "")))
        if (
            not name
            or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", name)
            or "/" in name
            or "\\" in name
            or name in (".", "..")
        ):
            self.unavailable_image("image_reference_invalid")
            return
        try:
            path = (self.media / name).resolve()
            readable_path = path.parent == self.media and path.is_file()
        except (OSError, ValueError, RuntimeError):
            readable_path = False
        if not readable_path:
            self.unavailable_image("image_missing")
            return
        width = a.get("width", "")
        match = re.search(
            r"(?:^|;)\s*width\s*:\s*(\d+)(?:px)?\s*(?:;|$)", a.get("style", "")
        )
        if match:
            width = match[1]
        requested = re.fullmatch(r"(\d+)(?:px)?", width)
        display_width = max(1, min(416, int(requested[1]))) if requested else None
        try:
            source = path.read_bytes()
        except (OSError, ValueError, Image.DecompressionBombError):
            self.unavailable_image('image_unreadable')
            return
        cache_key = (str(path), sha(source), str(self.cache), display_width)
        with _CONVERTED_LOCK:
            cached = _CONVERTED.get(cache_key)
            if cached:
                _CONVERTED.move_to_end(cache_key)
        if cached and all(Path(a['path']).is_file() for a in cached[1]):
            blocks, assets = deepcopy(cached)
            self.blocks.extend(blocks)
            self.assets.update((a['id'], a) for a in assets)
            return
        # 描述与 PNG 同时持久化；重启也无需解码、缩放和二值化原图。
        descriptor_key = sha(("mono-v1:" + cache_key[1] + ":" + str(display_width)).encode())
        descriptor = self.cache / ("conversion-" + descriptor_key + ".json")
        try:
            if descriptor.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("conversion manifest too large")
            saved = json.loads(descriptor.read_text())
            if saved["schema"] != "inkcards-conversion-v1":
                raise ValueError("conversion version")
            blocks, assets = saved["blocks"], saved["assets"]
            if not blocks or not assets:
                raise ValueError("conversion empty")
            for asset in assets:
                file = Path(asset["path"])
                if file.parent != self.cache or file.name != asset["id"] + ".png":
                    raise ValueError("conversion path")
                if sha(file.read_bytes()) != asset["id"]:
                    raise ValueError("conversion checksum")
            self.blocks.extend(blocks)
            self.assets.update((a["id"], a) for a in assets)
            with _CONVERTED_LOCK:
                _CONVERTED[cache_key] = deepcopy((blocks, assets))
                while len(_CONVERTED) > _MAX_CONVERTED:
                    _CONVERTED.popitem(last=False)
            return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            with Image.open(io.BytesIO(source)) as check:
                if getattr(check, "n_frames", 1) != 1:
                    self.unavailable_image("animated_image_unsupported")
                    return
                check.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            self.unavailable_image("image_unreadable")
            return
        first_block = len(self.blocks)
        with Image.open(io.BytesIO(source)) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("animated_image_unsupported")
            rgba = ImageOps.exif_transpose(image).convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            original = white.convert("L")
            gray = original.copy()
            target_width = display_width or 416
            if gray.height <= gray.width * 2 or display_width:
                gray.thumbnail((target_width, 280), Image.Resampling.LANCZOS)
            elif gray.width > target_width:
                target_h = max(1, round(gray.height * target_width / gray.width))
                gray = gray.resize((target_width, target_h), Image.Resampling.LANCZOS)
            native = binarize(original)
            # 详情只裁切；32px重叠使跨边缘笔画可在相邻视窗完整查看。
            detail_tiles = []
            for y in tile_starts(original.height, 560):
                for x in tile_starts(original.width, 416):
                    tile = native.crop(
                        (x, y, min(x + 416, native.width), min(y + 560, native.height))
                    )
                    detail_tiles.append(dict(self.cache_image(tile), x=x, y=y))
            detail = {
                "width": original.width,
                "height": original.height,
                "tiles": detail_tiles,
            }
            for top in range(0, gray.height, 280):
                tile = binarize(
                    gray.crop((0, top, gray.width, min(top + 280, gray.height)))
                )
                self.blocks.append(
                    dict(
                        self.cache_image(tile),
                        display_width=display_width or tile.width,
                        detail=detail,
                    )
                )

        blocks = self.blocks[first_block:]
        identities = {b['id'] for b in blocks}
        identities.update(t['id'] for b in blocks for t in b['detail']['tiles'])
        assets = [self.assets[i] for i in sorted(identities)]
        self.cache.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="conversion-", suffix=".tmp", dir=self.cache)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"schema": "inkcards-conversion-v1", "blocks": blocks, "assets": assets}, stream,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)
        with _CONVERTED_LOCK:
            _CONVERTED[cache_key] = deepcopy((blocks, assets))
            _CONVERTED.move_to_end(cache_key)
            while len(_CONVERTED) > _MAX_CONVERTED:
                _CONVERTED.popitem(last=False)

    def unavailable_image(self, reason):
        # 只降级源图片问题；缓存写入、包校验和提交错误仍然必须失败。
        self.blocks.append({'type': 'text', 'text': '\n[图片不可用]\n',
                            'media_warning': reason})

    def audio_placeholder(self):
        self.flush()
        self.has_audio = True
        self.blocks.append({'type': 'text', 'text': '\n[音频不可播放]\n',
                            'media_warning': 'audio_unavailable'})

    def cache_image(self, image):
        out = io.BytesIO()
        image.save(out, "PNG", optimize=True, compress_level=9)
        raw = out.getvalue()
        identity = sha(raw)
        self.cache.mkdir(exist_ok=True)
        cached = self.cache / (identity + ".png")
        if not cached.exists() or sha(cached.read_bytes()) != identity:
            fd, temporary = tempfile.mkstemp(prefix="png-", suffix=".tmp", dir=self.cache)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                os.replace(temporary, cached)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self.assets[identity] = {"id": identity, "path": str(cached)}
        return {
            "type": "image",
            "id": identity,
            "width": image.width,
            "height": image.height,
        }

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in ("audio", "script", "style", "noscript", "template", "pre", "textarea"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.ignored = max(0, self.ignored - 1)
        elif tag == "audio":
            self.audio_depth = max(0, self.audio_depth - 1)
        elif tag in ("pre", "textarea"):
            self.pre_depth = max(0, self.pre_depth - 1)
        elif not self.ignored and not self.audio_depth and tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, text):
        if not self.ignored and not self.audio_depth:
            if not self.pre_depth:
                # HTML 源码缩进不是可见空行；显式 br/块标签的换行另行保留。
                text = re.sub(r'[\t\n\r\f ]+', ' ', text)
            start = 0
            for match in AUDIO_TOKEN_RE.finditer(text):
                self.parts.append(text[start:match.start()])
                self.audio_placeholder()
                start = match.end()
            self.parts.append(text[start:])


def extract_blocks(html, collection):
    blocks, assets, _, _ = extract_side(html, collection)
    return blocks, assets


def extract_side(html, collection):
    """占位文字不计作音频卡的可学习内容；有效图片可以计入。"""
    parser = Blocks(collection)
    parser.feed(html)
    parser.close()
    parser.flush()
    meaningful = any(b['type'] == 'image' or
                     (b.get('text', '').strip() and not b.get('media_warning'))
                     for b in parser.blocks)
    return parser.blocks, list(parser.assets.values()), parser.has_audio, meaningful


def media_packages(assets):
    unique = {}
    for asset in assets:
        raw = Path(asset["path"]).read_bytes()
        if sha(raw) != asset["id"] or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("media_cache_corrupt")
        unique[asset["id"]] = raw
    packs, locations = [], {}
    group, body = [], bytearray(b"ICIM1\n")

    def finish():
        if not group:
            return
        raw = bytes(body)
        identity = sha(raw)
        name = "m-" + identity[:32] + ".bin"
        packs.append(
            {
                "id": identity,
                "sha256": identity,
                "name": name,
                "kind": "media",
                "bytes": len(raw),
                "record_count": len(group),
                "_body": raw,
            }
        )
        for image_id, offset, length in group:
            locations[image_id] = {"file": name, "offset": offset, "length": length}

    for identity, raw in sorted(unique.items()):
        if len(raw) + 6 > MAX_PACK:
            raise ValueError("media_image_too_large")
        if len(body) + len(raw) > MAX_PACK:
            finish()
            group, body = [], bytearray(b"ICIM1\n")
        group.append((identity, len(body) + 1, len(raw)))
        body.extend(raw)
    finish()
    return packs, locations
