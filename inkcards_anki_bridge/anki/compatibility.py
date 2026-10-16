"""已核对 Anki 版本和 build 的显式只读能力门槛。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple


# Anki 的版本显示有时保留 minor 的前导零；内部统一使用无前导零形式。
SUPPORTED_ANKI_VERSION = "26.08.1"
SUPPORTED_ANKI_VERSION_CANONICAL = "26.8.1"
SUPPORTED_ANKI_BUILD = "39e4b0b4"
SUPPORTED_ANKI_TARGETS = {
    SUPPORTED_ANKI_VERSION_CANONICAL: SUPPORTED_ANKI_BUILD,
    "26.9.3": "29bb700b",
}

READONLY_CAPABILITIES: Tuple[str, ...] = (
    "readonly_query",
    "typed_due",
    "text_snapshot",
    "deck_preset_export",
    "day_table_export",
)

_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\s*$")


class CompatibilityError(Exception):
    """版本或 build 不在固定 allowlist 时抛出的错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_anki_version(value: str) -> str:
    """把版本转为无前导零的三段形式，不接受未知格式。"""

    if not isinstance(value, str):
        raise CompatibilityError("invalid_anki_version", "Anki version must be a string")
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise CompatibilityError("invalid_anki_version", "Anki version is not numeric")
    return ".".join(str(int(part)) for part in match.groups())


@dataclass(frozen=True)
class AnkiCompatibility:
    """一次不可变的兼容性判定结果。

    ``supported=False`` 是未知版本、未知 build 和格式错误的统一结果；不会返回
    “可能兼容”的模糊状态。调用方可以先展示 ``unsupported``，再决定是否阻断。
    """

    supported: bool
    version: Optional[str]
    build: Optional[str]
    exact_version: bool
    exact_build: bool
    capabilities: Tuple[str, ...] = ()
    unsupported: Tuple[str, ...] = ()
    reason: Optional[str] = None

    @property
    def exact(self) -> bool:
        return self.exact_version and self.exact_build

    @property
    def is_supported(self) -> bool:
        return self.supported

    @property
    def unsupported_capabilities(self) -> Tuple[str, ...]:
        return self.unsupported

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "version": self.version,
            "build": self.build,
            "exact_version": self.exact_version,
            "exact_build": self.exact_build,
            "capabilities": list(self.capabilities),
            "unsupported": list(self.unsupported),
            "reason": self.reason,
        }


def check_anki_compatibility(version: str, build: Optional[str]) -> AnkiCompatibility:
    """检查固定 version/build；未知值一律 fail closed。"""

    try:
        canonical = canonical_anki_version(version)
    except CompatibilityError as exc:
        return AnkiCompatibility(
            supported=False,
            version=None,
            build=str(build) if build is not None else None,
            exact_version=False,
            exact_build=False,
            unsupported=(exc.code,),
            reason=exc.message,
        )

    observed_build = build.strip().lower() if isinstance(build, str) else None
    exact_version = canonical in SUPPORTED_ANKI_TARGETS
    exact_build = exact_version and observed_build == SUPPORTED_ANKI_TARGETS[canonical]
    unsupported = []
    if not exact_version:
        unsupported.append("unsupported_anki_version")
    if not exact_build:
        unsupported.append("unsupported_anki_build")
    supported = exact_version and exact_build
    reason = None if supported else "Anki version/build is not in the fixed compatibility allowlist"
    return AnkiCompatibility(
        supported=supported,
        version=canonical,
        build=observed_build,
        exact_version=exact_version,
        exact_build=exact_build,
        capabilities=READONLY_CAPABILITIES if supported else (),
        unsupported=tuple(unsupported),
        reason=reason,
    )


def require_anki_compatibility(version: str, build: Optional[str]) -> AnkiCompatibility:
    """要求 exact version/build，否则抛出可分类错误。"""

    result = check_anki_compatibility(version, build)
    if not result.supported:
        if not result.exact_version:
            code = "unsupported_anki_version"
        else:
            code = "unsupported_anki_build"
        raise CompatibilityError(code, result.reason or "unsupported Anki version/build")
    return result


# 更短的兼容别名，方便宿主/测试按语义调用。
check_compatibility = check_anki_compatibility
require_compatibility = require_anki_compatibility
detect_anki_compatibility = check_anki_compatibility


__all__ = [
    "AnkiCompatibility",
    "CompatibilityError",
    "READONLY_CAPABILITIES",
    "SUPPORTED_ANKI_BUILD",
    "SUPPORTED_ANKI_VERSION",
    "SUPPORTED_ANKI_VERSION_CANONICAL",
    "SUPPORTED_ANKI_TARGETS",
    "canonical_anki_version",
    "check_anki_compatibility",
    "check_compatibility",
    "detect_anki_compatibility",
    "require_anki_compatibility",
    "require_compatibility",
]
