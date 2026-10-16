"""InkCards 插件持久层的错误类型。"""

from __future__ import annotations


class StorageError(Exception):
    """表示插件数据库操作失败，并提供稳定的机器可读错误码。"""

    def __init__(self, message: str, code: str = "storage_error") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StorageValidationError(StorageError):
    """表示输入事件、身份或状态不满足持久层契约。"""


class SequenceError(StorageValidationError):
    """表示 session 序号缺口、重复、倒序或超出安全整数范围。"""


class ReceiptRegressionError(StorageValidationError):
    """表示尝试把交付或应用状态回退到较早状态。"""


class ProfileMismatchError(StorageValidationError):
    """表示数据库已经绑定到另一个 Anki profile。"""


class UnknownApplyError(StorageValidationError):
    """表示应用结果未知，因而不能自动重新应用。"""


class UnsupportedDatabaseError(StorageError):
    """表示目标文件不是受支持的 SQLite 数据库。"""


class UnsupportedSchemaError(StorageError):
    """表示数据库 schema 版本不在可迁移的明确 allowlist 中。"""


class SchemaError(StorageError):
    """表示 schema 表、列或约束与 InkCards 预期不一致。"""


class BusyStorageError(StorageError):
    """表示 SQLite 在有限 busy timeout 内仍被其他连接占用。"""


class CommitUnknownError(StorageError):
    """表示提交结果不可确认，因此调用方不能收到 durable receipt。"""
