"""InkCards Anki add-on entry point.

加载 add-on 只注册生命周期 hook；runtime 等到 ``profile_did_open`` 后才
启动。普通 Python 测试没有 ``aqt``，因而不会注册 hook 或创建用户文件。
"""

from __future__ import annotations

import sys

_controller = None

if "aqt" in sys.modules:
    from .inkcards_anki_bridge.plugin import register_in_anki

    try:
        _controller = register_in_anki(addon_module_name=__name__)
    except Exception as exc:  # Anki 启动失败不应阻塞主程序
        print(f"[InkCards] lifecycle disabled: {type(exc).__name__}: {exc}")


def shutdown() -> None:
    """供插件卸载、测试和 Anki 退出接线调用的幂等清理入口。"""

    global _controller
    controller = _controller
    _controller = None
    if controller is not None:
        controller.shutdown()
