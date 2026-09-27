"""一致性核查配对算法（app.matching）纯函数单元测试。"""

from datetime import datetime, timedelta, timezone

from app.matching import SideReading, match_pairs

_BASE = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def _r(i: int, minute: float, temp: float = 0.0) -> SideReading:
    """以 2026-09-20T08:00:00Z 为基准、偏移 ``minute`` 分钟构造一侧读数。"""

    return SideReading(
        id=i,
        event_time=(_BASE + timedelta(minutes=minute)).isoformat(),
        temperature_c=temp,
    )


def _ids(pairs):
    return [(p.sample_reading_id, p.logger_reading_id) for p in pairs]


def test_empty_sides_yield_no_pairs() -> None:
    assert match_pairs([], [_r(1, 0)], 60) == []
    assert match_pairs([_r(1, 0)], [], 60) == []
    assert match_pairs([], [], 60) == []


def test_basic_pairing_and_fields() -> None:
    pairs = match_pairs(
        [_r(11, 0, 4.0), _r(12, 10, -21.0)],
        [_r(21, 1, 4.5), _r(22, 11, -19.0)],
        600,
    )
    assert _ids(pairs) == [(11, 21), (12, 22)]
    assert pairs[0].time_diff_seconds == 60.0
    assert pairs[0].temperature_diff_c == 0.5
    assert pairs[1].temperature_diff_c == 2.0
    assert pairs[1].sample_event_time < pairs[1].logger_event_time


def test_max_pairs_dominates_total_time_diff() -> None:
    # 两对（总时差 80 分钟）优于一对（总时差 40 分钟）
    pairs = match_pairs([_r(1, 0), _r(2, 100)], [_r(3, 40), _r(4, 60)], 3600)
    assert _ids(pairs) == [(1, 3), (2, 4)]
    assert sum(p.time_diff_seconds for p in pairs) == 80 * 60


def test_min_total_time_diff_among_equal_counts() -> None:
    # 三选二：总时间差最小的组合是 (10,11) 与 (20,19)
    pairs = match_pairs(
        [_r(1, 10), _r(2, 20)],
        [_r(3, 11), _r(4, 19), _r(5, 100)],
        6000,
    )
    assert _ids(pairs) == [(1, 3), (2, 4)]
    assert sum(p.time_diff_seconds for p in pairs) == 120.0


def test_non_crossing_order_preserved() -> None:
    # 保序约束下唯一可行的大小为 2 的配对是 (10,13) 与 (20,25)
    pairs = match_pairs([_r(1, 10), _r(2, 20)], [_r(3, 13), _r(4, 25)], 600)
    assert _ids(pairs) == [(1, 3), (2, 4)]


def test_tie_broken_by_reading_id_sequence() -> None:
    # 两条直接读数与同一记录仪读数的时间差相同：按读数 ID 序列取最小，
    # 即选择 id=1（尽管它的事件时间更晚）
    pairs = match_pairs([_r(9, 0), _r(1, 4)], [_r(3, 2)], 240)
    assert _ids(pairs) == [(1, 3)]


def test_boundary_time_diff_is_allowed() -> None:
    # 恰好等于最大时间差仍可配对
    pairs = match_pairs([_r(1, 0)], [_r(2, 5)], 300)
    assert _ids(pairs) == [(1, 2)]
    # 差 301 秒则不可配对
    assert match_pairs([_r(1, 0)], [_r(2, 0)], 0) != []
    assert match_pairs([_r(1, 0)], [_r(2, 5)], 299) == []


def test_zero_max_time_diff_requires_same_instant() -> None:
    pairs = match_pairs([_r(1, 0), _r(2, 1)], [_r(3, 0), _r(4, 0.5)], 0)
    assert _ids(pairs) == [(1, 3)]


def test_unmatched_readings_on_both_sides() -> None:
    pairs = match_pairs(
        [_r(1, 0), _r(2, 10), _r(3, 20)],
        [_r(4, 1), _r(5, 500)],
        120,
    )
    assert _ids(pairs) == [(1, 4)]


def test_result_sorted_by_event_time_regardless_of_input_order() -> None:
    pairs = match_pairs(
        [_r(2, 10), _r(1, 0)],
        [_r(4, 11), _r(3, 1)],
        600,
    )
    assert _ids(pairs) == [(1, 3), (2, 4)]
    assert pairs[0].sample_event_time < pairs[1].sample_event_time
