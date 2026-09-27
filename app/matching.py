"""样本直接读数与共享记录仪归属读数的一对一温度配对（纯函数，不依赖数据库与 Web 框架）。

用于“样本 × 记录仪”温度一致性核查：

* 两侧读数分别按 ``(event_time, id)`` 升序排列；
* 仅当两条读数事件时间差的绝对值不超过 ``max_time_diff_seconds`` 时才允许配对；
* 配对一对一且**不交叉**（保序）：若直接读数 ``a1`` 排在 ``a2`` 之前且都参与配对，
  则 ``a1`` 配对的记录仪读数必须排在 ``a2`` 配对的记录仪读数之前；
* 优化目标按字典序依次取舍：
  1. 配对数最多；
  2. 总时间差（各配对时间差绝对值之和）最小；
  3. 仍相同时，配对读数 ID 序列 ``[(直接读数 id, 记录仪读数 id), ...]``
     按字典序取最小，保证结果稳定、可复现；
* 未配对读数不参与温差判定（未配对不算温差异常）。

实现：后缀 DP 求出前两项目标的最优值（时间换算为整数微秒，避免浮点累计误差），
再按第三项目标自左向右贪心重构——每一步在所有“保持最优”的候选下一对中
选择读数 ID 最小的一对。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class SideReading:
    """参与配对的一侧读数；``id`` 为各自表的主键，保证稳定可追溯。"""

    id: int
    event_time: str
    temperature_c: float


@dataclass
class MatchedPair:
    """一对配对读数；时间差 / 温差均为绝对值（未做舍入，由调用方决定展示精度）。"""

    sample_reading_id: int
    logger_reading_id: int
    sample_event_time: str
    logger_event_time: str
    sample_temperature_c: float
    logger_temperature_c: float
    time_diff_seconds: float
    temperature_diff_c: float


def _to_micros(event_time: str) -> int:
    """把 ISO 8601 事件时间换算为 UTC 纪元微秒（无时区按 UTC 处理）。"""

    dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.astimezone(timezone.utc).timestamp() * 1_000_000))


def match_pairs(
    sample_readings: list[SideReading],
    logger_readings: list[SideReading],
    max_time_diff_seconds: float,
) -> list[MatchedPair]:
    """在不交叉一对一约束下求最优配对，返回按事件时间顺序的配对列表。

    ``max_time_diff_seconds`` 为允许配对的最大事件时间差（秒，非负）；
    恰好等于该值的读数对仍允许配对。
    """

    a = sorted(sample_readings, key=lambda r: (r.event_time, r.id))
    b = sorted(logger_readings, key=lambda r: (r.event_time, r.id))
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return []

    limit_us = int(round(max_time_diff_seconds * 1_000_000))
    ta = [_to_micros(r.event_time) for r in a]
    tb = [_to_micros(r.event_time) for r in b]

    # f[i][j] = (配对数, 总时间差微秒的相反数)：后缀 a[i:] 与 b[j:] 的两项目标最优值
    f = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = f[i + 1][j]  # 跳过 a[i]
            if f[i][j + 1] > best:  # 跳过 b[j]
                best = f[i][j + 1]
            diff = abs(ta[i] - tb[j])
            if diff <= limit_us:  # 配对 a[i] 与 b[j]
                c0, c1 = f[i + 1][j + 1]
                cand = (c0 + 1, c1 - diff)
                if cand > best:
                    best = cand
            f[i][j] = best

    # 重构：在保持 (配对数, 总时间差) 最优的所有配对方案中，
    # 逐位选择 (直接读数 id, 记录仪读数 id) 最小的下一对，
    # 使配对读数 ID 序列字典序最小（稳定取舍）。
    pairs: list[MatchedPair] = []
    i = j = 0
    while i < n and j < m:
        value = f[i][j]
        if value[0] == 0:
            break  # 剩余读数无法再配对
        choose: Optional[tuple[int, int]] = None
        choose_key: Optional[tuple[int, int]] = None
        # 候选下一对 (i2, j2)：从 (i, j) 跳过任意前置读数后最优值不变，
        # 且在该处配对仍保持最优
        i2 = i
        while i2 < n and f[i2][j] == value:
            j2 = j
            while j2 < m and f[i2][j2] == value:
                diff = abs(ta[i2] - tb[j2])
                if diff <= limit_us:
                    c0, c1 = f[i2 + 1][j2 + 1]
                    if (c0 + 1, c1 - diff) == value:
                        key = (a[i2].id, b[j2].id)
                        if choose_key is None or key < choose_key:
                            choose_key = key
                            choose = (i2, j2)
                j2 += 1
            i2 += 1
        if choose is None:
            # 理论不可达：配对数 > 0 时必存在保持最优的可行配对
            break
        pi, pj = choose
        ra, rb = a[pi], b[pj]
        pairs.append(
            MatchedPair(
                sample_reading_id=ra.id,
                logger_reading_id=rb.id,
                sample_event_time=ra.event_time,
                logger_event_time=rb.event_time,
                sample_temperature_c=ra.temperature_c,
                logger_temperature_c=rb.temperature_c,
                time_diff_seconds=abs(ta[pi] - tb[pj]) / 1_000_000,
                temperature_diff_c=abs(ra.temperature_c - rb.temperature_c),
            )
        )
        i, j = pi + 1, pj + 1
    return pairs
