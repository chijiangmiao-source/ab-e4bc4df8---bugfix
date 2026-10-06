# 轨道载荷 A/B 双槽镜像升级 — 断电安全验收台

模拟轨道载荷维护员的启动镜像升级流程。核心安全保证：

- **镜像按设备隔离**：多台设备可同名使用 A/B 槽位，但镜像字节、摘要、已确认版本、
  候选阶段与诊断证据始终只属于创建或升级它的那台设备（blob 以 `(device_id, slot)` 为主键）；
- **任意时点断电都不会引导摘要不符或未确认的候选**；
- **新版本生效后永不回退**（旧槽位标记 `SUPERSEDED`，恢复时永不选择）；
- 恢复时**仅从「镜像属于本设备、清单完整、实测摘要与清单一致且已确认」的槽位中选定唯一
  活动槽位**，并展示逐槽诊断证据与裁决理由；保留的 `CONFIRMED` 状态不能赦免内容不符的镜像；
- 两个页面并发提交不同候选时，**仅一个请求取得当前代次的升级资格**，另一个得到稳定 `409`
  且不改写活动版本。

## 架构

```
backend/          FastAPI 服务
  models.py       槽位/设备/恢复报告领域模型（EMPTY→CANDIDATE→VERIFIED→CONFIRMED / REJECTED / SUPERSEDED）
  versioning.py   点分数字版本比较（候选必须严格更高）
  store.py        SQLite(WAL) 持久化：槽位清单、候选阶段、确认代次、资格令牌、诊断证据、
                  按 (设备, 槽位) 隔离的镜像 BLOB（含历史共享 blob 表的安全迁移）
  service.py      升级编排：代次资格、摘要校验、原子确认切换、断电恢复裁决
  api.py          HTTP API + 托管 web/dist 静态页面
web/              Vite 原生 JS 前端（中文界面，全部操作经真实 API）
tests/            pytest（19 个用例：三种断电、损坏候选、并发裁决、防回退、重开一致、
                  双设备隔离、历史受污染库的安全收敛）
scripts/
  verify.sh       一次性验收：pytest → 构建页面 → 真实 uvicorn → HTTP 冒烟
  make_legacy_db.py 生成历史共享 blob 格式的受污染库（验收迁移与安全拒绝）
  smoke_http.py   断电恢复、并发裁决、双设备隔离与遗留收敛的 HTTP 冒烟（144 条断言）
Dockerfile        运行镜像（多阶段：Node 构建页面 + Python 运行）
Dockerfile.verify 验收镜像（含 Node/Python，compose 中的 verify 服务）
docker-compose.yml
```

### 安全机制要点

| 故障点 | 断电时持久化状态 | 重新上电的裁决 |
| --- | --- | --- |
| 候选写入 `candidate_write` | 只落盘部分字节，槽位 `CANDIDATE`，`written < size` | 诊断 `incomplete_write`，继续引导旧槽 |
| 摘要校验 `digest_check` | 字节写完但校验结论未提交，`actual_digest` 为空 | 诊断 `unverified_candidate`，不升级 |
| 确认切换 `confirm_switch` | 候选仍 `VERIFIED`（未确认），代次不变 | 诊断 `unconfirmed_candidate`，引导旧版本；恢复后仍可再确认 |
| 镜像损坏 | 清单摘要 ≠ 实测摘要，槽位 `REJECTED`，证据保留 | 诊断 `digest_mismatch`，永不引导 |
| 他设备同名槽位写入/升级 | 本设备 blob 按 `(device_id, slot)` 隔离存放，不受影响 | 重新实测本设备自己的字节，摘要一致才具备引导资格 |
| 已确认槽位内容被替换（历史遗留） | 清单摘要 ≠ 实测摘要，`CONFIRMED` 状态保留 | 诊断 `digest_mismatch` 并保留可复核证据，安全拒绝引导；不回退 `SUPERSEDED` 槽位，不触碰他设备槽位 |

所有变更在 SQLite `BEGIN IMMEDIATE` 事务内完成，`COMMIT` 是唯一原子切换点；并发提交由数据库写锁串行化后再做代次资格裁决，因此冲突结果稳定。

### 历史受污染库的迁移

早期版本把镜像 blob 按槽位名全局共享（`PRIMARY KEY (slot)`），第二台设备的同名槽位
会覆盖第一台设备的镜像。当前版本在打开数据库时自动迁移：旧表中的每段字节**只复制给
清单（或已持久化实测值）能为该字节背书的设备**，其余一律丢弃而不猜测归属。由此：

- 健康设备的槽位字节原样保留，另一设备的恢复不会覆盖它；
- 已受影响设备的槽位测得的仍是（或变为缺失的）外来内容，恢复裁决以
  `digest_mismatch` 拒绝引导，清单摘要与实测摘要并列展示、证据追加写保留，可复核；
- 拒绝是收敛的：重复重开得到相同裁决且证据不重复追加，也绝不回退到 `SUPERSEDED` 槽位。

## 快速开始（Docker Compose）

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up -d --build
curl http://localhost:9090/api/health      # 健康响应
# 浏览器打开 http://localhost:9090
```

数据保存在命名卷 `upgrade-data`（容器内 `/data/upgrade.db`），容器/进程重启后槽位、版本、摘要、确认代次均保留。

### 一次性验收服务 verify

```bash
docker compose run --rm verify
```

该服务在 Compose 网络内执行：

1. `pytest` 代码测试；
2. `npm run build` 构建页面；
3. 先用 `scripts/make_legacy_db.py` 生成**历史共享 blob 格式的受污染库**（两台设备同名
   槽位、镜像互相覆盖的已持久化现场），再启动**真实 uvicorn**（启动时自动迁移），
   对三种断电恢复、损坏候选、并发 409 裁决、切换后重开一致性、**双设备同版本不同镜像
   的隔离**、**任一设备升级/断电后另一设备不受影响**、**摘要与活动槽位一致性**以及
   **受影响记录的安全收敛（证据保留 + 拒绝错误引导 + 不回退 + 不触碰他设备）**进行
   HTTP 冒烟；
4. 执行完毕**自行退出**，全部通过退出码为 0，任一失败非 0。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..
DATA_PATH=./data/upgrade.db uvicorn backend.api:app --reload
# 或一键验收（自动建临时库、起服务、冒烟、清理）
bash scripts/verify.sh
```

## 页面操作流

1. **创建双槽设备**：A 槽为当前版本（带镜像摘要，代次 1，已确认），B 槽为空。
2. **提交更高版本候选**：可勾选故障点（候选写入 / 摘要校验时断电）或“损坏镜像字节”。
3. **模拟断电 → 重新打开设备**：查看恢复裁决报告（选定槽位、合格槽位、逐槽诊断、防回退理由）。
4. **确认切换**：原子提交，活动槽切换、代次 +1，旧槽 `SUPERSEDED`；也可在提交前模拟断电。
5. **并发裁决**：两个“页面”以不同 request_id 同时提交不同版本，观察 200/409 车道与活动版本不变；确认获胜候选后断电重开，核对槽位/版本/摘要/代次完全一致。
6. 底部为追加写的持久化诊断证据。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| POST | `/api/devices` | 创建双槽设备（含当前版本与摘要） |
| GET | `/api/devices/{id}` | 设备视图（槽位/代次/资格/最近恢复报告） |
| POST | `/api/devices/{id}/candidate` | 提交候选（`fault_point`、`corrupt`、`request_id`） |
| POST | `/api/devices/{id}/confirm` | 确认切换（`fault_point=confirm_switch` 可注入断电） |
| POST | `/api/devices/{id}/power-off` / `power-on` | 模拟断电 / 重新打开（执行恢复裁决） |
| GET | `/api/devices/{id}/evidence` | 诊断证据与历次恢复报告 |
