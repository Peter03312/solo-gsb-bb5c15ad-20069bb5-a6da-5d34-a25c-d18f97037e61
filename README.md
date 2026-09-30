# 冷链货物追责服务

面向冷链货物追责的后端服务：录入货物与测温 / 交接事件，**按发生时间重建保管链**，自动识别越温区间并在交接时刻拆分责任。技术栈 **Python 3.11 + FastAPI + SQLAlchemy 2 + PostgreSQL**。

## 一、一键启动（Docker Compose，含数据库初始化）

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
| --- | --- |
| API 服务 | http://localhost:8000 |
| 交互式接口文档 Swagger UI | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/库/密码默认均为 `coldchain`） |

首次启动时，`db/init.sql` 由 PostgreSQL 官方镜像自动执行（建枚举类型、表、CHECK 约束、部分唯一索引）。应用启动时也会执行 `CREATE TABLE IF NOT EXISTS` 作为兜底。API 容器通过 `depends_on: service_healthy` 等待数据库就绪。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据库数据卷（下次重新初始化）
```

## 二、本地开发（不走 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 需要一个可连接的 PostgreSQL，并先执行 db/init.sql
export DATABASE_URL="postgresql+psycopg2://coldchain:coldchain@localhost:5432/coldchain"
uvicorn app.main:app --reload --port 8000
```

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://coldchain:coldchain@db:5432/coldchain` | SQLAlchemy 数据库连接串（容器内主机名为 `db`） |
| `POSTGRES_USER` | `coldchain` | 数据库用户（compose） |
| `POSTGRES_PASSWORD` | `coldchain` | 数据库密码（compose） |
| `POSTGRES_DB` | `coldchain` | 数据库名（compose） |
| `API_PORT` | `8000` | 宿主机映射的 API 端口 |
| `DB_PORT` | `5432` | 宿主机映射的数据库端口 |

也可在项目根目录放 `.env` 文件供 compose 读取。时间字段统一按 UTC 存储；请求时间建议带时区偏移（如 `+08:00`），无时区时间按 UTC 解释。

## 四、HTTP API

基址：`http://localhost:8000`。所有请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/cargo` | 创建货物 |
| `GET` | `/cargo` | 货物列表 |
| `GET` | `/cargo/{cargo_id}` | 货物详情 |
| `POST` | `/events` | 写入测温 / 交接事件 |
| `GET` | `/cargo/{cargo_id}/events` | 按发生时间列出事件 |
| `GET` | `/cargo/{cargo_id}/report` | **追责报告**：越温区间 + 责任片段 |
| `GET` | `/cargo/{cargo_id}/confirmations` | **责任确认视图**：全部已闭合区间（快照/最新表态/汇总） |
| `GET` | `/cargo/{cargo_id}/confirmations/{interval_id}` | 按开启测温事件ID定位单条已闭合区间 |
| `POST` | `/cargo/{cargo_id}/stances` | 对已闭合区间提交确认 / 异议 |
| `GET` | `/cargo/{cargo_id}/stances/history` | 该货物全部表态历史（含已失效表态） |
| `GET` | `/cargo/{cargo_id}/stances/history/{interval_id}` | 指定区间的表态历史（区间消失也可查） |
| `GET` | `/health` | 健康检查 |

### 1. 创建货物 `POST /cargo`

```json
{
  "cargo_id": "COLD-001",
  "started_at": "2026-09-30T08:00:00+08:00",
  "temp_upper_limit": "4.0",
  "initial_holder": "北京冷库"
}
```

字段：货物编号、起始时间、温度上限（℃）、起始保管方。编号重复返回 `409 cargo_exists`。

### 2. 写入事件 `POST /events`

两种事件由请求字段区分（互斥，不能同时出现）：

测温（含 `temperature`）：

```json
{ "event_id": "evt-1", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T10:00:00+08:00", "temperature": "8.2" }
```

交接（含 `giver` 与 `receiver`）：

```json
{ "event_id": "evt-2", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T12:00:00+08:00",
  "giver": "北京冷库", "receiver": "上海冷库" }
```

每个事件都带 **事件ID、货物编号、发生时间**。

### 3. 追责报告 `GET /cargo/{cargo_id}/report`

```json
{
  "cargo_id": "COLD-001",
  "temp_upper_limit": "4.000",
  "current_holder": "广州冷库",
  "excursions": [
    {
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "is_open": false,
      "segments": [
        { "start": "2026-09-30T02:00:00Z", "end": "2026-09-30T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "2026-09-30T04:00:00Z", "end": "2026-09-30T06:00:00Z", "responsible_party": "上海冷库" },
        { "start": "2026-09-30T06:00:00Z", "end": "2026-09-30T08:00:00Z", "responsible_party": "广州冷库" }
      ]
    }
  ]
}
```

未闭合区间：`end` 为 `null`、`is_open` 为 `true`，最后一个责任片段的 `end` 也是 `null`（未结束）。区间与片段均按时间升序返回。

### 4. 责任确认视图 `GET /cargo/{cargo_id}/confirmations[/{interval_id}]`

围绕**已闭合**越温区间开展责任确认。区间以「**开启测温事件ID**」定位（即 `interval_id`）。响应给出区间起止测温事件ID、责任片段、**当前快照标识**、各责任方的**最新有效表态**与**汇总状态**：

```json
{
  "cargo_id": "COLD-001",
  "interval_id": "evt-temp-1",
  "start_event_id": "evt-temp-1",
  "end_event_id": "evt-temp-2",
  "start": "2026-09-30T02:00:00Z",
  "end": "2026-09-30T08:00:00Z",
  "snapshot_id": "snap-757d7f8489196ca968d225e219ea58a1fb15fce39ec34d339222bd4034d774f7",
  "segments": [
    { "start": "2026-09-30T02:00:00Z", "end": "2026-09-30T04:00:00Z", "responsible_party": "北京冷库" },
    { "start": "2026-09-30T04:00:00Z", "end": "2026-09-30T08:00:00Z", "responsible_party": "上海冷库" }
  ],
  "party_stances": [
    { "responsible_party": "北京冷库", "stance": "confirm", "reason": null,
      "stance_id": "st-1", "created_at": "2026-09-30T09:00:00Z" },
    { "responsible_party": "上海冷库", "stance": "object", "reason": "测温探头存疑",
      "stance_id": "st-2", "created_at": "2026-09-30T09:05:00Z" }
  ],
  "summary_status": "disputed"
}
```

- **快照标识 `snapshot_id`**：`snap-` + 对（区间起止测温事件、各责任片段的起止与责任方）规范化内容计算的 SHA-256。边界或责任片段变化则快照变化；仅起止与片段完全不变时快照稳定。
- **最新有效表态 `party_stances`**：只统计**绑定当前快照**、且责任方仍在当前片段中的各责任方最新一条表态（改口后取新表态）；无有效表态的责任方不出现。表态历史另见 `/stances/history`。
- **汇总状态 `summary_status`**：
  - `all_confirmed` 全确认：当前片段中每个责任方的最新有效表态都是确认；
  - `disputed` 有异议：存在最新有效表态为异议（即使另有责任方尚未表态）；
  - `pending` 待确认：无异议但尚有责任方没有有效表态。
- 列表接口只返回**已闭合**区间（开放区间不可确认）；单查区间仍开放 → `409 interval_open`，区间不存在（或晚到重建后边界已变、区间被并入更早区间）→ `404 interval_not_found`。

### 5. 提交表态 `POST /cargo/{cargo_id}/stances`

```json
{
  "stance_id": "st-1",
  "cargo_id": "COLD-001",
  "interval_id": "evt-temp-1",
  "snapshot_id": "snap-757d7f84...",
  "responsible_party": "上海冷库",
  "stance": "object",
  "reason": "测温探头存疑，申请复核"
}
```

字段：表态编号、货物编号、区间标识（开启测温事件ID）、当前快照标识、责任方、`confirm`/`object`、异议理由（**异议必填非空，确认必须为空**）。责任方必须出现在区间当前责任片段中。

- 成功 `201`，响应即表态记录（含 `is_current`：是否对当前快照有效）。
- **幂等**：同一表态编号同内容重传 → `200 {"replayed": true, ...}`，返回原记录且不重复写入——**即使该表态绑定的快照已经失效**；同一编号异内容 → `409 stance_id_conflict`，原记录不变。
- **改口**：用**新表态编号**提交新表态；历次表态全部保留，汇总只取各责任方最新有效一条。
- **拒绝且不落库**：过期快照 `409 snapshot_stale`、区间不存在 `404 interval_not_found`、区间仍开放 `409 interval_open`、责任方不在当前片段 `422 party_not_in_segment`、理由形状不符 `422 bad_stance_shape`。
- 提交在事务中锁定货物行，与同货物的事件写入**共用同一把行锁串行化**，校验所用快照即提交瞬间的最新重建结果。

### 6. 表态历史 `GET /cargo/{cargo_id}/stances/history[/{interval_id}]`

返回历次表态（含改口前的、以及晚到重建后已失效的表态），每条带 `is_current` 与绑定的 `snapshot_id`。区间在当前重建结果中已不存在时，历史仍可查（其 `is_current` 恒为 `false`）。

## 五、业务规则

1. **时间合法性**：事件发生时间早于货物起始时间 → `422 before_start`，拒绝。
2. **越温区间**：
   - 测温 **高于** 温度上限（`temp > limit`，等于上限不算越温）开启区间；
   - 区间内首次 **不超温**（`temp <= limit`）的测温在其测温时刻闭合区间；
   - 区间内重复超限测温不另开区间；区间外的不超限测温无影响。
3. **责任拆分**：区间跨越交接时，按交接时刻切分——`[区间起, 交接时刻)` 归交出方，**交接时刻起归接收方**；不产生零长度片段。
4. **晚到记录**：事件允许乱序写入。每次写入都把候选事件并入既有事件，按发生时间整体重算受影响区间；因此交接、测温可事后补录。
5. **交接合法性（整体拒绝）**：任何一笔交接的交出方必须等于其发生时刻的保管方；晚到插入若导致此前已存在的交接不再满足该条件，本次写入**整体回滚拒绝，既有结果保持不变**（`422 giver_mismatch`）。
6. **同刻冲突**：同一货物同一时刻至多一笔测温、至多一笔交接（测温与交接可同刻发生，且测温先于交接处理），冲突 → `409 same_timestamp_conflict`。
7. **幂等**：同一 `event_id` 重传：内容完全一致（时间按同一时刻归一化比较）返回 `200 {"replayed": true}` 且不重复写入；内容不一致返回 `409 event_id_conflict`，原记录不变。
8. **并发**：同一货物的写入在事务中以 `SELECT ... FOR UPDATE` 锁定货物行串行化，并由部分唯一索引 / CHECK 约束在数据库层兜底。
9. **责任确认（仅已闭合区间）**：
   - 区间以开启测温事件ID定位；快照标识由「区间起止 + 责任片段」的 SHA-256 决定；
   - 表态绑定提交时的快照。晚到事件重建后，**边界或责任片段变化**的旧表态失效（历史仍可查）；**未变化**的继续有效；
   - 新表态提交时以当前快照校验，并与同货物事件写入共用货物行锁串行化；过期快照、区间不存在、区间仍开放一律拒绝且不落库；
   - 表态编号同内容重传返回原记录（即使快照已失效），异内容冲突；改口用新编号，历次表态保留。

错误响应统一格式：`{"error": {"code": "...", "message": "..."}}`。

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cargo_not_found` / `interval_not_found` | 货物或已闭合区间不存在（含重建后消失/边界变化） |
| 409 | `cargo_exists` / `event_id_conflict` / `same_timestamp_conflict` / `integrity_conflict` / `interval_open` / `snapshot_stale` / `stance_id_conflict` | 编号或同刻冲突、区间仍开放、快照过期、表态编号冲突 |
| 422 | `before_start` / `giver_mismatch` / `bad_event_shape` / `bad_stance_shape` / `party_not_in_segment` | 业务校验失败（整体拒绝） |
| 200 | 响应体 `replayed=true` | 同 ID 同内容幂等重传（事件或表态） |

## 六、调用示例

```bash
# curl 脚本（演示完整链路 + 幂等 + 报告）
bash examples/curl_examples.sh

# Python 示例（httpx）
python examples/python_client.py
```

或直接打开 http://localhost:8000/docs 使用 Swagger UI 调用。

## 七、测试

```bash
pip install pytest httpx

pytest -q                                          # 50 个用例（内存库），并发用例自动跳过
DATABASE_URL="sqlite:////tmp/cc.db" \
  pytest tests/test_concurrency.py -q              # 并发写入串行化用例（需文件型库）
```

测试覆盖：越温开闭、跨交接责任拆分、同刻测温+交接、未闭合区间、晚到重算、交出方整体拒绝、同刻冲突、幂等重传、并发写入串行化，以及责任确认的快照与汇总状态、确认/异议、改口留痕、晚到事件导致表态失效/继续有效、区间消失后历史可查、提交拒绝不落库等。

## 八、目录结构

```
.
├── app/
│   ├── main.py           # FastAPI 路由与异常映射
│   ├── crud.py           # 事件事务写入、幂等、整链校验
│   ├── confirmations.py  # 已闭合区间责任确认：快照视图 / 表态提交 / 历史
│   ├── chain.py          # 保管链重建 / 越温责任拆分 / 快照标识（纯函数）
│   ├── models.py         # SQLAlchemy 模型（货物 / 事件 / 责任表态）
│   ├── schemas.py        # Pydantic 请求/响应模型
│   ├── database.py       # 引擎、Session
│   └── timeutil.py       # UTC 归一化
├── db/init.sql           # PostgreSQL 初始化（枚举、表、约束、索引）
├── examples/             # curl / Python 调用示例（含责任确认）
├── tests/                # pytest 单元 + API + 并发 + 责任确认测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
