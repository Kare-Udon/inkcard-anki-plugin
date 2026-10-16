"""以 Anki 给出的下一次换日时间为锚，导出宿主本地日历边界。"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional, Tuple


def local_day_boundaries(cutoff_ms: int, days: int = 90) -> Optional[Tuple[int, Tuple[int, ...]]]:
    """返回当前学习日的起点和未来逐日截止点；遇到本地时间歧义时拒绝推算。"""
    if cutoff_ms < 0 or cutoff_ms % 1000 or days < 1:
        return None
    try:
        anchor = time.localtime(cutoff_ms // 1000)
        anchor_date = date(anchor.tm_year, anchor.tm_mon, anchor.tm_mday)
        clock = (anchor.tm_hour, anchor.tm_min, anchor.tm_sec)
        boundaries = []
        for offset in range(-1, days):
            local_date = anchor_date + timedelta(days=offset)
            seconds = int(time.mktime((
                local_date.year, local_date.month, local_date.day,
                *clock, 0, 0, -1,
            )))
            resolved = time.localtime(seconds)
            wall = (local_date.year, local_date.month, local_date.day, *clock)
            if (resolved.tm_year, resolved.tm_mon, resolved.tm_mday,
                    resolved.tm_hour, resolved.tm_min, resolved.tm_sec) != wall:
                return None
            # 回拨时同一墙钟时刻可能出现两次；不能擅自选其中一个。
            for is_dst in (0, 1):
                candidate = int(time.mktime((*wall, 0, 0, is_dst)))
                check = time.localtime(candidate)
                if candidate != seconds and (check.tm_year, check.tm_mon, check.tm_mday,
                        check.tm_hour, check.tm_min, check.tm_sec) == wall:
                    return None
            boundaries.append(seconds * 1000)
    except (OverflowError, OSError, ValueError):
        return None
    if boundaries[1] != cutoff_ms or any(a >= b for a, b in zip(boundaries, boundaries[1:])):
        return None
    return boundaries[0], tuple(boundaries[1:])
