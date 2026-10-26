"""包摘要只提供保守下界与准确的整组计数，不改变原有记录。"""
from inkcards_anki_bridge.services.snapshot_files import _queue_summary


def test_queue_summary_bounds_flags_bury_and_exact_timestamps():
    schedules = {
        'z': dict(deck_id='d', state='new', queue_order=2),
        'a': dict(deck_id='d', state='new', queue_order=9),
        'l': dict(deck_id='d', state='learning', queue_order=3, due_at_ms=1792000000123),
        'r': dict(deck_id='d', state='relearning', queue_order=7, due_at_ms=1792000000456),
        'v': dict(deck_id='d', state='review', queue_order=1, due_collection_day=-2, buried_until_day='today'),
        's': dict(deck_id='d', state='new', queue_order=0, suspended=True),
        'p': dict(deck_id='d', state='new', queue_order=0, paused=True),
        'b': dict(deck_id='d', state='new', queue_order=0, buried=True),
    }
    for key, card in schedules.items():
        card['card_id'] = key
    before = {key: dict(value) for key, value in schedules.items()}
    summary = _queue_summary([dict(card_id=key) for key in schedules], schedules)
    assert summary == [
        ['d', 'learning', '', 2, 1792000000123, 1792000000456, 3, 'l'],
        ['d', 'new', '', 2, 0, 0, 2, 'a'],
        ['d', 'review', 'today', 1, -2, -2, 1, 'v'],
    ]
    assert schedules == before
