"""业务服务层：读数写入、时间重排重算、循环/告警查询与确认。

共享温度记录仪：记录仪读数（``logger_readings``）通过“样本 × 左闭右开时段”
绑定（``logger_bindings``）归入样本，归属关系记录在 ``reading_attributions``。
重算时把直接读数与归属的记录仪读数合并到同一条时间轴上喂给引擎；
绑定时段变更或迟到记录仪读数到达时，先对账归属（删除失效、补建新属），
再整体重算受影响样本，因此不会留下重复派生读数，且直接读数从不被改动。

记录仪校准无效时段（``logger_invalid_periods``）：落在时段内的记录仪读数
照常幂等入库、可经原始读数接口查询，但对账时一律不归属样本，
从而不参与冻融循环、暴露剂量、告警与温度一致性核查；
时段的新增 / 修改 / 删除都会对绑定该记录仪的样本重新对账并回算，
删除时段后有效读数恢复归属。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import engine, matching
from .database import connect

# SQLite 单文件写入需要串行化；批量提交 + 重算整体持锁。
_write_lock = threading.RLock()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """归一化为 UTC ISO 8601（无时区的输入按 UTC 处理），保证字符串排序即时间排序。"""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------- 样本 ----------------


def create_sample(conn: sqlite3.Connection, data: dict) -> sqlite3.Row:
    now = utcnow_iso()
    sample_id = data.get("id") or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO samples (
            id, name, freeze_threshold_c, thaw_threshold_c,
            min_allowed_c, max_allowed_c, max_freeze_thaw_cycles,
            cold_dose_budget_c_min, heat_dose_budget_c_min,
            max_integration_interval_minutes,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sample_id,
            data["name"],
            data["freeze_threshold_c"],
            data["thaw_threshold_c"],
            data.get("min_allowed_c"),
            data.get("max_allowed_c"),
            data.get("max_freeze_thaw_cycles"),
            data.get("cold_dose_budget_c_min"),
            data.get("heat_dose_budget_c_min"),
            data.get("max_integration_interval_minutes"),
            now,
            now,
        ),
    )
    conn.commit()
    row = get_sample(conn, sample_id)
    assert row is not None
    return row


def get_sample(conn: sqlite3.Connection, sample_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()


def list_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM samples ORDER BY created_at, id"))


def update_sample(conn: sqlite3.Connection, sample_id: str, changes: dict) -> Optional[sqlite3.Row]:
    # changes 来自 exclude_unset：只含客户端显式提交的字段，
    # 因此显式传入的 null 也要落库（用于清空允许边界 / 预算等可空配置）。
    if "name" in changes and changes["name"] is None:
        raise ValueError("name 不能为空")
    if changes:
        assignments = ", ".join(f"{k} = ?" for k in changes)
        params = [*changes.values(), utcnow_iso(), sample_id]
        conn.execute(
            f"UPDATE samples SET {assignments}, updated_at = ? WHERE id = ?",
            params,
        )
        conn.commit()
    return get_sample(conn, sample_id)


def delete_sample(conn: sqlite3.Connection, sample_id: str) -> bool:
    cur = conn.execute("DELETE FROM samples WHERE id = ?", (sample_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------- 读数 ----------------


def list_readings(
    conn: sqlite3.Connection, sample_id: str, source: str = "direct"
) -> list[sqlite3.Row]:
    """按事件时间列出样本读数（迟到数据也按 event_time 排列，而非入库顺序）。

    ``source``：``direct`` 仅样本直接读数；``logger`` 仅经绑定归入的记录仪读数；
    ``all`` 合并两者（同时间戳时直接读数排在前）。
    """

    if source == "direct":
        rows = conn.execute(
            "SELECT id, sample_id, event_id, event_time, received_at, temperature_c, "
            "'direct' AS source, NULL AS logger_id, NULL AS logger_reading_id "
            "FROM readings WHERE sample_id = ? ORDER BY event_time, id",
            (sample_id,),
        ).fetchall()
        return list(rows)
    if source == "logger":
        rows = conn.execute(
            """
            SELECT lr.id AS logger_reading_id, lr.logger_id, lr.event_id,
                   lr.event_time, lr.received_at, lr.temperature_c
            FROM reading_attributions ra
            JOIN logger_readings lr ON lr.id = ra.logger_reading_id
            WHERE ra.sample_id = ?
            ORDER BY lr.event_time, lr.id
            """,
            (sample_id,),
        ).fetchall()
        return [
            {
                "id": row["logger_reading_id"],
                "sample_id": sample_id,
                "event_id": row["event_id"],
                "event_time": row["event_time"],
                "received_at": row["received_at"],
                "temperature_c": row["temperature_c"],
                "source": "logger",
                "logger_id": row["logger_id"],
                "logger_reading_id": row["logger_reading_id"],
            }
            for row in rows
        ]

    # all：UNION 两路按统一键排序，合成 id 保证全时间轴唯一且稳定
    rows = conn.execute(
        """
        SELECT id, sample_id, event_id, event_time, received_at, temperature_c,
               source, logger_id, logger_reading_id, sort_tie, engine_id
        FROM (
            SELECT id, sample_id, event_id, event_time, received_at, temperature_c,
                   'direct' AS source, NULL AS logger_id, NULL AS logger_reading_id,
                   0 AS sort_tie, id AS engine_id
            FROM readings WHERE sample_id = ?
            UNION ALL
            SELECT lr.id AS id, ? AS sample_id, lr.event_id, lr.event_time,
                   lr.received_at, lr.temperature_c,
                   'logger' AS source, lr.logger_id AS logger_id,
                   lr.id AS logger_reading_id,
                   1 AS sort_tie, -lr.id AS engine_id
            FROM reading_attributions ra
            JOIN logger_readings lr ON lr.id = ra.logger_reading_id
            WHERE ra.sample_id = ?
        )
        ORDER BY event_time, sort_tie, id
        """,
        (sample_id, sample_id, sample_id),
    ).fetchall()
    return list(rows)


def add_readings(
    conn: sqlite3.Connection, sample_id: str, readings: list[dict]
) -> tuple[int, int, list[sqlite3.Row]]:
    """幂等批量写入读数，随后重算样本派生数据。

    返回 ``(accepted, duplicates, new_alerts)``。
    同一批次内或库中已存在相同 event_id、或相同 (event_time, temperature_c)
    的读数均视为重复并跳过。
    """

    now = utcnow_iso()
    seen_event_ids: set[str] = set()
    seen_signatures: set[tuple[str, float]] = set()
    accepted = 0
    duplicates = 0

    with _write_lock:
        for r in readings:
            event_time_iso = to_utc_iso(r["event_time"])
            event_id = r.get("event_id")
            temp = float(r["temperature_c"])
            is_dup = False

            if event_id is not None:
                if event_id in seen_event_ids:
                    is_dup = True
                else:
                    row = conn.execute(
                        "SELECT 1 FROM readings WHERE sample_id = ? AND event_id = ?",
                        (sample_id, event_id),
                    ).fetchone()
                    if row is not None:
                        is_dup = True
                    else:
                        seen_event_ids.add(event_id)

            signature = (event_time_iso, temp)
            if not is_dup:
                if signature in seen_signatures:
                    is_dup = True
                else:
                    row = conn.execute(
                        "SELECT 1 FROM readings WHERE sample_id = ? AND event_time = ? AND temperature_c = ?",
                        (sample_id, event_time_iso, temp),
                    ).fetchone()
                    if row is not None:
                        is_dup = True
                    else:
                        seen_signatures.add(signature)

            if is_dup:
                duplicates += 1
                continue

            conn.execute(
                """
                INSERT INTO readings (sample_id, event_id, event_time, received_at, temperature_c)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sample_id, event_id, event_time_iso, now, temp),
            )
            accepted += 1

        conn.commit()
        new_alerts = recompute(conn, sample_id, lock_held=True)

    return accepted, duplicates, new_alerts


# ---------------- 重算（核心） ----------------


def _sample_config(sample: sqlite3.Row) -> engine.EngineConfig:
    return engine.EngineConfig(
        freeze_threshold_c=sample["freeze_threshold_c"],
        thaw_threshold_c=sample["thaw_threshold_c"],
        min_allowed_c=sample["min_allowed_c"],
        max_allowed_c=sample["max_allowed_c"],
        max_freeze_thaw_cycles=sample["max_freeze_thaw_cycles"],
        cold_dose_budget_c_min=sample["cold_dose_budget_c_min"],
        heat_dose_budget_c_min=sample["heat_dose_budget_c_min"],
        max_integration_interval_minutes=sample["max_integration_interval_minutes"],
    )


def recompute(
    conn: sqlite3.Connection,
    sample_id: str,
    lock_held: bool = False,
) -> list[sqlite3.Row]:
    """按时间顺序重放全部读数，重建循环与告警，并刷新样本状态缓存。

    告警以稳定 dedup_key 做 upsert，历史告警的确认状态不会丢失；
    本次新产生的告警行会作为列表返回。已不存在的陈旧告警会被清除。
    返回值即“新告警”，供提交接口即时反馈。
    """

    def _do() -> list[sqlite3.Row]:
        sample = get_sample(conn, sample_id)
        if sample is None:
            raise KeyError(sample_id)

        # 合并时间轴：样本直接读数 + 绑定时段归入的记录仪读数。
        # 同时间戳直接读数在前；引擎 id 正值=直接读数 readings.id，
        # 负值=记录仪读数 -logger_readings.id，保证唯一且跨重算稳定。
        merged = list_readings(conn, sample_id, source="all")
        origins: dict[int, sqlite3.Row] = {}
        eng_readings: list[engine.EngineReading] = []
        for row in merged:
            eng_id = row["engine_id"]
            origins[eng_id] = row
            eng_readings.append(
                engine.EngineReading(
                    id=eng_id,
                    event_time=row["event_time"],
                    temperature_c=row["temperature_c"],
                )
            )
        derived = engine.derive(eng_readings, _sample_config(sample))
        now = utcnow_iso()

        # 1) 重建完整循环
        conn.execute("DELETE FROM cycles WHERE sample_id = ?", (sample_id,))
        conn.executemany(
            """
            INSERT INTO cycles (
                sample_id, cycle_index, freeze_event_time, freeze_temperature_c,
                thaw_event_time, thaw_temperature_c, min_temperature_c, max_temperature_c
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    sample_id,
                    c.cycle_index,
                    c.freeze_event_time,
                    c.freeze_temperature_c,
                    c.thaw_event_time,
                    c.thaw_temperature_c,
                    c.min_temperature_c,
                    c.max_temperature_c,
                )
                for c in derived.cycles
            ],
        )

        # 2) 告警 upsert：按 dedup_key 保留已确认标记
        def _refs(a: engine.AlertRecord) -> tuple[Optional[int], Optional[int]]:
            """把引擎读数 id 还原为 (reading_id, logger_reading_id)。"""

            origin = origins.get(a.reading_id)
            if origin is None:
                return None, None
            if origin["source"] == "logger":
                return None, origin["logger_reading_id"]
            return origin["id"], None

        def _key(a: engine.AlertRecord) -> str:
            """带读数来源的稳定去重键。

            temperature_excursion 等“锚定单条读数”的告警按来源加 logger 前缀，
            避免同一时间轴上直接读数与记录仪读数各自触发的告警键冲突；
            cycle_limit（按循环序号）与 exposure_budget（按方向）的去重键
            与读数来源无关，窗口变化导致锚点读数在直接/记录仪间切换时
            已确认状态仍能保留。
            """

            base = engine.alert_dedup_key(sample_id, a)
            if a.alert_type in (
                engine.ALERT_CYCLE_LIMIT,
                engine.ALERT_EXPOSURE_BUDGET,
            ):
                return base
            _, logger_reading_id = _refs(a)
            if logger_reading_id is None:
                return base
            # 形如 {sample}:range:logger:{id} / {sample}:{type}:logger:{id}
            head = base.rsplit(":", 1)[0]
            return f"{head}:logger:{logger_reading_id}"

        current_keys = {_key(a) for a in derived.alerts}
        existing = {
            row["dedup_key"]: row
            for row in conn.execute(
                "SELECT * FROM alerts WHERE sample_id = ?", (sample_id,)
            )
        }
        # 清理：重算后已不再成立的告警
        stale_keys = set(existing) - current_keys
        if stale_keys:
            conn.executemany(
                "DELETE FROM alerts WHERE dedup_key = ?",
                [(k,) for k in stale_keys],
            )

        new_keys: list[str] = []
        for a in derived.alerts:
            key = _key(a)
            reading_id, logger_reading_id = _refs(a)
            if key in existing:
                # 内容可能随配置变化，更新描述但保留 acknowledged
                conn.execute(
                    """
                    UPDATE alerts SET
                        alert_type = ?, severity = ?, event_time = ?,
                        temperature_c = ?, cycle_index = ?, reading_id = ?,
                        logger_reading_id = ?,
                        direction = ?, dose_c_min = ?, budget_c_min = ?, message = ?
                    WHERE dedup_key = ?
                    """,
                    (
                        a.alert_type,
                        a.severity,
                        a.event_time,
                        a.temperature_c,
                        a.cycle_index,
                        reading_id,
                        logger_reading_id,
                        a.direction,
                        a.dose_c_min,
                        a.budget_c_min,
                        a.message,
                        key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO alerts (
                        sample_id, alert_type, severity, event_time, temperature_c,
                        cycle_index, reading_id, logger_reading_id,
                        direction, dose_c_min, budget_c_min,
                        message, acknowledged, dedup_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        sample_id,
                        a.alert_type,
                        a.severity,
                        a.event_time,
                        a.temperature_c,
                        a.cycle_index,
                        reading_id,
                        logger_reading_id,
                        a.direction,
                        a.dose_c_min,
                        a.budget_c_min,
                        a.message,
                        key,
                        now,
                    ),
                )
                new_keys.append(key)

        # 3) 刷新样本当前状态缓存
        conn.execute(
            """
            UPDATE samples SET
                current_state = ?,
                active_freeze_event_time = ?,
                active_freeze_min_c = ?,
                active_freeze_max_c = ?,
                cycle_count = ?,
                cold_dose_c_min = ?,
                heat_dose_c_min = ?,
                exposure_gap_minutes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                derived.current_state,
                derived.active_freeze_event_time,
                derived.active_freeze_min_c,
                derived.active_freeze_max_c,
                derived.cycle_count,
                derived.cold_dose_c_min,
                derived.heat_dose_c_min,
                derived.gap_minutes,
                now,
                sample_id,
            ),
        )
        conn.commit()

        if new_keys:
            placeholders = ",".join("?" for _ in new_keys)
            return list(
                conn.execute(
                    f"SELECT * FROM alerts WHERE dedup_key IN ({placeholders}) ORDER BY event_time, id",
                    new_keys,
                )
            )
        return []

    if lock_held:
        return _do()
    with _write_lock:
        return _do()


# ---------------- 查询 ----------------


def list_cycles(conn: sqlite3.Connection, sample_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM cycles WHERE sample_id = ? ORDER BY cycle_index",
            (sample_id,),
        )
    )


def list_alerts(
    conn: sqlite3.Connection,
    sample_id: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM alerts WHERE 1=1"
    params: list = []
    if sample_id is not None:
        sql += " AND sample_id = ?"
        params.append(sample_id)
    if acknowledged is not None:
        sql += " AND acknowledged = ?"
        params.append(1 if acknowledged else 0)
    sql += " ORDER BY event_time DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return list(conn.execute(sql, params))


def get_alert(conn: sqlite3.Connection, alert_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def acknowledge_alert(
    conn: sqlite3.Connection, alert_id: int, acknowledged: bool
) -> Optional[sqlite3.Row]:
    with _write_lock:
        cur = conn.execute(
            "UPDATE alerts SET acknowledged = ? WHERE id = ?",
            (1 if acknowledged else 0, alert_id),
        )
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_alert(conn, alert_id)


def acknowledge_sample_alerts(
    conn: sqlite3.Connection, sample_id: str, acknowledged: bool = True
) -> int:
    with _write_lock:
        cur = conn.execute(
            "UPDATE alerts SET acknowledged = ? WHERE sample_id = ?",
            (1 if acknowledged else 0, sample_id),
        )
        conn.commit()
        return cur.rowcount


def get_state(conn: sqlite3.Connection, sample_id: str) -> Optional[dict]:
    sample = get_sample(conn, sample_id)
    if sample is None:
        return None
    # 最近读数基于合并时间轴（直接读数 + 归入的记录仪读数）
    last_rows = list_readings(conn, sample_id, source="all")
    last = last_rows[-1] if last_rows else None
    unacked = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE sample_id = ? AND acknowledged = 0",
        (sample_id,),
    ).fetchone()["n"]

    def _usage(dose: float, budget: Optional[float]) -> Optional[float]:
        if budget is None or budget <= 0:
            return None
        return round(dose / budget, 6)

    return {
        "sample_id": sample["id"],
        "name": sample["name"],
        "current_state": sample["current_state"],
        "cycle_count": sample["cycle_count"],
        "active_freeze_event_time": sample["active_freeze_event_time"],
        "active_freeze_min_c": sample["active_freeze_min_c"],
        "active_freeze_max_c": sample["active_freeze_max_c"],
        "cold_dose_c_min": sample["cold_dose_c_min"],
        "heat_dose_c_min": sample["heat_dose_c_min"],
        "exposure_gap_minutes": sample["exposure_gap_minutes"],
        "cold_dose_budget_c_min": sample["cold_dose_budget_c_min"],
        "heat_dose_budget_c_min": sample["heat_dose_budget_c_min"],
        "max_integration_interval_minutes": sample["max_integration_interval_minutes"],
        "cold_budget_usage": _usage(
            sample["cold_dose_c_min"], sample["cold_dose_budget_c_min"]
        ),
        "heat_budget_usage": _usage(
            sample["heat_dose_c_min"], sample["heat_dose_budget_c_min"]
        ),
        "last_event_time": last["event_time"] if last else None,
        "last_temperature_c": last["temperature_c"] if last else None,
        "unacknowledged_alert_count": unacked,
    }


# ---------------- 共享记录仪 ----------------


class ValidationError(Exception):
    """业务校验失败（时段非法、区间重叠等），路由层映射为 422。"""


def _find_overlap(
    conn: sqlite3.Connection,
    sample_id: str,
    start_iso: str,
    end_iso: str,
    exclude_binding_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """返回与左闭右开时段 [start, end) 重叠的同一样本既有绑定（不含自身）。"""

    sql = (
        "SELECT * FROM logger_bindings WHERE sample_id = ? "
        "AND start_time < ? AND end_time > ?"
    )
    params: list = [sample_id, end_iso, start_iso]
    if exclude_binding_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_binding_id)
    return conn.execute(sql, params).fetchone()


# ---- 记录仪登记 ----


def create_logger(conn: sqlite3.Connection, data: dict) -> sqlite3.Row:
    now = utcnow_iso()
    logger_id = data.get("id") or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO loggers (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (logger_id, data["name"], now, now),
    )
    conn.commit()
    row = get_logger(conn, logger_id)
    assert row is not None
    return row


def get_logger(conn: sqlite3.Connection, logger_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM loggers WHERE id = ?", (logger_id,)).fetchone()


def list_loggers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM loggers ORDER BY created_at, id"))


def update_logger(
    conn: sqlite3.Connection, logger_id: str, name: str
) -> Optional[sqlite3.Row]:
    conn.execute(
        "UPDATE loggers SET name = ?, updated_at = ? WHERE id = ?",
        (name, utcnow_iso(), logger_id),
    )
    conn.commit()
    return get_logger(conn, logger_id)


def delete_logger(conn: sqlite3.Connection, logger_id: str) -> bool:
    """删除记录仪：级联删读数/绑定/归属；受影响样本随后撤销旧归属并重算。"""

    with _write_lock:
        # 先取出受影响样本（级联删除后归属行将不存在）
        affected = [
            row["sample_id"]
            for row in conn.execute(
                """
                SELECT DISTINCT b.sample_id
                FROM logger_bindings b
                WHERE b.logger_id = ?
                """,
                (logger_id,),
            )
        ]
        cur = conn.execute("DELETE FROM loggers WHERE id = ?", (logger_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        if deleted:
            for sample_id in affected:
                recompute(conn, sample_id, lock_held=True)
        return deleted


# ---- 记录仪读数 ----


def list_logger_readings(
    conn: sqlite3.Connection, logger_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM logger_readings WHERE logger_id = ? ORDER BY event_time, id",
            (logger_id,),
        )
    )


def add_logger_readings(
    conn: sqlite3.Connection, logger_id: str, readings: list[dict]
) -> tuple[int, int, list[sqlite3.Row]]:
    """幂等批量写入记录仪读数。

    与样本直接读数一致：``event_id`` 唯一；无 event_id 时
    ``(event_time, temperature_c)`` 签名去重（批次内 + 库内）。
    新读数落库后，对所有与其存在绑定时段的样本做归属对账并重算
    （迟到读数由此自动归入正确样本、撤销不再成立的旧归属）。
    返回 ``(accepted, duplicates, new_alerts)``。
    """

    now = utcnow_iso()
    seen_event_ids: set[str] = set()
    seen_signatures: set[tuple[str, float]] = set()
    accepted = 0
    duplicates = 0

    with _write_lock:
        for r in readings:
            event_time_iso = to_utc_iso(r["event_time"])
            event_id = r.get("event_id")
            temp = float(r["temperature_c"])
            is_dup = False

            if event_id is not None:
                if event_id in seen_event_ids:
                    is_dup = True
                else:
                    row = conn.execute(
                        "SELECT 1 FROM logger_readings WHERE logger_id = ? AND event_id = ?",
                        (logger_id, event_id),
                    ).fetchone()
                    if row is not None:
                        is_dup = True
                    else:
                        seen_event_ids.add(event_id)

            signature = (event_time_iso, temp)
            if not is_dup:
                if signature in seen_signatures:
                    is_dup = True
                else:
                    row = conn.execute(
                        "SELECT 1 FROM logger_readings "
                        "WHERE logger_id = ? AND event_time = ? AND temperature_c = ?",
                        (logger_id, event_time_iso, temp),
                    ).fetchone()
                    if row is not None:
                        is_dup = True
                    else:
                        seen_signatures.add(signature)

            if is_dup:
                duplicates += 1
                continue

            conn.execute(
                """
                INSERT INTO logger_readings (logger_id, event_id, event_time, received_at, temperature_c)
                VALUES (?, ?, ?, ?, ?)
                """,
                (logger_id, event_id, event_time_iso, now, temp),
            )
            accepted += 1

        conn.commit()

        new_alerts: list[sqlite3.Row] = []
        if accepted:
            for sample_id in samples_bound_to_logger(conn, logger_id):
                reconcile_sample(conn, sample_id, lock_held=True)
                # recompute 仅返回本次新插入（dedup_key 此前不存在）的告警
                new_alerts.extend(recompute(conn, sample_id, lock_held=True))
            new_alerts.sort(key=lambda a: (a["event_time"], a["id"]))

    return accepted, duplicates, new_alerts


def samples_bound_to_logger(conn: sqlite3.Connection, logger_id: str) -> list[str]:
    return [
        row["sample_id"]
        for row in conn.execute(
            "SELECT DISTINCT sample_id FROM logger_bindings WHERE logger_id = ?",
            (logger_id,),
        )
    ]


# ---- 归属对账 ----


def reconcile_sample(
    conn: sqlite3.Connection, sample_id: str, lock_held: bool = False
) -> None:
    """按当前绑定时段重建样本对记录仪读数的归属（幂等全量对账）。

    左闭右开：``start_time <= event_time < end_time`` 才归入。
    同一样本各绑定时段不重叠，故一条记录仪读数至多归属一次，
    对账删除失效归属、补建新归属，不会产生重复派生读数。
    落在记录仪**校准无效时段**内的读数不归属任何样本
    （原始读数仍在 ``logger_readings`` 中，可经原始读数接口查询）。
    本函数只重建归属表；循环/告警由调用方随后 ``recompute`` 完成。
    """

    def _do() -> None:
        conn.execute(
            """
            DELETE FROM reading_attributions
            WHERE sample_id = ?
              AND logger_reading_id NOT IN (
                  SELECT lr.id
                  FROM logger_readings lr
                  JOIN logger_bindings b ON b.logger_id = lr.logger_id
                  WHERE b.sample_id = ?
                    AND lr.event_time >= b.start_time
                    AND lr.event_time < b.end_time
                    AND NOT EXISTS (
                        SELECT 1 FROM logger_invalid_periods p
                        WHERE p.logger_id = lr.logger_id
                          AND lr.event_time >= p.start_time
                          AND lr.event_time < p.end_time
                    )
              )
            """,
            (sample_id, sample_id),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO reading_attributions (sample_id, logger_reading_id)
            SELECT ?, lr.id
            FROM logger_readings lr
            JOIN logger_bindings b ON b.logger_id = lr.logger_id
            WHERE b.sample_id = ?
              AND lr.event_time >= b.start_time
              AND lr.event_time < b.end_time
              AND NOT EXISTS (
                  SELECT 1 FROM logger_invalid_periods p
                  WHERE p.logger_id = lr.logger_id
                    AND lr.event_time >= p.start_time
                    AND lr.event_time < p.end_time
              )
            """,
            (sample_id, sample_id),
        )
        conn.commit()

    if lock_held:
        _do()
    else:
        with _write_lock:
            _do()


# ---- 绑定时段 ----


def list_bindings_for_sample(
    conn: sqlite3.Connection, sample_id: str
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT b.*, s.name AS sample_name, l.name AS logger_name,
               (SELECT COUNT(*) FROM reading_attributions ra
                 JOIN logger_readings lr ON lr.id = ra.logger_reading_id
                WHERE ra.sample_id = b.sample_id
                  AND lr.logger_id = b.logger_id
                  AND lr.event_time >= b.start_time
                  AND lr.event_time < b.end_time) AS reading_count
        FROM logger_bindings b
        JOIN loggers l ON l.id = b.logger_id
        JOIN samples s ON s.id = b.sample_id
        WHERE b.sample_id = ?
        ORDER BY b.start_time, b.id
        """,
        (sample_id,),
    ).fetchall()
    return list(rows)


def list_bindings_for_logger(
    conn: sqlite3.Connection, logger_id: str
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT b.*, s.name AS sample_name, l.name AS logger_name,
               (SELECT COUNT(*) FROM reading_attributions ra
                 JOIN logger_readings lr ON lr.id = ra.logger_reading_id
                WHERE ra.sample_id = b.sample_id
                  AND lr.logger_id = b.logger_id
                  AND lr.event_time >= b.start_time
                  AND lr.event_time < b.end_time) AS reading_count
        FROM logger_bindings b
        JOIN loggers l ON l.id = b.logger_id
        JOIN samples s ON s.id = b.sample_id
        WHERE b.logger_id = ?
        ORDER BY b.start_time, b.id
        """,
        (logger_id,),
    ).fetchall()
    return list(rows)


def get_binding(
    conn: sqlite3.Connection, binding_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM logger_bindings WHERE id = ?", (binding_id,)
    ).fetchone()


def create_binding(
    conn: sqlite3.Connection,
    sample_id: str,
    logger_id: str,
    start_time: datetime,
    end_time: datetime,
) -> sqlite3.Row:
    """新增样本↔记录仪左闭右开绑定时段并重建该样本；同一样本时段重叠拒绝。"""

    start_iso = to_utc_iso(start_time)
    end_iso = to_utc_iso(end_time)
    if end_iso <= start_iso:
        raise ValidationError("end_time 必须严格晚于 start_time（左闭右开时段）")

    with _write_lock:
        overlap = _find_overlap(conn, sample_id, start_iso, end_iso)
        if overlap is not None:
            raise ValidationError(
                f"绑定时段与现有时段重叠：binding_id={overlap['id']} "
                f"[{overlap['start_time']}, {overlap['end_time']})"
            )
        now = utcnow_iso()
        cur = conn.execute(
            """
            INSERT INTO logger_bindings (sample_id, logger_id, start_time, end_time, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sample_id, logger_id, start_iso, end_iso, now, now),
        )
        binding_id = cur.lastrowid
        conn.commit()
        reconcile_sample(conn, sample_id, lock_held=True)
        recompute(conn, sample_id, lock_held=True)

    row = get_binding(conn, binding_id)
    assert row is not None
    return row


def update_binding(
    conn: sqlite3.Connection,
    binding_id: int,
    changes: dict,
) -> sqlite3.Row:
    """PATCH 绑定时段：校验不重叠，然后撤销旧归属、重建并重算样本。"""

    binding = get_binding(conn, binding_id)
    if binding is None:
        raise KeyError(binding_id)

    # 仅提交单个端点时与另一个现有端点合并后再校验，避免出现倒挂时段
    start_iso = (
        to_utc_iso(changes["start_time"])
        if changes.get("start_time") is not None
        else binding["start_time"]
    )
    end_iso = (
        to_utc_iso(changes["end_time"])
        if changes.get("end_time") is not None
        else binding["end_time"]
    )
    if end_iso <= start_iso:
        raise ValidationError("end_time 必须严格晚于 start_time（左闭右开时段）")

    with _write_lock:
        overlap = _find_overlap(
            conn, binding["sample_id"], start_iso, end_iso, exclude_binding_id=binding_id
        )
        if overlap is not None:
            raise ValidationError(
                f"绑定时段与现有时段重叠：binding_id={overlap['id']} "
                f"[{overlap['start_time']}, {overlap['end_time']})"
            )
        conn.execute(
            "UPDATE logger_bindings SET start_time = ?, end_time = ?, updated_at = ? WHERE id = ?",
            (start_iso, end_iso, utcnow_iso(), binding_id),
        )
        conn.commit()
        sample_id = binding["sample_id"]
        reconcile_sample(conn, sample_id, lock_held=True)
        recompute(conn, sample_id, lock_held=True)

    row = get_binding(conn, binding_id)
    assert row is not None
    return row


def delete_binding(conn: sqlite3.Connection, binding_id: int) -> bool:
    binding = get_binding(conn, binding_id)
    if binding is None:
        return False
    sample_id = binding["sample_id"]
    with _write_lock:
        conn.execute("DELETE FROM logger_bindings WHERE id = ?", (binding_id,))
        conn.commit()
        reconcile_sample(conn, sample_id, lock_held=True)
        recompute(conn, sample_id, lock_held=True)
    return True


# ---- 校准无效时段 ----


def _find_invalid_period_overlap(
    conn: sqlite3.Connection,
    logger_id: str,
    start_iso: str,
    end_iso: str,
    exclude_period_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """返回与左闭右开时段 [start, end) 重叠的同一记录仪既有无效时段（不含自身）。"""

    sql = (
        "SELECT * FROM logger_invalid_periods WHERE logger_id = ? "
        "AND start_time < ? AND end_time > ?"
    )
    params: list = [logger_id, end_iso, start_iso]
    if exclude_period_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_period_id)
    return conn.execute(sql, params).fetchone()


def _rebuild_samples_bound_to_logger(conn: sqlite3.Connection, logger_id: str) -> None:
    """对账并重算绑定到该记录仪的全部样本（校准无效时段变更后调用）。

    调用方须已持有 ``_write_lock``。
    """

    for sample_id in samples_bound_to_logger(conn, logger_id):
        reconcile_sample(conn, sample_id, lock_held=True)
        recompute(conn, sample_id, lock_held=True)


def list_invalid_periods(
    conn: sqlite3.Connection, logger_id: str
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT p.*, l.name AS logger_name,
               (SELECT COUNT(*) FROM logger_readings lr
                 WHERE lr.logger_id = p.logger_id
                   AND lr.event_time >= p.start_time
                   AND lr.event_time < p.end_time) AS reading_count
        FROM logger_invalid_periods p
        JOIN loggers l ON l.id = p.logger_id
        WHERE p.logger_id = ?
        ORDER BY p.start_time, p.id
        """,
        (logger_id,),
    ).fetchall()
    return list(rows)


def get_invalid_period(
    conn: sqlite3.Connection, period_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM logger_invalid_periods WHERE id = ?", (period_id,)
    ).fetchone()


def create_invalid_period(
    conn: sqlite3.Connection,
    logger_id: str,
    start_time: datetime,
    end_time: datetime,
) -> sqlite3.Row:
    """新增记录仪校准无效时段；同一记录仪时段重叠拒绝。

    落库后对所有绑定该记录仪的样本做归属对账并重算：
    时段内的记录仪读数不再归属样本（原始读数保留）。
    """

    start_iso = to_utc_iso(start_time)
    end_iso = to_utc_iso(end_time)
    if end_iso <= start_iso:
        raise ValidationError("end_time 必须严格晚于 start_time（左闭右开时段）")

    with _write_lock:
        overlap = _find_invalid_period_overlap(conn, logger_id, start_iso, end_iso)
        if overlap is not None:
            raise ValidationError(
                f"校准无效时段与现有时段重叠：period_id={overlap['id']} "
                f"[{overlap['start_time']}, {overlap['end_time']})"
            )
        now = utcnow_iso()
        cur = conn.execute(
            """
            INSERT INTO logger_invalid_periods (logger_id, start_time, end_time, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (logger_id, start_iso, end_iso, now, now),
        )
        period_id = cur.lastrowid
        conn.commit()
        _rebuild_samples_bound_to_logger(conn, logger_id)

    row = get_invalid_period(conn, period_id)
    assert row is not None
    return row


def update_invalid_period(
    conn: sqlite3.Connection,
    period_id: int,
    changes: dict,
) -> sqlite3.Row:
    """PATCH 校准无效时段：校验不重叠，然后对账并重算受影响样本。"""

    period = get_invalid_period(conn, period_id)
    if period is None:
        raise KeyError(period_id)

    # 仅提交单个端点时与另一个现有端点合并后再校验，避免出现倒挂时段
    start_iso = (
        to_utc_iso(changes["start_time"])
        if changes.get("start_time") is not None
        else period["start_time"]
    )
    end_iso = (
        to_utc_iso(changes["end_time"])
        if changes.get("end_time") is not None
        else period["end_time"]
    )
    if end_iso <= start_iso:
        raise ValidationError("end_time 必须严格晚于 start_time（左闭右开时段）")

    with _write_lock:
        overlap = _find_invalid_period_overlap(
            conn, period["logger_id"], start_iso, end_iso, exclude_period_id=period_id
        )
        if overlap is not None:
            raise ValidationError(
                f"校准无效时段与现有时段重叠：period_id={overlap['id']} "
                f"[{overlap['start_time']}, {overlap['end_time']})"
            )
        conn.execute(
            "UPDATE logger_invalid_periods SET start_time = ?, end_time = ?, updated_at = ? WHERE id = ?",
            (start_iso, end_iso, utcnow_iso(), period_id),
        )
        conn.commit()
        _rebuild_samples_bound_to_logger(conn, period["logger_id"])

    row = get_invalid_period(conn, period_id)
    assert row is not None
    return row


def delete_invalid_period(conn: sqlite3.Connection, period_id: int) -> bool:
    """删除校准无效时段：原时段内的有效读数恢复归属，受影响样本重算。"""

    period = get_invalid_period(conn, period_id)
    if period is None:
        return False
    with _write_lock:
        conn.execute("DELETE FROM logger_invalid_periods WHERE id = ?", (period_id,))
        conn.commit()
        _rebuild_samples_bound_to_logger(conn, period["logger_id"])
    return True


# ---------------- 样本 × 记录仪温度一致性核查 ----------------

# 温差严格大于阈值才判异常；浮点临界（表示误差在 1e-9 内）按不超限处理
_TEMP_DIFF_EPS = 1e-9


def logger_consistency_check(
    conn: sqlite3.Connection,
    sample_id: str,
    logger_id: str,
    max_time_diff_seconds: float,
    temp_diff_threshold_c: float,
) -> dict:
    """样本直接读数与当前有效归属的该记录仪读数做最优一一配对，筛出温差超限配对。

    只读查询：归属表随迟到读数 / 绑定变更实时对账，本函数直接读取最新归属，
    不改动任何读数、归属、循环、剂量或告警数据。
    """

    direct = list(
        conn.execute(
            "SELECT id, event_time, temperature_c FROM readings "
            "WHERE sample_id = ? ORDER BY event_time, id",
            (sample_id,),
        )
    )
    attributed = list(
        conn.execute(
            """
            SELECT lr.id, lr.event_time, lr.temperature_c
            FROM reading_attributions ra
            JOIN logger_readings lr ON lr.id = ra.logger_reading_id
            WHERE ra.sample_id = ? AND lr.logger_id = ?
            ORDER BY lr.event_time, lr.id
            """,
            (sample_id, logger_id),
        )
    )
    pairs = matching.match_pairs(
        [matching.SideReading(**row) for row in direct],
        [matching.SideReading(**row) for row in attributed],
        max_time_diff_seconds,
    )
    anomalies = [
        p
        for p in pairs
        if p.temperature_diff_c > temp_diff_threshold_c + _TEMP_DIFF_EPS
    ]
    return {
        "sample_id": sample_id,
        "logger_id": logger_id,
        "max_time_diff_seconds": max_time_diff_seconds,
        "temp_diff_threshold_c": temp_diff_threshold_c,
        "paired_count": len(pairs),
        "unmatched_sample_count": len(direct) - len(pairs),
        "unmatched_logger_count": len(attributed) - len(pairs),
        "anomaly_count": len(anomalies),
        "anomalies": [
            {
                "reading_id": p.sample_reading_id,
                "logger_reading_id": p.logger_reading_id,
                "sample_event_time": p.sample_event_time,
                "logger_event_time": p.logger_event_time,
                "sample_temperature_c": p.sample_temperature_c,
                "logger_temperature_c": p.logger_temperature_c,
                "time_diff_seconds": round(p.time_diff_seconds, 6),
                "temperature_diff_c": round(p.temperature_diff_c, 6),
            }
            for p in anomalies
        ],
    }


def get_db_connection() -> sqlite3.Connection:
    """FastAPI 依赖：每请求一个连接。"""

    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
