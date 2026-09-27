#!/usr/bin/env bash
# 端到端示例：创建样本 -> 正常冻融 -> 迟到数据重排 -> 幂等去重 -> 越限/超限告警
#           -> 暴露剂量预算告警 -> 查询
# 用法: ./examples/demo.sh （需先在 localhost:8000 启动服务）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
SID="sample-demo-001"

req() { curl -sS -X "$@"; }

echo "== 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 创建样本（低温剂量预算 50°C·min / 高温 200°C·min，最大积分间隔 120 分钟）=="
curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' -d "{
  \"id\": \"$SID\",
  \"name\": \"新冠灭活疫苗 批次 A-2026-09\",
  \"freeze_threshold_c\": -20,
  \"thaw_threshold_c\": 0,
  \"min_allowed_c\": -80,
  \"max_allowed_c\": 8,
  \"max_freeze_thaw_cycles\": 2,
  \"cold_dose_budget_c_min\": 50,
  \"heat_dose_budget_c_min\": 200,
  \"max_integration_interval_minutes\": 120
}"; echo

echo "== 第一批读数（完成第 1 次冻融）=="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-001", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
    {"event_id": "evt-002", "event_time": "2026-09-20T08:15:00Z", "temperature_c": -5.0},
    {"event_id": "evt-003", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22.0},
    {"event_id": "evt-004", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -10.0},
    {"event_id": "evt-005", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2.0}
  ]
}'; echo

echo "== 迟到数据（09:00/09:30，重排后与 10:00/10:30 共同多出一次循环）=="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-late-1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 3.0},
    {"event_id": "evt-late-2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": -25.0}
  ]
}'; echo

echo "== 重复 event_id（duplicates 应为 1，accepted 为 0）=="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-003", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22.0}
  ]
}'; echo

echo "== 越限 + 第 3 次循环 =="
curl -sS -X POST "$BASE/samples/$SID/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "evt-006", "event_time": "2026-09-20T11:00:00Z", "temperature_c": -85.5},
    {"event_id": "evt-007", "event_time": "2026-09-20T11:30:00Z", "temperature_c": 5.0}
  ]
}'; echo

echo "== 当前状态（含双向暴露剂量、缺口时长与预算使用率）=="
curl -sS "$BASE/samples/$SID/state"; echo

echo "== 循环列表 =="
curl -sS "$BASE/samples/$SID/cycles"; echo

echo "== 全部告警 =="
curl -sS "$BASE/alerts?sample_id=$SID"; echo

echo "== 登记共享记录仪并提交读数 =="
curl -sS -X POST "$BASE/loggers" -H 'Content-Type: application/json' -d '{
  "id": "logger-truck-07", "name": "7 号冷链车厢记录仪"
}'; echo
curl -sS -X POST "$BASE/loggers/logger-truck-07/readings" -H 'Content-Type: application/json' -d '{
  "readings": [
    {"event_id": "lg-1", "event_time": "2026-09-20T08:00:30Z", "temperature_c": 4.2},
    {"event_id": "lg-2", "event_time": "2026-09-20T08:30:20Z", "temperature_c": -21.5},
    {"event_id": "lg-3", "event_time": "2026-09-20T10:30:10Z", "temperature_c": 2.4}
  ]
}'; echo

echo "== 为样本设置记录仪绑定时段（左闭右开 [08:00, 12:00)）=="
curl -sS -X POST "$BASE/samples/$SID/logger-bindings?logger_id=logger-truck-07" \
  -H 'Content-Type: application/json' \
  -d '{"start_time": "2026-09-20T08:00:00Z", "end_time": "2026-09-20T12:00:00Z"}'; echo

echo "== 样本 × 记录仪温度一致性核查（最大时间差 300 秒、温差阈值 2°C；只读）=="
curl -sS "$BASE/samples/$SID/logger-consistency/logger-truck-07?max_time_diff_seconds=300&temp_diff_threshold_c=2.0"; echo

echo "== 登记校准无效时段 [09:30, 10:00)：时段内记录仪读数不归属样本（原始读数仍入库可查）=="
curl -sS -X POST "$BASE/loggers/logger-truck-07/invalid-periods" \
  -H 'Content-Type: application/json' \
  -d '{"start_time": "2026-09-20T09:30:00Z", "end_time": "2026-09-20T10:00:00Z"}'; echo

echo "== 无效时段列表与归属变化 =="
curl -sS "$BASE/loggers/logger-truck-07/invalid-periods"; echo
curl -sS "$BASE/samples/$SID/readings?source=logger"; echo

echo "== 删除无效时段（有效读数恢复归属，样本自动回算）=="
PID=$(curl -sS "$BASE/loggers/logger-truck-07/invalid-periods" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
curl -sS -X DELETE "$BASE/invalid-periods/$PID"; echo
curl -sS "$BASE/samples/$SID/readings?source=logger"; echo
