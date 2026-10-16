"""InkCards x4-2 纯 domain 导出。

这里不导入 Anki、HTTP 或 SQLite；外层适配器以后只负责把持久化和传输数据
转换为本模块定义的输入，并消费状态机给出的动作。
"""

from . import sync_gate as _sync_gate
from .sync_gate import *  # noqa: F403

__all__ = list(_sync_gate.__all__)
