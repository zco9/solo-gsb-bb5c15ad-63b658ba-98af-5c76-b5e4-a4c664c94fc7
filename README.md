# 实验室样本冻融追踪 API

基于 **FastAPI + SQLite + Docker** 的服务，用于追踪实验室样本（疫苗、生物制剂、细胞株等）在冷链过程中的冻融循环：

- 客户端按样本提交**带事件时间**的温度读数；
- 服务端按 `event_time` 时间顺序**重排迟到数据**后重算，而不是按入库顺序处理；
- 使用可配置的**冻结/解冻阈值 + 滞回（Schmitt 触发器）**规则识别完整冻融循环，避免临界温度抖动造成的误判；
- 重复事件通过客户端 `event_id`（或“同时刻同温度”签名）**幂等去重**；
- 暴露样本**当前状态、完整循环次数/明细、温度越限与循环超限告警**查询与确认接口；
- 按相邻读数线性插值累计**双向温度暴露剂量（超限幅度 × 分钟）**，支持低温/高温剂量预算、
  最大可积分间隔（缺口跳过）与预算超限告警；
- 支持**共享温度记录仪**：登记记录仪与带 `event_id` 的读数，为样本设置记录仪绑定的
  **左闭右开时段**；记录仪读数仅归入发生时间落在绑定时段内的样本（同一读数可归入多个样本），
  与样本直接读数合并到同一条时间轴上重算冻融循环、剂量与告警；迟到读数或绑定时段变更会
  撤销旧归属并重建受影响样本；
- 支持**记录仪校准无效时段**：按左闭右开时段登记（同一记录仪互不重叠），时段内的记录仪读数
  照常幂等入库、可经原始读数接口查询，但不归属样本，也不参与冻融循环、暴露剂量、告警与
  温度一致性核查；时段增删改或迟到读数到达后自动对受影响样本重新对账回算，
  删除时段后有效读数恢复归属；
- 提供**样本 × 记录仪温度一致性核查**：取样本直接读数与当前有效归属的该记录仪读数，
  按事件时间顺序做**不交叉一对一配对**（配对数最多 → 总时间差最小 → 读数 ID 序列稳定取舍），
  返回温差严格大于阈值的配对明细与配对 / 两侧未配对数量；只读查询，不改写任何既有结果；
- 容器启动即自动初始化数据库（建表、索引），旧版本数据库自动兼容迁移。

## 目录结构

```
app/
  main.py        # FastAPI 路由与启动初始化
  engine.py      # 纯函数冻融状态机（循环识别 + 告警推导），不依赖 Web/DB
  matching.py    # 纯函数一致性配对（不交叉一对一最优配对），不依赖 Web/DB
  database.py    # SQLite 连接与幂等建表脚本
  schemas.py     # Pydantic 请求/响应模型
  services.py    # 业务层：幂等写入、时间重排重算、告警确认、一致性核查
tests/           # pytest：引擎/配对单元测试 + API 集成测试
examples/        # requests.http / demo.sh 示例请求
Dockerfile
docker-compose.yml
```

## 快速开始（Docker）

```bash
docker compose up --build
```

服务监听 http://localhost:8000 ：

- Swagger 文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health

SQLite 数据文件持久化在 named volume `freeze-thaw-data`（容器内 `/data/freeze_thaw.db`），首次启动自动建库。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 启动（默认数据文件 ./data/freeze_thaw.db，可用 DB_PATH 覆盖）
uvicorn app.main:app --reload

# 运行测试
pytest -q
```

## 冻融识别规则（滞回）

为每个样本配置两个阈值：`freeze_threshold_c < thaw_threshold_c`（示例：-20°C / 0°C）。

| 当前状态 | 条件 | 迁移 |
 | --- | --- | --- |
| `thawed`（已解冻） | 温度 **≤ freeze_threshold_c** | → `frozen`，记录冻结起点 |
| `frozen`（冻结中） | 温度 **≥ thaw_threshold_c** | → `thawed`，**闭环一次完整冻融循环** |
| `frozen` | 温度处于两阈值之间（如 -10°C） | 保持 `frozen`（滞回，不翻转） |
| `thawed` | 温度处于两阈值之间 | 保持 `thawed` |

每个完整循环记录：冻结起点时间/温度、解冻时间/温度、循环段内最低/最高温度。
**只冻结未解冻不算循环**；当前仍处于冻结段时，状态接口会返回冻结起点与段内极值。

### 告警规则

1. **`temperature_excursion`（critical）**：读数超出样本允许区间
   `[min_allowed_c, max_allowed_c]`。连续越限只告警一次（去抖），回到区间后再次越限才再次告警。
2. **`cycle_limit_exceeded`（warning）**：完整循环次数超过 `max_freeze_thaw_cycles`，每超出一次循环告警一次。

告警支持单条确认、按样本批量确认；重算会保留确认状态（按稳定去重键 upsert）。

## 累计温度暴露剂量

在冻融状态机之外，服务还会按读数时间轴累计**温度暴露剂量**（单位 °C·min，即
“超出允许区间的幅度 × 持续分钟数”）：

1. 对按 `event_time` 排序后的**相邻读数做线性插值**，把曲线越过 `min_allowed_c`
   或 `max_allowed_c` 的区段在跨越点切分（线性线段在一个区间内至多跨越一次）；
2. 低温方向累计 `min_allowed_c - 温度` 的积分，高温方向累计
   `温度 - max_allowed_c` 的积分（梯形面积，界内幅度计 0）；
3. 相邻读数间隔超过 `max_integration_interval_minutes` 时，该区间**整体跳过不积分**，
   间隔时长计入 `exposure_gap_minutes`（缺口时长）；恰好等于上限的间隔仍参与积分；
4. 任一方向累计剂量**首次达到**对应预算（`cold_dose_budget_c_min` /
   `heat_dose_budget_c_min`）时，产生一条 `exposure_budget_exceeded`（critical）告警，
   告警事件时间为插值求出的精确跨越时刻；每个方向至多一条，去重键为
   `sample_id:exposure_budget:cold|heat`。

剂量、缺口与告警全部由重算流程派生：**迟到读数补录、阈值 / 预算 / 积分间隔 PATCH
修改后都会整体重算**——预算放宽后超限告警会消失（确认状态随去重键保留），预算收紧后
会重新出现，保证剂量与告警可逆更新。

> 配置剂量预算时必须同时提供对应的允许区间边界（冷预算需 `min_allowed_c`，
> 热预算需 `max_allowed_c`）。

## 迟到数据与重算

每次成功写入新读数后，服务都会：

1. 取出该样本**全部**读数，按 `(event_time, id)` 升序重排（迟到数据自然回到正确时间位置）；
2. 用 `engine.derive` 纯函数重放状态机，重建完整循环、暴露剂量与告警；
3. 刷新样本当前状态缓存。

PATCH 样本的阈值、允许区间、循环上限、剂量预算或最大可积分间隔后，也会走同一条重算
流程，因此剂量与预算告警对“迟到数据”和“配置变更”都保持可逆、可重复的更新语义。

也可调用 `POST /samples/{id}/recompute` 手动触发兜底重算。

## 共享温度记录仪与绑定时段

多台物理记录仪（如冷链车厢/冰箱共用的温度探头）可独立登记，其读数通过**绑定时段**
归入一个或多个样本：

1. `POST /loggers` 登记记录仪（自带 ID 或由服务端生成 UUID），
   `POST /loggers/{id}/readings` 提交带 `event_id`、发生时间、温度的读数，
   幂等规则与样本读数一致（`event_id` 唯一，或同时刻同温度判重）；
2. `POST /samples/{id}/logger-bindings?logger_id=...` 为样本设置一个
   **左闭右开 `[start_time, end_time)`** 的绑定时段；
3. 归属规则：记录仪读数当且仅当 `start_time <= event_time < end_time` 时归入该样本。
   - **同一样本**的绑定时段（允许跨不同记录仪）**不得重叠**（端点相接合法），重叠提交返回 422；
   - 不同样本可绑定同一时段，因此**同一条记录仪读数可归入多个样本**；
4. 重算时把记录仪归属读数与样本**直接读数合并到同一条时间轴**（同时间戳直接读数在前），
   按既有滞回状态机、剂量积分、告警规则统一推导；直接读数始终独立保存、不会被记录仪流程修改；
5. **迟到读数**（时间早于已提交数据）或**绑定时段 PATCH/DELETE** 都会对受影响样本做
   **归属对账**（删除失效归属、补建新归属）后整体重算：不产生重复派生读数，
   循环/剂量/告警随之可逆更新；
6. 删除样本或记录仪会级联清理对应绑定与归属；删除记录仪后受影响样本自动重算。

记录仪读数派生的告警带有 `logger_reading_id`（直接读数告警仍为 `reading_id`）；
温度越限告警按“来源 + 读数”去重，因此同一时间轴上直接读数与记录仪读数各自的越限
不会互相覆盖；循环超限（按循环序号）与剂量预算（按方向）告警与读数来源无关，
窗口变化使锚点读数在直接/记录仪间切换时确认状态仍保留。

查询：

- `GET /samples/{id}/logger-bindings` / `GET /loggers/{id}/bindings`：样本 / 记录仪两侧的绑定列表；
- `GET /loggers/{id}/readings`：记录仪原始读数；
- `GET /samples/{id}/readings?source=all|direct|logger`：样本读数，默认 `all`
  为直接读数与记录仪归属读数合并后的时间轴（返回项带 `source`、`logger_id`、`logger_reading_id`）；
- `GET /samples/{id}/state` 的最近读数/循环/剂量同样基于合并时间轴。

## 记录仪校准无效时段

记录仪送校 / 故障 / 维护期间产生的读数不可信，可登记**校准无效时段**将其排除出样本归属：

1. `POST /loggers/{id}/invalid-periods` 登记**左闭右开 `[start_time, end_time)`** 时段
   （`start_time` 必须严格早于 `end_time`）；**同一记录仪**的时段不得重叠
   （端点相接合法），重叠提交返回 422；
2. 落在无效时段内的记录仪读数**仍照常幂等入库**（`event_id` / 同时刻同温度判重规则不变），
   并可通过 `GET /loggers/{id}/readings` 查询原始数据；
3. 但这些读数**不归属任何样本**——对账时一律跳过，因此不参与合并时间轴上的
   冻融循环识别、暴露剂量积分、告警推导与样本 × 记录仪温度一致性核查
   （一致性核查的“当前有效归属”自然不含它们）；
4. 新增、`PATCH`、删除时段或补入迟到读数后，服务对所有绑定该记录仪的样本
   **重新对账归属并整体回算**：循环 / 剂量 / 告警随之可逆更新，
   直接读数与既有告警确认状态按现有规则（稳定去重键 upsert）保留；
5. **删除时段后**，原时段内的有效读数恢复归属并重新参与上述推导；
   删除记录仪会级联清理其全部无效时段。

查询：

- `GET /loggers/{id}/invalid-periods`：该记录仪的无效时段列表
  （`reading_count` 为当前落在时段内、被排除归属的原始读数条数）；
- `GET /invalid-periods/{id}`：单条时段详情；
- `PATCH/DELETE /invalid-periods/{id}`：修改 / 删除时段（自动对账回算受影响样本）。

## 样本 × 记录仪温度一致性核查

`GET /samples/{sample_id}/logger-consistency/{logger_id}?max_time_diff_seconds=300&temp_diff_threshold_c=2.0`

用于核查“样本直接读数”与“共享记录仪读数”是否一致（例如样本自带探头与车厢记录仪互证）：

1. **取数**：样本的全部直接读数，以及**当前有效归属**于该样本的该记录仪读数
   （即落在现行绑定时段内的归属读数；迟到读数、绑定新建 / PATCH / DELETE 后的
   查询立即反映最新归属）；
2. **配对**：两侧分别按 `(event_time, id)` 升序，做**不交叉的一对一配对**
   （保序：若两对配对交叉，即直接读数 a₁ 在 a₂ 前、而其配对记录仪读数却在
   a₂ 的配对读数之后，则不允许）。仅当两条读数
   事件时间差的绝对值 `<= max_time_diff_seconds`（非负，恰好等于也可配对）时才允许配对。
   配对目标按字典序依次取舍：
   1. **配对数最多**；
   2. **总时间差最小**（各配对时间差绝对值之和）；
   3. 仍相同时，**配对读数 ID 序列** `[(直接读数 id, 记录仪读数 id), ...]`
      按字典序取最小——结果稳定、可复现；
3. **判定**：配对温差（绝对值）**严格大于** `temp_diff_threshold_c`（非负）的配对
   计入异常明细；**正常配对同样占用读数**，未配对读数不参与温差判定
   （未配对不算温差异常）。

响应字段：

| 字段 | 说明 |
| --- | --- |
| `paired_count` | 成功配对的读数对数（含温差正常的配对） |
| `unmatched_sample_count` / `unmatched_logger_count` | 两侧各自未配对的读数条数 |
| `anomaly_count` / `anomalies` | 温差超限的配对数与明细（按事件时间顺序） |
| `anomalies[].reading_id` / `logger_reading_id` | 配对两侧读数 ID（`readings.id` / `logger_readings.id`） |
| `anomalies[].sample_event_time` / `logger_event_time` | 两侧事件时间 |
| `anomalies[].sample_temperature_c` / `logger_temperature_c` | 两侧温度（°C） |
| `anomalies[].time_diff_seconds` / `temperature_diff_c` | 该配对的时间差（秒）与温差（°C），均为绝对值 |

本接口为**只读查询**：不改动原始读数、归属关系与既有冻融循环、暴露剂量、告警结果。

示例：

```bash
curl 'http://localhost:8000/samples/sample-demo-001/logger-consistency/logger-truck-07?max_time_diff_seconds=300&temp_diff_threshold_c=2.0'
```

## 幂等去重

- 提供 `event_id` 时：`(sample_id, event_id)` 唯一，重复提交直接跳过；
- 不提供 `event_id` 时：`(sample_id, event_time, temperature_c)` 相同视为重复；
- 同一请求批次内的重复同样会被识别。

批量提交响应返回 `accepted`（新写入）与 `duplicates`（跳过）计数。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/samples` | 创建样本（阈值、允许区间、循环上限可配置） |
| GET | `/samples` | 样本列表（含状态/循环数摘要） |
| GET/PATCH/DELETE | `/samples/{id}` | 样本详情 / 修改配置（自动重算） / 删除 |
| POST | `/samples/{id}/readings` | 批量提交读数（幂等，提交后自动重算，返回新告警） |
| GET | `/samples/{id}/readings?source=all\|direct\|logger` | 读数列表（按事件时间排序；默认合并直接读数与记录仪归属读数） |
| POST/GET | `/loggers` | 登记共享温度记录仪 / 记录仪列表 |
| GET/PATCH/DELETE | `/loggers/{id}` | 记录仪详情 / 改名 / 删除（级联清理并重建受影响样本） |
| POST | `/loggers/{id}/readings` | 批量提交记录仪读数（幂等；自动归属并重算受影响样本，返回新告警） |
| GET | `/loggers/{id}/readings` | 记录仪原始读数列表 |
| POST/GET | `/samples/{id}/logger-bindings?logger_id=...` | 设置记录仪左闭右开绑定时段 / 样本绑定列表（同时段不重叠） |
| GET | `/loggers/{id}/bindings` | 该记录仪对各样本的绑定列表 |
| PATCH/DELETE | `/logger-bindings/{id}` | 修改 / 删除绑定时段（撤销旧归属、重建并重算样本） |
| POST/GET | `/loggers/{id}/invalid-periods` | 登记 / 查询记录仪**校准无效时段**（左闭右开；同一记录仪不重叠；时段内读数入库但不归属、不参与推导） |
| GET/PATCH/DELETE | `/invalid-periods/{id}` | 时段详情 / 修改 / 删除（自动对账回算受影响样本；删除后有效读数恢复归属） |
| GET | `/samples/{id}/logger-consistency/{logger_id}?max_time_diff_seconds=...&temp_diff_threshold_c=...` | **温度一致性核查**：直接读数与当前归属记录仪读数不交叉一对一最优配对，返回温差超限配对明细与配对/未配对数量（只读） |
| GET | `/samples/{id}/state` | **当前状态**、循环次数、双向暴露剂量/缺口/预算使用率、未确认告警数、最近读数 |
| GET | `/samples/{id}/cycles` | **完整冻融循环明细** |
| POST | `/samples/{id}/recompute` | 手动重算 |
| GET | `/alerts` | **告警查询**（可按样本、确认状态过滤，分页） |
| GET/POST | `/alerts/{id}` `/alerts/{id}/ack` | 告警详情 / 确认（或取消确认） |
| POST | `/samples/{id}/alerts/ack` | 批量确认样本下所有告警 |

## 示例请求（curl）

```bash
# 1. 创建样本（含低温/高温剂量预算与 120 分钟最大可积分间隔）
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "sample-demo-001",
    "name": "新冠灭活疫苗 批次 A-2026-09",
    "freeze_threshold_c": -20,
    "thaw_threshold_c": 0,
    "min_allowed_c": -80,
    "max_allowed_c": 8,
    "max_freeze_thaw_cycles": 2,
    "cold_dose_budget_c_min": 50,
    "heat_dose_budget_c_min": 200,
    "max_integration_interval_minutes": 120
  }'

# 2. 提交读数（完成一次冻融）
curl -X POST http://localhost:8000/samples/sample-demo-001/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_id": "evt-1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
    {"event_id": "evt-2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22.0},
    {"event_id": "evt-3", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2.0}
  ]}'

# 3. 迟到数据（09:00/09:30，服务端自动插入时间轴中间重排）
curl -X POST http://localhost:8000/samples/sample-demo-001/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_id": "late-1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -25.0},
    {"event_id": "late-2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 3.0}
  ]}'

# 4. 查询当前状态、暴露剂量与循环
curl http://localhost:8000/samples/sample-demo-001/state
curl http://localhost:8000/samples/sample-demo-001/cycles

# 5. 越限告警 / 未确认告警 / 确认
curl 'http://localhost:8000/alerts?sample_id=sample-demo-001'
curl 'http://localhost:8000/alerts?acknowledged=false'
curl -X POST http://localhost:8000/samples/sample-demo-001/alerts/ack \
  -H 'Content-Type: application/json' -d '{"acknowledged": true}'
```

更多可直接执行的示例：

- `examples/requests.http`（VS Code REST Client / IntelliJ HTTP Client）
- `examples/demo.sh`（curl 端到端脚本：正常数据 → 迟到重排 → 幂等去重 → 越限/超限告警）

```bash
chmod +x examples/demo.sh && ./examples/demo.sh
```

### 共享记录仪示例（curl）

```bash
# 1. 登记记录仪
curl -X POST http://localhost:8000/loggers \
  -H 'Content-Type: application/json' \
  -d '{"id": "logger-truck-07", "name": "7 号冷链车厢记录仪"}'

# 2. 提交记录仪读数（event_id、发生时间、温度）
curl -X POST http://localhost:8000/loggers/logger-truck-07/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_id": "lg-1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 4.0},
    {"event_id": "lg-2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": -22.0},
    {"event_id": "lg-3", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2.0}
  ]}'

# 3. 为样本设置左闭右开绑定时段（同一样本不得重叠）
curl -X POST 'http://localhost:8000/samples/sample-demo-001/logger-bindings?logger_id=logger-truck-07' \
  -H 'Content-Type: application/json' \
  -d '{"start_time": "2026-09-20T08:00:00Z", "end_time": "2026-09-20T12:00:00Z"}'

# 4. 查询绑定（样本侧 / 记录仪侧）与合并后的样本读数
curl http://localhost:8000/samples/sample-demo-001/logger-bindings
curl http://localhost:8000/loggers/logger-truck-07/bindings
curl 'http://localhost:8000/samples/sample-demo-001/readings?source=all'

# 5. 迟到读数：提交 09:00 的补录数据后，受影响样本自动撤销旧归属并重建
curl -X POST http://localhost:8000/loggers/logger-truck-07/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_id": "lg-late", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -25.0}
  ]}'

# 6. 温度一致性核查：样本直接读数 vs 当前归属的记录仪读数
#    （最大时间差 300 秒、温差阈值 2°C；只读，不影响冻融/剂量/告警）
curl 'http://localhost:8000/samples/sample-demo-001/logger-consistency/logger-truck-07?max_time_diff_seconds=300&temp_diff_threshold_c=2.0'

# 7. 校准无效时段：09:30–10:00 记录仪送校，时段内读数不归属样本（原始读数仍入库可查）
curl -X POST http://localhost:8000/loggers/logger-truck-07/invalid-periods \
  -H 'Content-Type: application/json' \
  -d '{"start_time": "2026-09-20T09:30:00Z", "end_time": "2026-09-20T10:00:00Z"}'

# 8. 查询 / 修改 / 删除无效时段（删除后有效读数恢复归属，受影响样本自动回算）
curl http://localhost:8000/loggers/logger-truck-07/invalid-periods
curl -X PATCH http://localhost:8000/invalid-periods/1 \
  -H 'Content-Type: application/json' -d '{"end_time": "2026-09-20T09:45:00Z"}'
curl -X DELETE http://localhost:8000/invalid-periods/1
```

## 时间处理

- 输入接受任意 ISO 8601 时间；带时区的统一换算为 UTC，**无时区时间按 UTC 处理**。
- 内部以 UTC ISO 8601 字符串存储，字典序即时间序，迟到数据只需插入即可参与正确重算。

## 配置项

| 环境变量 | 默认值 | 说明 |
 | --- | --- | --- |
| `DB_PATH` | 本地 `data/freeze_thaw.db`；镜像内 `/data/freeze_thaw.db` | SQLite 文件路径，启动自动建库 |

样本级配置（创建时指定，可 PATCH 修改）：`freeze_threshold_c`、`thaw_threshold_c`、
`min_allowed_c`、`max_allowed_c`、`max_freeze_thaw_cycles`、
`cold_dose_budget_c_min`、`heat_dose_budget_c_min`、
`max_integration_interval_minutes`。

暴露剂量相关字段：

| 字段 | 单位 | 说明 |
| --- | --- | --- |
| `cold_dose_budget_c_min` | °C·min | 低温累计剂量预算，首次跨越产生 `exposure_budget_exceeded`（需同时配置 `min_allowed_c`） |
| `heat_dose_budget_c_min` | °C·min | 高温累计剂量预算（需同时配置 `max_allowed_c`） |
| `max_integration_interval_minutes` | 分钟 | 相邻读数最大可积分间隔，超过则跳过该区间并计入缺口 |
| `cold_dose_c_min` / `heat_dose_c_min` | °C·min | 状态接口返回的双向累计剂量（派生值） |
| `exposure_gap_minutes` | 分钟 | 因间隔超过上限而跳过的缺口总时长（派生值） |
| `cold_budget_usage` / `heat_budget_usage` | 比值 | 预算使用率 = 剂量 / 预算，`>= 1` 表示已超限；未配置预算为 `null` |

### 数据库兼容迁移

容器启动（或本地 `init_db`）时会先幂等建表，再检测旧库缺失的列并以 `ALTER TABLE`
补齐（暴露剂量配置/缓存列、告警方向与剂量列、告警的记录仪读数引用列
`alerts.logger_reading_id`），并补建共享记录仪相关表
（`loggers` / `logger_readings` / `logger_bindings` / `reading_attributions` /
`logger_invalid_periods`）。
旧版本的 SQLite 数据文件无需手工处理即可平滑升级，历史样本与读数保留，
首次重算后剂量缓存自动填充。

## 测试

```bash
pytest -q
```

覆盖内容：阈值边界与滞回、死区防抖、不完整冻结不计循环、温度越限去抖告警、
循环超限告警、迟到数据重排、`event_id`/签名双重幂等、告警确认在重算后保留、404 处理，
以及暴露剂量的线性插值切分、双向累计、缺口跳过、预算跨越插值时刻告警、稳定去重与
配置变更可逆重算、旧版数据库 ALTER TABLE 迁移；共享记录仪部分覆盖登记/读数幂等、
左闭右开窗口与同一样本时段重叠拒绝、一读多样本归属、与直接读数合并重算冻融/剂量、
迟到读数与窗口 PATCH/DELETE 的归属撤销与无重复重建、直接读数不被改动、
记录仪来源告警的去重与确认保留、样本/记录仪删除级联；一致性核查部分覆盖
不交叉一对一配对的三级目标取舍（配对数最多、总时间差最小、读数 ID 序列稳定）、
时间差边界与零窗口、温差严格超限判定、两侧未配对计数、迟到读数与绑定调整后的
最新归属反映、核查只读不改写既有结果、参数校验（负值 422、缺失 422、不存在 404）；
校准无效时段部分覆盖时段增删改查与重叠/倒挂/端点相接校验、时段内读数照常幂等入库
但不归属样本且不参与冻融/剂量/告警/一致性核查、迟到读数与时段 PATCH/DELETE 后的
归属对账与恢复、直接读数与既有告警确认状态保留、逐记录仪隔离与删除级联。
