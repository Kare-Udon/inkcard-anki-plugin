"""InkCards Anki 插件的轻量元数据入口。

这里必须保持为普通 Python 可导入的最小包：导入插件包本身不触发旧的
``protocol.py``、``aqt`` 或 Qt UI。Anki 宿主入口由顶层 ``__init__.py``
和 ``plugin.py`` 按需加载。
"""

__version__ = "0.2.0-preview.20"

__all__ = ["__version__"]
