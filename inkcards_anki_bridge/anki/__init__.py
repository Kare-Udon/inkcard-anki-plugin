"""Anki 固定版本只读兼容层。

本包只包含标准库可导入的模型、能力门槛、只读适配器和宿主操作边界。
真正的 ``aqt``/``anki`` 导入由调用方在 Anki 进程内延迟注入。
"""

from .compatibility import (
    SUPPORTED_ANKI_BUILD,
    SUPPORTED_ANKI_VERSION,
    AnkiCompatibility,
    CompatibilityError,
    check_anki_compatibility,
    require_anki_compatibility,
)
from .adapter_260801 import Anki260801Adapter, Anki260801ReadOnlyAdapter, ReadOnlyAnkiAdapter
from .catalog_jobs import (
    CatalogJob,
    CatalogJobError,
    CatalogJobManager,
    CatalogJobState,
    DeckCatalog,
    DeckCatalogEntry,
    DeckPreflight,
    make_preflight_scope,
)
from .models import AnkiSnapshot, DueKind, SkipReason, TypedSnapshot
from .application_context import CapturedApplicationContext, SnapshotApplicationContext
from .snapshot_jobs import (
    SnapshotJob,
    SnapshotJobError,
    SnapshotJobManager,
    SnapshotJobState,
    SnapshotScope,
    make_scope,
)

__all__ = [
    "AnkiCompatibility",
    "Anki260801Adapter",
    "Anki260801ReadOnlyAdapter",
    "AnkiSnapshot",
    "CatalogJob",
    "CatalogJobError",
    "CatalogJobManager",
    "CatalogJobState",
    "CapturedApplicationContext",
    "CompatibilityError",
    "DeckCatalog",
    "DeckCatalogEntry",
    "DeckPreflight",
    "DueKind",
    "SUPPORTED_ANKI_BUILD",
    "SUPPORTED_ANKI_VERSION",
    "ReadOnlyAnkiAdapter",
    "SnapshotJob",
    "SnapshotJobError",
    "SnapshotJobManager",
    "SnapshotJobState",
    "SnapshotApplicationContext",
    "SnapshotScope",
    "SkipReason",
    "TypedSnapshot",
    "check_anki_compatibility",
    "require_anki_compatibility",
    "make_preflight_scope",
    "make_scope",
]
