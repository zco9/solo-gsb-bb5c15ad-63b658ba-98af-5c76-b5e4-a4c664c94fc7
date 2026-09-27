"""API 集成测试：通过临时 SQLite 文件覆盖 DB_PATH。"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    import app.database as database
    import app.services as services
    import app.main as main

    importlib.reload(database)
    importlib.reload(services)
    importlib.reload(main)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def _create_sample(client, sid="s1", **kw):
    payload = {
        "id": sid,
        "name": "测试样本",
        "freeze_threshold_c": -20,
        "thaw_threshold_c": 0,
        "min_allowed_c": -80,
        "max_allowed_c": 8,
        "max_freeze_thaw_cycles": 2,
    }
    payload.update(kw)
    r = client.post("/samples", json=payload)
    assert r.status_code == 201, r.text
    return r


def _submit(client, sid, readings):
    return client.post(f"/samples/{sid}/readings", json={"readings": readings})


def test_health_and_startup_init(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # 建库后应能直接创建样本
    assert _create_sample(client).status_code == 201


def test_invalid_thresholds_rejected(client) -> None:
    r = client.post(
        "/samples",
        json={
            "id": "bad",
            "name": "x",
            "freeze_threshold_c": 5,
            "thaw_threshold_c": -5,
        },
    )
    assert r.status_code == 422


def test_full_cycle_state_and_cycles(client) -> None:
    _create_sample(client)
    r = _submit(
        client,
        "s1",
        [
            {"event_id": "a", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4},
            {"event_id": "b", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22},
            {"event_id": "c", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 2},
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 3
    assert body["current_state"] == "thawed"
    assert body["cycle_count"] == 1

    state = client.get("/samples/s1/state").json()
    assert state["current_state"] == "thawed"
    assert state["cycle_count"] == 1
    assert state["last_temperature_c"] == 2.0

    cycles = client.get("/samples/s1/cycles").json()
    assert len(cycles) == 1
    assert cycles[0]["min_temperature_c"] == -22.0
    assert cycles[0]["max_temperature_c"] == 2.0


def test_late_readings_reordered(client) -> None:
    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4},
            {"event_id": "e2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22},
            # 先到一段解冻
            {"event_id": "e5", "event_time": "2026-09-20T12:00:00Z", "temperature_c": 3},
        ],
    )
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 迟到：09:00 先解冻（闭环第 1 次循环）、09:30 再冻、10:00 再解冻
    # —— 全部插入 08:30 与 12:00 之间，重排后得到 2 次完整循环
    r = _submit(
        client,
        "s1",
        [
            {"event_id": "late1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 3},
            {"event_id": "late2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": -25},
            {"event_id": "late3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 1},
        ],
    )
    assert r.json()["cycle_count"] == 2

    # 读数查询严格按事件时间排序
    times = [r["event_time"] for r in client.get("/samples/s1/readings").json()]
    assert times == sorted(times)
    assert times[0].startswith("2026-09-20T08:00:00")


def test_idempotent_event_id_and_signature(client) -> None:
    _create_sample(client)
    payload = [
        {"event_id": "x1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4},
    ]
    assert _submit(client, "s1", payload).json()["accepted"] == 1
    again = _submit(client, "s1", payload).json()
    assert again["accepted"] == 0
    assert again["duplicates"] == 1

    # 无 event_id，同时刻同温度也判重
    sig = [{"event_time": "2026-09-20T09:00:00Z", "temperature_c": -21}]
    assert _submit(client, "s1", sig).json()["accepted"] == 1
    assert _submit(client, "s1", sig).json()["duplicates"] == 1


def test_excursion_and_cycle_limit_alerts(client) -> None:
    _create_sample(client)
    # 3 次完整循环 + 一次越限
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -22},
            {"event_id": "2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
            {"event_id": "3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -22},
            {"event_id": "4", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2},
            {"event_id": "5", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -85.5},
            {"event_id": "6", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2},
        ],
    )
    alerts = client.get("/alerts?sample_id=s1").json()
    types = sorted(a["alert_type"] for a in alerts)
    assert types == ["cycle_limit_exceeded", "temperature_excursion"]

    excursion = next(a for a in alerts if a["alert_type"] == "temperature_excursion")
    assert excursion["temperature_c"] == -85.5

    state = client.get("/samples/s1/state").json()
    assert state["unacknowledged_alert_count"] == 2

    # 确认告警
    r = client.post(f"/alerts/{excursion['id']}/ack", json={"acknowledged": True})
    assert r.json()["acknowledged"] is True
    unacked = client.get("/alerts?acknowledged=false").json()
    assert all(a["id"] != excursion["id"] for a in unacked)

    # 样本级批量确认
    n = client.post("/samples/s1/alerts/ack", json={"acknowledged": True}).json()["updated"]
    assert n == 2
    assert client.get("/samples/s1/state").json()["unacknowledged_alert_count"] == 0


def test_alert_dedup_survives_recompute(client) -> None:
    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
        ],
    )
    alert_id = client.get("/alerts?sample_id=s1").json()[0]["id"]
    client.post(f"/alerts/{alert_id}/ack", json={"acknowledged": True})

    # 新读数触发重算，告警 id 与确认状态应保留
    _submit(
        client,
        "s1",
        [{"event_id": "3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 3}],
    )
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 1
    assert alerts[0]["id"] == alert_id
    assert alerts[0]["acknowledged"] is True


def test_patch_thresholds_triggers_recompute_and_keeps_ack(client) -> None:
    # 默认 -20/0：-5°C 处于滞回区，不构成循环
    _create_sample(client, max_freeze_thaw_cycles=None)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -25},
            {"event_id": "2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -5},
            {"event_id": "3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 4},
        ],
    )
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 收紧解冻阈值到 -10：-5°C 即解冻，循环不变但段极值描述重算
    r = client.patch("/samples/s1", json={"thaw_threshold_c": -10})
    assert r.status_code == 200
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 收紧冻结阈值到 -30：-25°C 不再触发冻结，循环应消失
    r = client.patch("/samples/s1", json={"freeze_threshold_c": -30})
    assert r.status_code == 200
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0

    # 非法阈值组合 422（-30 >= -10 不成立，改成解冻阈值低于冻结阈值）
    r = client.patch("/samples/s1", json={"thaw_threshold_c": -40})
    assert r.status_code == 422


def test_sample_not_found(client) -> None:
    assert client.get("/samples/nope/state").status_code == 404
    assert client.get("/alerts/999").status_code == 404


# ---------------- 累计温度暴露剂量 ----------------


def test_exposure_dose_state_and_budget_alert(client) -> None:
    _create_sample(
        client,
        min_allowed_c=-80,
        max_allowed_c=8,
        cold_dose_budget_c_min=100,
        heat_dose_budget_c_min=1000,
        max_integration_interval_minutes=120,
    )
    # -85°C 恒温 60 分钟：低温剂量 5 × 60 = 300；预算 100 在 20 分钟处跨越
    r = _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85},
        ],
    )
    assert r.status_code == 200
    new_types = [a["alert_type"] for a in r.json()["new_alerts"]]
    assert "exposure_budget_exceeded" in new_types

    state = client.get("/samples/s1/state").json()
    assert state["cold_dose_c_min"] == 300.0
    assert state["heat_dose_c_min"] == 0.0
    assert state["exposure_gap_minutes"] == 0.0
    assert state["cold_dose_budget_c_min"] == 100.0
    assert state["cold_budget_usage"] == 3.0
    assert state["heat_budget_usage"] == 0.0

    alert = next(
        a
        for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "exposure_budget_exceeded"
    )
    assert alert["direction"] == "cold"
    assert alert["severity"] == "critical"
    assert alert["event_time"].startswith("2026-09-20T08:20:00")
    assert alert["temperature_c"] == -85.0
    assert alert["budget_c_min"] == 100.0

    sample = client.get("/samples/s1").json()
    assert sample["cold_dose_budget_c_min"] == 100.0
    assert sample["cold_dose_c_min"] == 300.0


def test_exposure_gap_skips_long_intervals(client) -> None:
    _create_sample(
        client,
        max_allowed_c=8,
        max_integration_interval_minutes=60,
    )
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 20},
            # 间隔 61 分钟：跳过并计缺口
            {"event_id": "2", "event_time": "2026-09-20T09:01:00Z", "temperature_c": 20},
            # 间隔 30 分钟：正常积分（幅度 12 × 30 = 360）
            {"event_id": "3", "event_time": "2026-09-20T09:31:00Z", "temperature_c": 20},
        ],
    )
    state = client.get("/samples/s1/state").json()
    assert state["exposure_gap_minutes"] == 61.0
    assert state["heat_dose_c_min"] == 360.0


def test_exposure_budget_alert_is_deduped_and_reversible(client) -> None:
    _create_sample(client, min_allowed_c=-80, cold_dose_budget_c_min=100)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85},
        ],
    )
    alerts_before = client.get("/alerts?sample_id=s1").json()
    budget_alerts = [a for a in alerts_before if a["alert_type"] == "exposure_budget_exceeded"]
    assert len(budget_alerts) == 1
    alert_id = budget_alerts[0]["id"]
    client.post(f"/alerts/{alert_id}/ack", json={"acknowledged": True})

    # 继续追加读数触发重算：告警仍是同一条（稳定去重），确认状态保留
    _submit(
        client,
        "s1",
        [
            {"event_id": "3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -85},
        ],
    )
    alerts_after = client.get("/alerts?sample_id=s1").json()
    budget_after = [a for a in alerts_after if a["alert_type"] == "exposure_budget_exceeded"]
    assert len(budget_after) == 1
    assert budget_after[0]["id"] == alert_id
    assert budget_after[0]["acknowledged"] is True

    # 预算调得足够大：重算后预算告警应被清除（可逆）
    r = client.patch("/samples/s1", json={"cold_dose_budget_c_min": 1_000_000})
    assert r.status_code == 200
    assert not [
        a
        for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "exposure_budget_exceeded"
    ]
    assert client.get("/samples/s1/state").json()["cold_budget_usage"] < 1

    # 再调回小预算：告警重新出现
    client.patch("/samples/s1", json={"cold_dose_budget_c_min": 100})
    assert [
        a
        for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "exposure_budget_exceeded"
    ]


def test_late_readings_recompute_exposure_dose(client) -> None:
    _create_sample(client, max_allowed_c=8, heat_dose_budget_c_min=1_000)
    _submit(
        client,
        "s1",
        [
            {"event_id": "1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 5},
            {"event_id": "3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 5},
        ],
    )
    before = client.get("/samples/s1/state").json()["heat_dose_c_min"]

    # 迟到一个 09:00 的高温点，插值剂量随之重算
    _submit(
        client,
        "s1",
        [
            {"event_id": "late", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 20},
        ],
    )
    after = client.get("/samples/s1/state").json()
    # 两段对称三角：5 -> 20 -> 5，每段在 f=0.2 处越过 8，
    # 正值段 48 分钟、幅度 0 -> 12：0.5 × 12 × 48 = 288，两段合计 576
    assert after["heat_dose_c_min"] == 576.0
    assert before != after["heat_dose_c_min"]


def test_dose_config_validation(client) -> None:
    # 低温预算但没有下限 -> 422
    r = client.post(
        "/samples",
        json={
            "id": "bad1",
            "name": "x",
            "freeze_threshold_c": -20,
            "thaw_threshold_c": 0,
            "cold_dose_budget_c_min": 100,
        },
    )
    assert r.status_code == 422

    # 预算非正 -> 422
    r = client.post(
        "/samples",
        json={
            "id": "bad2",
            "name": "x",
            "freeze_threshold_c": -20,
            "thaw_threshold_c": 0,
            "max_allowed_c": 8,
            "heat_dose_budget_c_min": 0,
        },
    )
    assert r.status_code == 422

    # PATCH 合并现有配置后仍缺边界 -> 422
    _create_sample(client, sid="s2", min_allowed_c=None, max_allowed_c=None)
    r = client.patch("/samples/s2", json={"cold_dose_budget_c_min": 100})
    assert r.status_code == 422

    # 显式 null 清空：保留冷预算时清空下限 -> 422；先清预算再清下限 -> 200
    _create_sample(client, sid="s3", cold_dose_budget_c_min=100)
    r = client.patch("/samples/s3", json={"min_allowed_c": None})
    assert r.status_code == 422
    assert client.patch("/samples/s3", json={"cold_dose_budget_c_min": None}).status_code == 200
    assert client.patch("/samples/s3", json={"min_allowed_c": None}).status_code == 200
    # 未提交 name 时的普通字段更新不受影响；显式 name=null -> 422
    assert client.patch("/samples/s3", json={"thaw_threshold_c": -5}).status_code == 200
    assert client.patch("/samples/s3", json={"name": None}).status_code == 422


def test_database_migration_from_legacy_schema(tmp_path, monkeypatch) -> None:
    """旧版本数据库（无暴露剂量列）启动时应通过 ALTER TABLE 平滑升级。"""

    import sqlite3

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE samples (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            freeze_threshold_c REAL NOT NULL, thaw_threshold_c REAL NOT NULL,
            min_allowed_c REAL, max_allowed_c REAL, max_freeze_thaw_cycles INTEGER,
            current_state TEXT NOT NULL DEFAULT 'thawed',
            active_freeze_event_time TEXT, active_freeze_min_c REAL,
            active_freeze_max_c REAL, cycle_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL,
            event_id TEXT, event_time TEXT NOT NULL, received_at TEXT NOT NULL,
            temperature_c REAL NOT NULL,
            UNIQUE(sample_id, event_id),
            UNIQUE(sample_id, event_time, temperature_c)
        );
        CREATE TABLE cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL,
            cycle_index INTEGER NOT NULL, freeze_event_time TEXT NOT NULL,
            freeze_temperature_c REAL NOT NULL, thaw_event_time TEXT NOT NULL,
            thaw_temperature_c REAL NOT NULL, min_temperature_c REAL NOT NULL,
            max_temperature_c REAL NOT NULL, UNIQUE(sample_id, cycle_index)
        );
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL,
            alert_type TEXT NOT NULL, severity TEXT NOT NULL, event_time TEXT NOT NULL,
            temperature_c REAL, cycle_index INTEGER, reading_id INTEGER,
            message TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
            dedup_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        INSERT INTO samples VALUES (
            'legacy-1', '旧库样本', -20, 0, -80, 8, 2,
            'thawed', NULL, NULL, NULL, 0, '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(legacy))

    import app.database as database
    import app.services as services
    import app.main as main

    importlib.reload(database)
    importlib.reload(services)
    importlib.reload(main)
    database.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        # 旧样本仍可查询，新列有默认值
        state = c.get("/samples/legacy-1/state").json()
        assert state["cold_dose_c_min"] == 0.0
        assert state["heat_dose_c_min"] == 0.0
        assert state["exposure_gap_minutes"] == 0.0
        assert state["cold_budget_usage"] is None

        # 旧样本可 PATCH 预算并正常重算
        r = c.patch("/samples/legacy-1", json={"cold_dose_budget_c_min": 100})
        assert r.status_code == 200
        r = c.post(
            "/samples/legacy-1/readings",
            json={
                "readings": [
                    {"event_id": "x1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
                    {"event_id": "x2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85},
                ]
            },
        )
        assert r.status_code == 200
        state = c.get("/samples/legacy-1/state").json()
        assert state["cold_dose_c_min"] == 300.0
        assert any(
            a["alert_type"] == "exposure_budget_exceeded"
            for a in c.get("/alerts?sample_id=legacy-1").json()
        )


# ---------------- 共享温度记录仪 ----------------


def _register_logger(client, lid="lg1", name="记录仪 A"):
    r = client.post("/loggers", json={"id": lid, "name": name})
    assert r.status_code == 201, r.text
    return r


def _bind(client, sample_id, logger_id, start, end, expected=201):
    return client.post(
        f"/samples/{sample_id}/logger-bindings",
        params={"logger_id": logger_id},
        json={"start_time": start, "end_time": end},
    )


def _logger_submit(client, lid, readings):
    return client.post(f"/loggers/{lid}/readings", json={"readings": readings})


def _freeze_thaw():
    return [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "g2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0},
        {"event_id": "g3", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2.0},
    ]


def test_logger_registration_crud_and_404(client) -> None:
    _register_logger(client)
    assert client.get("/loggers").json()[0]["id"] == "lg1"
    assert client.get("/loggers/lg1").json()["name"] == "记录仪 A"
    assert client.patch("/loggers/lg1", json={"name": "改名"}).json()["name"] == "改名"
    # 重复 ID
    assert client.post("/loggers", json={"id": "lg1", "name": "x"}).status_code == 409
    # 不存在
    assert client.get("/loggers/nope").status_code == 404
    assert client.patch("/loggers/nope", json={"name": "x"}).status_code == 404
    assert client.get("/loggers/nope/readings").status_code == 404
    r = _logger_submit(client, "nope", [
        {"event_time": "2026-09-20T08:00:00Z", "temperature_c": 1},
    ])
    assert r.status_code == 404


def test_logger_readings_idempotent(client) -> None:
    _register_logger(client)
    r = _logger_submit(client, "lg1", _freeze_thaw())
    assert r.status_code == 200
    assert r.json()["accepted"] == 3
    assert r.json()["duplicates"] == 0

    # event_id 重复
    r = _logger_submit(client, "lg1", [_freeze_thaw()[0]])
    assert r.json() == {**r.json(), "accepted": 0, "duplicates": 1}

    # 无 event_id 同时刻同温度判重
    sig = [{"event_time": "2026-09-20T10:00:00Z", "temperature_c": -21}]
    assert _logger_submit(client, "lg1", sig).json()["accepted"] == 1
    assert _logger_submit(client, "lg1", sig).json()["duplicates"] == 1

    # 不同记录仪各自独立命名空间
    _register_logger(client, "lg2")
    assert _logger_submit(client, "lg2", [_freeze_thaw()[0]]).json()["accepted"] == 1
    rows = client.get("/loggers/lg1/readings").json()
    assert [r["event_time"] for r in rows] == sorted(r["event_time"] for r in rows)
    assert rows[0]["logger_id"] == "lg1"


def test_binding_half_open_window_and_overlap(client) -> None:
    _create_sample(client)
    _register_logger(client)

    # 非法时段
    r = _bind(client, "s1", "lg1", "2026-09-20T10:00:00Z", "2026-09-20T10:00:00Z")
    assert r.status_code == 422
    r = _bind(client, "s1", "lg1", "2026-09-20T11:00:00Z", "2026-09-20T10:00:00Z")
    assert r.status_code == 422

    assert _bind(
        client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z"
    ).status_code == 201

    # 各种重叠
    for start, end in [
        ("2026-09-20T09:00:00Z", "2026-09-20T11:00:00Z"),  # 跨越
        ("2026-09-20T07:00:00Z", "2026-09-20T08:30:00Z"),  # 头交
        ("2026-09-20T09:30:00Z", "2026-09-20T10:30:00Z"),  # 尾交
        ("2026-09-20T08:30:00Z", "2026-09-20T09:30:00Z"),  # 包含
    ]:
        assert _bind(client, "s1", "lg1", start, end).status_code == 422

    # 仅端点相接（左闭右开）不重叠
    assert _bind(
        client, "s1", "lg1", "2026-09-20T10:00:00Z", "2026-09-20T12:00:00Z"
    ).status_code == 201
    # [07:00,08:00) 只在起点相接
    assert _bind(
        client, "s1", "lg1", "2026-09-20T07:00:00Z", "2026-09-20T08:00:00Z"
    ).status_code == 201

    # 绑定到不存在的样本/记录仪
    assert _bind(
        client, "nope", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T09:00:00Z"
    ).status_code == 404
    assert _bind(
        client, "s1", "nope", "2026-09-20T08:00:00Z", "2026-09-20T09:00:00Z"
    ).status_code == 404


def test_overlap_rejected_even_across_different_loggers(client) -> None:
    """同一样本的绑定时段不得重叠——即使来自不同记录仪（保证归属/积分不重复）。"""

    _create_sample(client)
    _register_logger(client, "lg1")
    _register_logger(client, "lg2")
    assert _bind(
        client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z"
    ).status_code == 201
    # 不同记录仪、时段重叠 -> 仍拒绝
    assert _bind(
        client, "s1", "lg2", "2026-09-20T09:00:00Z", "2026-09-20T11:00:00Z"
    ).status_code == 422
    # 端点相接允许（即便也是不同记录仪）
    assert _bind(
        client, "s1", "lg2", "2026-09-20T10:00:00Z", "2026-09-20T12:00:00Z"
    ).status_code == 201
    # 但不同样本可以同时绑定同一记录仪的同一时段（一读多样本）
    _create_sample(client, sid="s2")
    assert _bind(
        client, "s2", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z"
    ).status_code == 201


def test_logger_readings_attributed_to_samples_and_shared(client) -> None:
    """一条记录仪读数可归入多个样本；只归入发生时间落在窗口内的样本。"""

    _create_sample(client, sid="s1")
    _create_sample(client, sid="s2")
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z")
    _bind(client, "s2", "lg1", "2026-09-20T09:00:00Z", "2026-09-20T12:00:00Z")

    # 先提交读数、后建绑定的顺序在本用例相反：先绑定后读数
    r = _logger_submit(client, "lg1", [
        {"event_id": "g0", "event_time": "2026-09-20T07:30:00Z", "temperature_c": 4.0},   # 窗口外
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},   # s1 起点含
        {"event_id": "g2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0}, # s1
        {"event_id": "g3", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2.0},   # s1+s2 共享
        {"event_id": "g4", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -25.0}, # s1 终点不含；s2 含
        {"event_id": "g5", "event_time": "2026-09-20T11:30:00Z", "temperature_c": 2.0},   # s2
        {"event_id": "g6", "event_time": "2026-09-20T12:00:00Z", "temperature_c": 2.0},   # s2 终点不含
    ])
    assert r.status_code == 200
    assert set(r.json()["affected_samples"]) == {"s1", "s2"}

    s1_logger = client.get("/samples/s1/readings?source=logger").json()
    s2_logger = client.get("/samples/s2/readings?source=logger").json()
    assert [x["event_id"] for x in s1_logger] == ["g1", "g2", "g3"]
    assert [x["event_id"] for x in s2_logger] == ["g3", "g4", "g5"]

    # s1：08:00 暖 → 08:30 冻 → 09:30 解冻 = 1 次循环
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1
    # s2：09:30 已是解冻态 → 10:00 冻 → 11:30 解冻 = 1 次循环
    assert client.get("/samples/s2/state").json()["cycle_count"] == 1

    # 默认 source=all；direct 为空时全部是 logger
    merged = client.get("/samples/s1/readings").json()
    assert [x["event_id"] for x in merged] == ["g1", "g2", "g3"]
    assert {x["source"] for x in merged} == {"logger"}

    # 绑定查询两侧
    for_s1 = client.get("/samples/s1/logger-bindings").json()
    assert len(for_s1) == 1 and for_s1[0]["reading_count"] == 3
    assert for_s1[0]["logger_name"] == "记录仪 A"
    for_lg = client.get("/loggers/lg1/bindings").json()
    assert {b["sample_id"] for b in for_lg} == {"s1", "s2"}


def test_logger_and_direct_readings_merge_on_one_timeline(client) -> None:
    """记录仪读数与直接读数合并后按既有规则重算冻融与剂量。"""

    _create_sample(client, min_allowed_c=-80, max_allowed_c=8,
                   heat_dose_budget_c_min=1_000)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")

    # 直接读数：08:00 暖、09:00 冻
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "d2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -25.0},
    ]})
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0

    # 记录仪读数：10:00 解冻 -> 闭环一次循环
    r = _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 2.0},
        {"event_id": "g2", "event_time": "2026-09-20T11:00:00Z", "temperature_c": 20.0},
    ])
    assert r.status_code == 200
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    merged = client.get("/samples/s1/readings").json()
    assert [(x["event_time"][11:16], x["source"]) for x in merged] == [
        ("08:00", "direct"),
        ("09:00", "direct"),
        ("10:00", "logger"),
        ("11:00", "logger"),
    ]
    # 直接读数永不改动
    direct = client.get("/samples/s1/readings?source=direct").json()
    assert [x["event_id"] for x in direct] == ["d1", "d2"]
    # 记录仪的 20°C 参与剂量积分（10:00 的 2°C -> 11:00 的 20°C，越过 8°C）
    state = client.get("/samples/s1/state").json()
    assert state["heat_dose_c_min"] > 0


def test_late_logger_reading_rebuilds_attribution(client) -> None:
    """迟到记录仪读数撤销旧归属结论并重建，不留下重复派生读数。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")

    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "g2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0},
        {"event_id": "g3", "event_time": "2026-09-20T12:30:00Z", "temperature_c": 2.0},
    ])
    # g3 在窗口外：只有冻结、没有解冻，0 次循环
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0

    # 迟到 10:00 解冻（窗口内）-> 1 次循环
    r = _logger_submit(client, "lg1", [
        {"event_id": "late", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 2.0},
    ])
    assert r.json()["accepted"] == 1
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    rows = client.get("/samples/s1/readings?source=logger").json()
    ids = [x["logger_reading_id"] for x in rows]
    assert len(ids) == len(set(ids))  # 无重复派生读数
    assert [x["event_id"] for x in rows] == ["g1", "g2", "late"]


def test_binding_change_revokes_and_rebuilds(client) -> None:
    """PATCH/DELETE 绑定时段后旧归属撤销、受影响样本重建，直接读数不变。"""

    _create_sample(client, min_allowed_c=-80, max_allowed_c=8)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")

    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "g2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0},
        {"event_id": "g3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 2.0},
    ])
    # 直接读数保持冻结态，不替记录仪闭环
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T11:00:00Z", "temperature_c": -22.0},
    ]})
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    binding = client.get("/samples/s1/logger-bindings").json()[0]
    bid = binding["id"]

    # 缩小窗口，g3(10:00) 被排除 -> 没有记录仪解冻点，循环消失
    r = client.patch(f"/logger-bindings/{bid}", json={"end_time": "2026-09-20T09:00:00Z"})
    assert r.status_code == 200
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0
    rows = client.get("/samples/s1/readings?source=logger").json()
    assert [x["event_id"] for x in rows] == ["g1", "g2"]
    # 无重复
    assert len({x["logger_reading_id"] for x in rows}) == len(rows)
    # 直接读数不动
    assert [x["event_id"] for x in client.get("/samples/s1/readings?source=direct").json()] == ["d1"]

    # 扩回窗口 -> 归属重建，循环恢复
    client.patch(f"/logger-bindings/{bid}", json={"end_time": "2026-09-20T12:00:00Z"})
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1
    assert len(client.get("/samples/s1/readings?source=logger").json()) == 3

    # PATCH 成与自身重叠校验（缩小到零长度 422；与其他窗口重叠 422）
    assert client.patch(
        f"/logger-bindings/{bid}",
        json={"start_time": "2026-09-20T12:00:00Z", "end_time": "2026-09-20T11:00:00Z"},
    ).status_code == 422
    _bind(client, "s1", "lg1", "2026-09-20T13:00:00Z", "2026-09-20T14:00:00Z")
    assert client.patch(
        f"/logger-bindings/{bid}",
        json={"start_time": "2026-09-20T13:30:00Z"},
    ).status_code == 422

    # DELETE 绑定：记录仪归属全撤销
    assert client.delete(f"/logger-bindings/{bid}").status_code == 204
    assert client.get("/samples/s1/readings?source=logger").json() == []
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0
    assert client.delete(f"/logger-bindings/{bid}").status_code == 404
    assert client.patch(
        f"/logger-bindings/{bid}", json={"end_time": "2026-09-20T12:00:00Z"}
    ).status_code == 404
    # 直接读数仍在
    assert client.get("/samples/s1/readings?source=direct").json()[0]["event_id"] == "d1"


def test_logger_derived_alerts_have_source_and_preserve_ack(client) -> None:
    """记录仪读数产生的告警标注 logger_reading_id；直接/记录仪同名告警不冲突；
    窗口重建后已确认告警按稳定去重键保留。"""

    _create_sample(client, min_allowed_c=-80, max_allowed_c=8)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")

    # 记录仪越限
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85.0},
        {"event_id": "g2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 2.0},
    ])
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["alert_type"] == "temperature_excursion"
    assert a["reading_id"] is None
    assert a["logger_reading_id"] is not None
    client.post(f"/alerts/{a['id']}/ack", json={"acknowledged": True})

    # 直接读数在另一时刻也越限：去重键不同，两条告警共存
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -90.0},
        {"event_id": "d2", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2.0},
    ]})
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 2
    by_source = {a["reading_id"] is not None: a for a in alerts}
    assert by_source[True]["reading_id"] is not None
    assert by_source[False]["logger_reading_id"] is not None

    # 迟到记录仪读数触发重算：记录仪告警 id/确认状态保留，且不重复
    _logger_submit(client, "lg1", [
        {"event_id": "late", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -86.0},
    ])
    alerts = client.get("/alerts?sample_id=s1").json()
    logger_alerts = [x for x in alerts if x["logger_reading_id"] is not None]
    assert len(logger_alerts) == 1
    assert logger_alerts[0]["id"] == a["id"]
    assert logger_alerts[0]["acknowledged"] is True

    # 窗口收缩为不覆盖任何读数的合法空窗口：记录仪告警撤销；直接读数告警保留
    bid = client.get("/samples/s1/logger-bindings").json()[0]["id"]
    client.patch(f"/logger-bindings/{bid}", json={
        "start_time": "2026-09-20T06:00:00Z",
        "end_time": "2026-09-20T07:00:00Z",
    })
    alerts = client.get("/alerts?sample_id=s1").json()
    assert all(x["logger_reading_id"] is None for x in alerts)
    assert len(alerts) == 1 and alerts[0]["reading_id"] is not None


def test_logger_cycle_alert_ack_survives_window_change(client) -> None:
    """循环超限告警按循环序号去重：窗口变更使闭环锚点从记录仪读数切换为
    直接读数时，同一条告警（同 id、已确认）保留；循环彻底不成立时按既有
    陈旧告警清理规则删除，恢复后为新告警行。"""

    _create_sample(client, max_freeze_thaw_cycles=0, min_allowed_c=None, max_allowed_c=None)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    # 一次完整循环（全部记录仪读数）-> cycle_limit 告警
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -25.0},
        {"event_id": "g2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 2.0},
    ])
    alert = next(
        a for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "cycle_limit_exceeded"
    )
    client.post(f"/alerts/{alert['id']}/ack", json={"acknowledged": True})

    # 加入一个 10:00 的直接解冻读数；再把记录仪解冻点 g2 移出窗口
    # （end 改为 09:00，左闭右开排除 g2，g1 冻结点仍在窗口内）：
    # 冻结由记录仪读数触发、解冻改由直接读数闭环，循环仍然成立
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 2.0},
    ]})
    bid = client.get("/samples/s1/logger-bindings").json()[0]["id"]
    client.patch(f"/logger-bindings/{bid}", json={"end_time": "2026-09-20T09:00:00Z"})
    same = next(
        a for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "cycle_limit_exceeded"
    )
    assert same["id"] == alert["id"]
    assert same["acknowledged"] is True
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 只把起点移到 10:00 会与现有终点 09:00 倒挂 -> 422，绑定不变
    assert client.patch(
        f"/logger-bindings/{bid}", json={"start_time": "2026-09-20T10:00:00Z"}
    ).status_code == 422
    # 同时给出合法的空窗口：g1 冻结点也离开 -> 循环不成立，陈旧告警删除
    client.patch(f"/logger-bindings/{bid}", json={
        "start_time": "2026-09-20T10:00:00Z",
        "end_time": "2026-09-20T11:00:00Z",
    })
    assert not [
        a for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "cycle_limit_exceeded"
    ]

    # 恢复窗口：按既有陈旧告警语义，重新出现的是全新、未确认告警（无重复）
    client.patch(f"/logger-bindings/{bid}", json={"start_time": "2026-09-20T08:00:00Z"})
    again = [
        a for a in client.get("/alerts?sample_id=s1").json()
        if a["alert_type"] == "cycle_limit_exceeded"
    ]
    assert len(again) == 1
    assert again[0]["acknowledged"] is False


def test_sample_and_logger_delete_cascade(client) -> None:
    _create_sample(client, sid="s1")
    _create_sample(client, sid="s2")
    _register_logger(client, "lg1")
    _register_logger(client, "lg2")
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    _bind(client, "s2", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    _bind(client, "s1", "lg2", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -25.0},
        {"event_id": "g2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 2.0},
    ])
    _logger_submit(client, "lg2", [
        {"event_id": "h1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -25.0},
        {"event_id": "h2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 2.0},
    ])
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # 删样本：其绑定/归属/告警级联，其他样本不受影响
    assert client.delete("/samples/s1").status_code == 204
    assert client.get("/loggers/lg1/bindings").json() == [
        b for b in client.get("/loggers/lg1/bindings").json()
    ]
    assert {b["sample_id"] for b in client.get("/loggers/lg1/bindings").json()} == {"s2"}
    assert client.get("/loggers/lg2/bindings").json() == []
    assert client.get("/samples/s2/state").json()["cycle_count"] == 1
    # 记录仪原始读数未受影响
    assert len(client.get("/loggers/lg1/readings").json()) == 2

    # 删记录仪：级联读数/绑定，样本撤销归属并重算
    assert client.delete("/loggers/lg1").status_code == 204
    assert client.get("/loggers/lg1").status_code == 404
    assert client.get("/samples/s2/state").json()["cycle_count"] == 0
    assert client.get("/samples/s2/readings?source=logger").json() == []
    assert client.delete("/loggers/lg1").status_code == 404
    # 另一台记录仪与 s2 无绑定，不受影响
    assert client.get("/loggers/lg2").status_code == 200


def test_logger_readings_before_binding_are_attributed_when_bound(client) -> None:
    """读数可以早于绑定登记：建立绑定时对账补建归属并重算。"""

    _create_sample(client)
    _register_logger(client)
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "g2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0},
        {"event_id": "g3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 2.0},
    ])
    assert client.get("/samples/s1/readings?source=logger").json() == []
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0

    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    rows = client.get("/samples/s1/readings?source=logger").json()
    assert [x["event_id"] for x in rows] == ["g1", "g2", "g3"]
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1


# ---------------- 样本 × 记录仪温度一致性核查 ----------------


def _consistency(client, sid, lid, max_diff, threshold):
    return client.get(
        f"/samples/{sid}/logger-consistency/{lid}",
        params={"max_time_diff_seconds": max_diff, "temp_diff_threshold_c": threshold},
    )


def _consistency_setup(client):
    """直接读数 08:00/08:10/08:20，记录仪读数 08:01/08:11/08:25（均在绑定窗口内）。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "d2", "event_time": "2026-09-20T08:10:00Z", "temperature_c": -21.0},
        {"event_id": "d3", "event_time": "2026-09-20T08:20:00Z", "temperature_c": 3.0},
    ]})
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:01:00Z", "temperature_c": 4.5},
        {"event_id": "g2", "event_time": "2026-09-20T08:11:00Z", "temperature_c": -19.0},
        {"event_id": "g3", "event_time": "2026-09-20T08:25:00Z", "temperature_c": 9.0},
    ])


def test_consistency_basic_pairing_and_anomalies(client) -> None:
    _consistency_setup(client)
    r = _consistency(client, "s1", "lg1", 600, 1.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_id"] == "s1"
    assert body["logger_id"] == "lg1"
    assert body["max_time_diff_seconds"] == 600.0
    assert body["temp_diff_threshold_c"] == 1.0
    # 三对全部配上：0.5 / 2.0 / 6.0 °C 温差，后两对超限
    assert body["paired_count"] == 3
    assert body["unmatched_sample_count"] == 0
    assert body["unmatched_logger_count"] == 0
    assert body["anomaly_count"] == 2

    direct = {x["event_id"]: x["id"] for x in client.get("/samples/s1/readings?source=direct").json()}
    logger = {x["event_id"]: x["id"] for x in client.get("/loggers/lg1/readings").json()}
    anomalies = body["anomalies"]
    assert [(a["reading_id"], a["logger_reading_id"]) for a in anomalies] == [
        (direct["d2"], logger["g2"]),
        (direct["d3"], logger["g3"]),
    ]
    a2, a3 = anomalies
    assert a2["sample_event_time"].startswith("2026-09-20T08:10:00")
    assert a2["logger_event_time"].startswith("2026-09-20T08:11:00")
    assert a2["sample_temperature_c"] == -21.0
    assert a2["logger_temperature_c"] == -19.0
    assert a2["time_diff_seconds"] == 60.0
    assert a2["temperature_diff_c"] == 2.0
    assert a3["time_diff_seconds"] == 300.0
    assert a3["temperature_diff_c"] == 6.0


def test_consistency_threshold_is_strict(client) -> None:
    _consistency_setup(client)
    # 阈值 2.0：温差恰好 2.0 的配对不算异常，只剩 6.0 一对
    body = _consistency(client, "s1", "lg1", 600, 2.0).json()
    assert body["anomaly_count"] == 1
    assert body["anomalies"][0]["temperature_diff_c"] == 6.0
    # 阈值 6.0：全部不超限（严格大于才算）
    body = _consistency(client, "s1", "lg1", 600, 6.0).json()
    assert body["anomaly_count"] == 0
    assert body["anomalies"] == []
    # 配对数不受阈值影响（正常配对也占用读数）
    assert body["paired_count"] == 3


def test_consistency_unmatched_counts(client) -> None:
    _consistency_setup(client)
    # 窗口内再补一条远离直接读数的记录仪读数与一条直接读数（相距 30 分钟 > 上限）
    _logger_submit(client, "lg1", [
        {"event_id": "g4", "event_time": "2026-09-20T11:00:00Z", "temperature_c": 5.0},
    ])
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d4", "event_time": "2026-09-20T11:31:00Z", "temperature_c": 5.0},
    ]})
    body = _consistency(client, "s1", "lg1", 600, 1.0).json()
    assert body["paired_count"] == 3
    assert body["unmatched_sample_count"] == 1
    assert body["unmatched_logger_count"] == 1
    # 未配对读数不算温差异常
    assert body["anomaly_count"] == 2

    # 窗口外的记录仪读数不参与归属，进不了配对
    _logger_submit(client, "lg1", [
        {"event_id": "g5", "event_time": "2026-09-20T13:00:00Z", "temperature_c": 99.0},
    ])
    body = _consistency(client, "s1", "lg1", 600, 1.0).json()
    assert body["unmatched_logger_count"] == 1


def test_consistency_max_pairs_then_min_time_diff(client) -> None:
    """配对数优先于总时间差：两对（总时差大）优于一对（总时差小）。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 0.0},
        {"event_id": "d2", "event_time": "2026-09-20T09:40:00Z", "temperature_c": 0.0},
    ]})
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:40:00Z", "temperature_c": 0.0},
        {"event_id": "g2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 0.0},
    ])
    body = _consistency(client, "s1", "lg1", 3600, 0.5).json()
    assert body["paired_count"] == 2
    assert body["unmatched_sample_count"] == 0
    assert body["unmatched_logger_count"] == 0


def test_consistency_tie_break_by_reading_id_sequence(client) -> None:
    """配对数与总时间差都相同时，按配对读数 ID 序列稳定取舍。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    # 先提交 08:10 的读数（id 更小），再提交 08:00 的读数（id 更大）：
    # 两者与 g1(08:05) 的时间差都是 300 秒，目标前两项打平
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d-late", "event_time": "2026-09-20T08:10:00Z", "temperature_c": 10.0},
    ]})
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d-early", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 0.0},
    ]})
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:05:00Z", "temperature_c": 0.0},
    ])
    direct = {x["event_id"]: x["id"] for x in client.get("/samples/s1/readings?source=direct").json()}
    body = _consistency(client, "s1", "lg1", 600, 1.0).json()
    assert body["paired_count"] == 1
    assert body["unmatched_sample_count"] == 1
    # ID 序列取最小：选中 id 更小的 d-late（温差 10°C 超限）；
    # 若选中 d-early（温差 0）则不会有异常
    assert body["anomaly_count"] == 1
    assert body["anomalies"][0]["reading_id"] == direct["d-late"]
    assert body["anomalies"][0]["temperature_diff_c"] == 10.0
    # 重复查询结果稳定
    assert _consistency(client, "s1", "lg1", 600, 1.0).json() == body


def test_consistency_reflects_late_readings_and_binding_changes(client) -> None:
    """迟到读数与绑定调整后的查询反映最新归属。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 0.0},
        {"event_id": "d2", "event_time": "2026-09-20T08:02:00Z", "temperature_c": 0.0},
    ]})
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:03:00Z", "temperature_c": 0.0},
    ])
    body = _consistency(client, "s1", "lg1", 300, 1.0).json()
    assert body["paired_count"] == 1
    assert body["unmatched_sample_count"] == 1

    # 迟到的记录仪读数 08:01 落入窗口：立即可与新归属配对
    _logger_submit(client, "lg1", [
        {"event_id": "g-late", "event_time": "2026-09-20T08:01:00Z", "temperature_c": 0.0},
    ])
    body = _consistency(client, "s1", "lg1", 300, 1.0).json()
    assert body["paired_count"] == 2
    assert body["unmatched_sample_count"] == 0
    assert body["unmatched_logger_count"] == 0

    # 收缩绑定窗口（右开到 08:02）：g1(08:03) 被移出归属
    bid = client.get("/samples/s1/logger-bindings").json()[0]["id"]
    client.patch(f"/logger-bindings/{bid}", json={"end_time": "2026-09-20T08:02:00Z"})
    body = _consistency(client, "s1", "lg1", 300, 1.0).json()
    assert body["paired_count"] == 1
    assert body["unmatched_sample_count"] == 1
    assert body["unmatched_logger_count"] == 0

    # 删除绑定：归属全部撤销，两侧都不再配对
    client.delete(f"/logger-bindings/{bid}")
    body = _consistency(client, "s1", "lg1", 300, 1.0).json()
    assert body["paired_count"] == 0
    assert body["unmatched_sample_count"] == 2
    assert body["unmatched_logger_count"] == 0
    assert body["anomalies"] == []


def test_consistency_is_read_only(client) -> None:
    """核查不改动读数与既有冻融、剂量、告警结果。"""

    _create_sample(client, min_allowed_c=-80, max_allowed_c=8)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "d2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0},
        {"event_id": "d3", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2.0},
    ]})
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:01:00Z", "temperature_c": 40.0},
        {"event_id": "g2", "event_time": "2026-09-20T08:31:00Z", "temperature_c": -25.5},
    ])

    before = {
        "state": client.get("/samples/s1/state").json(),
        "cycles": client.get("/samples/s1/cycles").json(),
        "alerts": client.get("/alerts?sample_id=s1").json(),
        "readings": client.get("/samples/s1/readings?source=all").json(),
        "logger_readings": client.get("/loggers/lg1/readings").json(),
    }
    r = _consistency(client, "s1", "lg1", 600, 1.0)
    assert r.status_code == 200
    assert r.json()["anomaly_count"] == 1  # (d1, g1) 温差 36°C
    after = {
        "state": client.get("/samples/s1/state").json(),
        "cycles": client.get("/samples/s1/cycles").json(),
        "alerts": client.get("/alerts?sample_id=s1").json(),
        "readings": client.get("/samples/s1/readings?source=all").json(),
        "logger_readings": client.get("/loggers/lg1/readings").json(),
    }
    assert before == after
    assert after["state"]["cycle_count"] == 1


def test_consistency_validation(client) -> None:
    _create_sample(client)
    _register_logger(client)
    # 样本 / 记录仪不存在
    assert _consistency(client, "nope", "lg1", 600, 1.0).status_code == 404
    assert _consistency(client, "s1", "nope", 600, 1.0).status_code == 404
    # 负的时间差 / 负的温差阈值
    assert _consistency(client, "s1", "lg1", -1, 1.0).status_code == 422
    assert _consistency(client, "s1", "lg1", 600, -0.5).status_code == 422
    # 缺少必填参数
    assert client.get("/samples/s1/logger-consistency/lg1").status_code == 422
    # 零值合法：仅同一时刻的读数可配对
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 1.0},
    ]})
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 5.0},
        {"event_id": "g2", "event_time": "2026-09-20T08:00:30Z", "temperature_c": 5.0},
    ])
    body = _consistency(client, "s1", "lg1", 0, 0).json()
    assert body["paired_count"] == 1
    assert body["unmatched_logger_count"] == 1
    assert body["anomalies"][0]["temperature_diff_c"] == 4.0


# ---------------- 记录仪校准无效时段 ----------------


def _add_invalid_period(client, lid, start, end):
    return client.post(
        f"/loggers/{lid}/invalid-periods",
        json={"start_time": start, "end_time": end},
    )


def test_invalid_period_crud_and_validation(client) -> None:
    _register_logger(client)

    # 非法时段：零长度 / 倒挂
    assert _add_invalid_period(
        client, "lg1", "2026-09-20T10:00:00Z", "2026-09-20T10:00:00Z"
    ).status_code == 422
    assert _add_invalid_period(
        client, "lg1", "2026-09-20T11:00:00Z", "2026-09-20T10:00:00Z"
    ).status_code == 422

    r = _add_invalid_period(client, "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z")
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["logger_id"] == "lg1"
    assert r.json()["start_time"].startswith("2026-09-20T08:00:00")

    # 同一记录仪各种重叠均拒绝
    for start, end in [
        ("2026-09-20T09:00:00Z", "2026-09-20T11:00:00Z"),  # 跨越
        ("2026-09-20T07:00:00Z", "2026-09-20T08:30:00Z"),  # 头交
        ("2026-09-20T09:30:00Z", "2026-09-20T10:30:00Z"),  # 尾交
        ("2026-09-20T08:30:00Z", "2026-09-20T09:30:00Z"),  # 包含
    ]:
        assert _add_invalid_period(client, "lg1", start, end).status_code == 422

    # 端点相接（左闭右开）允许
    assert _add_invalid_period(
        client, "lg1", "2026-09-20T10:00:00Z", "2026-09-20T12:00:00Z"
    ).status_code == 201
    # 不同记录仪互不影响（同时段合法）
    _register_logger(client, "lg2")
    assert _add_invalid_period(
        client, "lg2", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z"
    ).status_code == 201

    # 列表与单条查询
    periods = client.get("/loggers/lg1/invalid-periods").json()
    assert [p["id"] for p in periods] == sorted(p["id"] for p in periods)
    assert len(periods) == 2
    assert periods[0]["logger_name"] == "记录仪 A"
    assert periods[0]["reading_count"] == 0
    assert client.get(f"/invalid-periods/{pid}").json()["id"] == pid

    # 404：不存在的记录仪 / 时段
    assert _add_invalid_period(
        client, "nope", "2026-09-20T08:00:00Z", "2026-09-20T09:00:00Z"
    ).status_code == 404
    assert client.get("/loggers/nope/invalid-periods").status_code == 404
    assert client.get("/invalid-periods/999").status_code == 404
    assert client.patch(
        "/invalid-periods/999", json={"end_time": "2026-09-20T12:00:00Z"}
    ).status_code == 404
    assert client.delete("/invalid-periods/999").status_code == 404

    # PATCH：与现有另一端点合并后倒挂 422；与其他时段重叠 422；合法修改 200
    assert client.patch(
        f"/invalid-periods/{pid}", json={"start_time": "2026-09-20T11:00:00Z"}
    ).status_code == 422
    assert client.patch(
        f"/invalid-periods/{pid}", json={"end_time": "2026-09-20T11:00:00Z"}
    ).status_code == 422
    r = client.patch(f"/invalid-periods/{pid}", json={"end_time": "2026-09-20T09:00:00Z"})
    assert r.status_code == 200
    assert r.json()["end_time"].startswith("2026-09-20T09:00:00")

    # DELETE
    assert client.delete(f"/invalid-periods/{pid}").status_code == 204
    assert client.get(f"/invalid-periods/{pid}").status_code == 404
    assert client.delete(f"/invalid-periods/{pid}").status_code == 404


def test_invalid_period_readings_stored_but_not_attributed(client) -> None:
    """落在无效时段的记录仪读数照常幂等入库、可经原始接口查询，但不归属样本。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
        {"event_id": "g2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0},
        {"event_id": "g3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 2.0},
    ])
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1

    # [08:30, 09:00) 标为校准无效：恰好覆盖 g2（左闭），不覆盖 g3（右开）-> 循环消失
    r = _add_invalid_period(client, "lg1", "2026-09-20T08:30:00Z", "2026-09-20T09:00:00Z")
    assert r.status_code == 201
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0

    # 原始读数仍在、可查、幂等规则不变
    rows = client.get("/loggers/lg1/readings").json()
    assert [x["event_id"] for x in rows] == ["g1", "g2", "g3"]
    dup = _logger_submit(client, "lg1", [
        {"event_id": "g2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -25.0},
    ])
    assert dup.json()["duplicates"] == 1

    # 归属中只剩 g1、g3；合并时间轴同样不含 g2
    attributed = client.get("/samples/s1/readings?source=logger").json()
    assert [x["event_id"] for x in attributed] == ["g1", "g3"]
    merged = client.get("/samples/s1/readings").json()
    assert [x["event_id"] for x in merged] == ["g1", "g3"]

    # 绑定列表的归属计数同步排除无效读数
    binding = client.get("/samples/s1/logger-bindings").json()[0]
    assert binding["reading_count"] == 2
    # 无效时段列表报告被排除的读数条数
    period = client.get("/loggers/lg1/invalid-periods").json()[0]
    assert period["reading_count"] == 1


def test_invalid_period_excludes_from_dose_alerts_and_restore(client) -> None:
    """无效时段读数不参与暴露剂量与告警；删除时段后恢复归属并回算。"""

    _create_sample(client, min_allowed_c=-80, max_allowed_c=8)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    # 0 -> 20 -> 0 °C：两段对称三角，热剂量 2 × (0.5 × 12 × 36) = 432；
    # g2(20°C) 越上限产生一条记录仪越限告警
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 0.0},
        {"event_id": "g2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 20.0},
        {"event_id": "g3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 0.0},
    ])
    state = client.get("/samples/s1/state").json()
    assert state["heat_dose_c_min"] == 432.0
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 1 and alerts[0]["logger_reading_id"] is not None

    # [08:30, 09:30) 无效：g2 排除 -> 时间轴剩 0 -> 0，剂量归零、告警撤销
    pid = _add_invalid_period(
        client, "lg1", "2026-09-20T08:30:00Z", "2026-09-20T09:30:00Z"
    ).json()["id"]
    state = client.get("/samples/s1/state").json()
    assert state["heat_dose_c_min"] == 0.0
    assert client.get("/alerts?sample_id=s1").json() == []

    # PATCH 时段使其不再覆盖 g2 -> 归属恢复、剂量与告警回算
    r = client.patch(f"/invalid-periods/{pid}", json={
        "start_time": "2026-09-20T11:00:00Z",
        "end_time": "2026-09-20T11:30:00Z",
    })
    assert r.status_code == 200
    assert client.get("/samples/s1/state").json()["heat_dose_c_min"] == 432.0
    assert len(client.get("/alerts?sample_id=s1").json()) == 1

    # 重新覆盖 g2 后再删除时段 -> 有效读数恢复归属
    client.patch(f"/invalid-periods/{pid}", json={
        "start_time": "2026-09-20T08:30:00Z",
        "end_time": "2026-09-20T09:30:00Z",
    })
    assert client.get("/samples/s1/state").json()["heat_dose_c_min"] == 0.0
    assert client.delete(f"/invalid-periods/{pid}").status_code == 204
    state = client.get("/samples/s1/state").json()
    assert state["heat_dose_c_min"] == 432.0
    assert [x["event_id"] for x in client.get("/samples/s1/readings?source=logger").json()] == [
        "g1", "g2", "g3",
    ]
    # 告警按既有陈旧清理规则重新出现（新行、未确认）
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 1
    assert alerts[0]["logger_reading_id"] is not None
    assert alerts[0]["acknowledged"] is False


def test_invalid_period_late_reading_and_delete_restores(client) -> None:
    """补入落在无效时段的迟到读数：照常入库但不归属；删除时段后恢复归属。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    pid = _add_invalid_period(
        client, "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z"
    ).json()["id"]

    # 迟到读数落在无效时段：accepted=1、原始可查，但不归属、不参与推导
    r = _logger_submit(client, "lg1", [
        {"event_id": "late", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -25.0},
    ])
    assert r.json()["accepted"] == 1
    assert len(client.get("/loggers/lg1/readings").json()) == 1
    assert client.get("/samples/s1/readings?source=logger").json() == []
    state = client.get("/samples/s1/state").json()
    assert state["last_event_time"] is None
    assert state["cycle_count"] == 0

    # 再补一条窗口内、无效时段外的解冻读数：仍只有它归属
    _logger_submit(client, "lg1", [
        {"event_id": "ok", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2.0},
    ])
    assert [x["event_id"] for x in client.get("/samples/s1/readings?source=logger").json()] == ["ok"]
    assert client.get("/samples/s1/state").json()["cycle_count"] == 0

    # 删除无效时段：late 恢复归属 -> 冻结(09:00) + 解冻(10:30) 闭环一次循环
    assert client.delete(f"/invalid-periods/{pid}").status_code == 204
    assert [x["event_id"] for x in client.get("/samples/s1/readings?source=logger").json()] == [
        "late", "ok",
    ]
    assert client.get("/samples/s1/state").json()["cycle_count"] == 1


def test_invalid_period_preserves_direct_readings_and_ack(client) -> None:
    """无效时段回算不改动直接读数；既有告警确认状态按现有规则保留。"""

    _create_sample(client, min_allowed_c=-80, max_allowed_c=8)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    # 直接读数越限并确认其告警
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85.0},
        {"event_id": "d2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2.0},
    ]})
    alert = client.get("/alerts?sample_id=s1").json()[0]
    assert alert["reading_id"] is not None
    client.post(f"/alerts/{alert['id']}/ack", json={"acknowledged": True})

    # 记录仪读数也产生一条越限告警
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -90.0},
    ])
    assert len(client.get("/alerts?sample_id=s1").json()) == 2

    # 无效时段覆盖 g1：记录仪告警撤销；直接读数告警 id 与确认状态保留
    pid = _add_invalid_period(
        client, "lg1", "2026-09-20T08:45:00Z", "2026-09-20T09:15:00Z"
    ).json()["id"]
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 1
    assert alerts[0]["id"] == alert["id"]
    assert alerts[0]["acknowledged"] is True
    # 直接读数本身不变
    direct = client.get("/samples/s1/readings?source=direct").json()
    assert [x["event_id"] for x in direct] == ["d1", "d2"]

    # 删除时段：记录仪告警按既有规则重新出现，直接读数告警确认仍保留
    client.delete(f"/invalid-periods/{pid}")
    alerts = client.get("/alerts?sample_id=s1").json()
    assert len(alerts) == 2
    direct_alert = next(a for a in alerts if a["reading_id"] is not None)
    assert direct_alert["id"] == alert["id"]
    assert direct_alert["acknowledged"] is True


def test_invalid_period_excluded_from_consistency(client) -> None:
    """一致性核查只使用当前有效归属：无效时段读数不参与配对。"""

    _create_sample(client)
    _register_logger(client)
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T12:00:00Z")
    client.post("/samples/s1/readings", json={"readings": [
        {"event_id": "d1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 0.0},
        {"event_id": "d2", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 0.0},
    ]})
    _logger_submit(client, "lg1", [
        {"event_id": "g1", "event_time": "2026-09-20T08:01:00Z", "temperature_c": 0.0},
        {"event_id": "g2", "event_time": "2026-09-20T09:01:00Z", "temperature_c": 0.0},
    ])
    body = _consistency(client, "s1", "lg1", 300, 1.0).json()
    assert body["paired_count"] == 2

    # g1 落入无效时段 -> 只剩一对，g1 不再计入未配对（已不属于有效归属）
    pid = _add_invalid_period(
        client, "lg1", "2026-09-20T08:00:00Z", "2026-09-20T08:30:00Z"
    ).json()["id"]
    body = _consistency(client, "s1", "lg1", 300, 1.0).json()
    assert body["paired_count"] == 1
    assert body["unmatched_sample_count"] == 1
    assert body["unmatched_logger_count"] == 0

    # 删除时段后恢复
    client.delete(f"/invalid-periods/{pid}")
    body = _consistency(client, "s1", "lg1", 300, 1.0).json()
    assert body["paired_count"] == 2
    assert body["unmatched_sample_count"] == 0
    assert body["unmatched_logger_count"] == 0


def test_invalid_period_is_per_logger_and_cascades(client) -> None:
    """无效时段只影响所属记录仪；删除记录仪级联清理其无效时段。"""

    _create_sample(client)
    _register_logger(client, "lg1")
    _register_logger(client, "lg2")
    # 同一样本的绑定时段不得重叠（跨记录仪亦然）：两台记录仪各管一段
    _bind(client, "s1", "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z")
    _bind(client, "s1", "lg2", "2026-09-20T10:00:00Z", "2026-09-20T12:00:00Z")
    _logger_submit(client, "lg1", [
        {"event_id": "a1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -25.0},
    ])
    _logger_submit(client, "lg2", [
        {"event_id": "b1", "event_time": "2026-09-20T11:00:00Z", "temperature_c": -25.0},
    ])
    assert len(client.get("/samples/s1/readings?source=logger").json()) == 2

    # lg1 的无效时段只排除 lg1 的读数
    _add_invalid_period(client, "lg1", "2026-09-20T08:00:00Z", "2026-09-20T10:00:00Z")
    rows = client.get("/samples/s1/readings?source=logger").json()
    assert [x["event_id"] for x in rows] == ["b1"]
    # lg1 原始读数仍在
    assert len(client.get("/loggers/lg1/readings").json()) == 1

    # 删除记录仪：级联清理无效时段；重新登记同名记录仪后时段列表为空
    assert client.delete("/loggers/lg1").status_code == 204
    _register_logger(client, "lg1")
    assert client.get("/loggers/lg1/invalid-periods").json() == []
    # lg2 的归属不受影响
    assert [x["event_id"] for x in client.get("/samples/s1/readings?source=logger").json()] == ["b1"]
