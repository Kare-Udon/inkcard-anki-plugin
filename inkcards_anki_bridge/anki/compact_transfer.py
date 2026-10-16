"""四态事件投影：只在隔离 scratch 评分，不改宿主时钟或用户库。"""

from __future__ import annotations

import copy
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


def project_native_answer(
    collection: Any,
    before: Mapping[str, Any],
    config: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    today: int,
    cutoff: int,
    event_day: int,
) -> dict[str, Any]:
    from anki.collection import Collection, Config
    from anki.scheduler_pb2 import CardAnswer
    from anki.models import StockNotetypeKind
    from anki.utils import from_json_bytes
    from .compact_260903 import CompactNativeError, _capture_card, _proto_dict
    from .compact_projection import json_sha256, map_rating

    card_id = int(before["card_id"])
    event_seconds = int(event["answered_at_ms"]) // 1000
    now = int(time.time())
    shift = now - event_seconds
    event_cutoff = cutoff - (today - event_day) * 86400
    last_review_shift = cutoff - event_cutoff
    # 完整 raw Card 从正式库只读取得；note/deck 仅在 scratch 映射。
    raw_card = collection._backend.get_card(card_id)
    if raw_card.original_deck_id or raw_card.queue not in (0, 1, 2, 3):
        raise CompactNativeError("filtered_card_unsupported", "筛选/隐藏卡片不可移植")
    with tempfile.TemporaryDirectory(prefix="inkcards-compact-transfer-") as directory:
        scratch = Collection(str(Path(directory) / "projection.anki2"))
        try:
            for key in ("rollover", "localOffset", "creationOffset"):
                scratch.set_config(key, collection.get_config(key))
            scratch.db.execute("UPDATE col SET crt=?", int(collection.crt))
            if (
                int(scratch.sched.today) != today
                or int(scratch.sched.day_cutoff) != cutoff
            ):
                raise CompactNativeError(
                    "projection_clock_mismatch", "scratch 日界与宿主不符"
                )
            scratch.set_config("fsrs", True)
            scratch.set_config_bool(
                Config.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED,
                collection.fsrs_short_term_with_steps_enabled,
            )
            identity_config = scratch.decks.config_dict_for_deck_id(1)
            native_config = copy.deepcopy(dict(config["native_config"]))
            native_config.update(mod=0, usn=-1, name="isolated projection")
            native_config["id"] = identity_config["id"]
            scratch.decks.save(native_config)
            scratch._backend.set_load_balancer_enabled(False)
            # 官方 stock kind 创建模型，再按返回 ID 取回，避免宿主语言改变名称。
            basic_model = from_json_bytes(
                scratch._backend.get_stock_notetype_legacy(StockNotetypeKind.KIND_BASIC)
            )
            basic_model_id = scratch.models.add_dict(basic_model).id
            note = scratch.new_note(scratch.models.get(basic_model_id))
            note.fields[0], note.fields[1] = (
                "isolated projection",
                "isolated projection",
            )
            scratch.add_note(note, 1)
            generated = int(scratch.card_ids_of_note(note.id)[0])
            scratch.db.execute("UPDATE cards SET id=? WHERE id=?", card_id, generated)
            raw_card.note_id, raw_card.deck_id = note.id, 1
            if raw_card.queue == 1:
                raw_card.due += shift
            elif raw_card.queue in (2, 3):
                raw_card.due += today - event_day
            if raw_card.last_review_time_secs:
                # FSRS 以 cutoff-lrt 算日数；学习 elapsed 秒仍由 now-due 计算。
                raw_card.last_review_time_secs += last_review_shift
            scratch._backend.update_cards(cards=[raw_card], skip_undo_entry=True)
            source_history = []
            if not raw_card.last_review_time_secs:
                # 旧卡无持久 lrt 时，Anki 仍会读取原 revlog 的 FSRS 历史依据。
                # 只复制到一次性 scratch 并平移日界，不给原卡/快照补造 lrt。
                source_history = collection.db.all(
                    "SELECT id,cid,usn,ease,ivl,lastIvl,factor,time,type FROM revlog WHERE cid=? ORDER BY id",
                    card_id,
                )
                if any(int(row[0]) >= int(event["answered_at_ms"]) and int(row[3]) > 0 for row in source_history):
                    raise CompactNativeError("event_precedes_last_review", "旧卡真实复习日志晚于事件，拒绝投影")
                for row in source_history:
                    if int(row[0]) < int(event["answered_at_ms"]):
                        scratch.db.execute(
                            "INSERT INTO revlog(id,cid,usn,ease,ivl,lastIvl,factor,time,type) VALUES (?,?,?,?,?,?,?,?,?)",
                            int(row[0]) + last_review_shift * 1000, card_id, *row[2:],
                        )
            states = scratch._backend.get_scheduling_states(card_id)
            rating, name = map_rating(event["rating"])
            selected = getattr(states, name)
            normal = selected.normal
            kind = normal.WhichOneof("kind")
            if kind not in ("learning", "review", "relearning"):
                raise CompactNativeError(
                    "answer_state_unsupported", "原生评分未进入已验证状态"
                )
            review = (
                normal.review
                if kind == "review"
                else normal.relearning.review
                if kind == "relearning"
                else None
            )
            if review is not None and review.leeched:
                raise CompactNativeError(
                    "leech_transfer_unsupported", "leech 标记/暂停副作用尚未验证"
                )
            # 时间平移不得悄悄跨越不同的原生日切分类。
            learning = (
                normal.learning
                if kind == "learning"
                else normal.relearning.learning
                if kind == "relearning"
                else None
            )
            answer = CardAnswer(
                card_id=card_id,
                current_state=states.current,
                new_state=selected,
                rating=rating,
                answered_at_millis=now * 1000,
                milliseconds_taken=int(event["duration_ms"]),
            )
            scratch.sched.answer_card(answer)
            after = _capture_card(scratch.get_card(card_id))
            rows = scratch.db.all(
                "SELECT id,cid,ease,ivl,lastIvl,time,type,factor FROM revlog WHERE cid=? AND id=?",
                card_id, now * 1000,
            )
            if len(rows) != 1:
                raise CompactNativeError(
                    "projection_revlog_invalid", "scratch 未产生唯一原生评分日志"
                )
            log = dict(
                zip(
                    (
                        "id",
                        "card_id",
                        "ease",
                        "interval",
                        "last_interval",
                        "review_time",
                        "type",
                        "factor",
                    ),
                    map(int, rows[0]),
                )
            )
            log["id"] = int(event["answered_at_ms"])
            if learning is not None:
                # 两侧必须是同一原生日级/分钟类别及日偏移，未知边界在 intent 前拒绝。
                scheduled = int(learning.scheduled_secs)
                if after["queue"] == 1:
                    actual_delay = after["due"] - now
                    if event_seconds + actual_delay >= event_cutoff:
                        raise CompactNativeError(
                            "event_cutoff_transfer_unsupported",
                            "事件分钟步跨日，当前时钟投影不能证明原生 revlog 类别",
                        )
                elif after["queue"] == 3:
                    expected_offset = (
                        event_seconds + scheduled - event_cutoff
                    ) // 86400 + 1
                    if expected_offset <= 0 or after["due"] - today != expected_offset:
                        raise CompactNativeError(
                            "event_cutoff_transfer_unsupported",
                            "两侧日级学习偏移不同，拒绝时间平移",
                        )
                else:
                    raise CompactNativeError(
                        "learning_queue_transfer_unsupported", "原生学习 queue 不可移植"
                    )
            historical_card = dict(after)
            historical_card.pop("schedule_revision", None)
            historical_card.update(
                card_id=before["card_id"],
                note_id=before["note_id"],
                deck_id=before["deck_id"],
                last_review_time=event_seconds,
            )
            if after["queue"] == 1:
                historical_card["due"] -= shift
            elif after["queue"] in (2, 3):
                historical_card["due"] -= today - event_day
            native_after = scratch._backend.get_card(card_id)
            native_after.note_id, native_after.deck_id = (
                int(before["note_id"]),
                int(before["deck_id"]),
            )
            native_after.due = historical_card["due"]
            native_after.last_review_time_secs = event_seconds
            native_after.mtime_secs, native_after.usn = 0, 0
            return {
                "native_card": _proto_dict(native_after),
                "states": states,
                "card": historical_card,
                "runtime_card": after,
                "log": log,
                "projection": {
                    "rating": event["rating"],
                    "rating_name": name,
                    "anki_rating": rating,
                    "answered_at_ms": event["answered_at_ms"],
                    "duration_ms": event["duration_ms"],
                    "event_collection_day": event_day,
                    "runtime_today": today,
                    "historical_due": historical_card["due"],
                    "runtime_due": after["due"],
                    "interval_days": historical_card["ivl"],
                    "clock_method": "isolated-relative-time-transfer-v2",
                    "shift_seconds": shift,
                    "last_review_shift_seconds": last_review_shift,
                    "native_source_history_count": len(source_history),
                    "native_source_history_sha256": json_sha256(source_history) if source_history else None,
                    "event_cutoff_seconds": event_cutoff,
                    "fsrs_elapsed_days": max(
                        0, event_cutoff - int(before["last_review_time"])
                    )
                    // 86400
                    if before["last_review_time"]
                    else 0,
                    "native_current_state": _proto_dict(states.current),
                },
            }
        except CompactNativeError:
            raise
        except Exception as exc:
            raise CompactNativeError(
                "isolated_projection_failed", "隔离原生规划失败，正式库未评分"
            ) from exc
        finally:
            scratch.close()
