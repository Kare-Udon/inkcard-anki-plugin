"""InkCards Anki UI。

``presenter`` 是不依赖 Anki/Qt 的纯 Python 状态层；Qt 只在 host/dialog
接线真正被调用时延迟导入。
"""

from .presenter import (
    ButtonState,
    CounterView,
    DeckItem,
    InkCardsPresenter,
    StatusView,
    SyncOverview,
    ViewState,
    build_status_view,
    build_sync_overview,
    is_valid_device_id,
    safe_error_message,
)

__all__ = [
    "ButtonState",
    "CounterView",
    "DeckItem",
    "InkCardsPresenter",
    "StatusView",
    "SyncOverview",
    "ViewState",
    "build_status_view",
    "build_sync_overview",
    "is_valid_device_id",
    "safe_error_message",
]
