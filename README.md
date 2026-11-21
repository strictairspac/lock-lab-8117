# 实验样本流转并发台

青屿检测样本中心（24 人）的样本移交复核工作台。原生 HTML/CSS/JavaScript 前端，Python FastAPI 后端，真实 PostgreSQL 两连接事务与行锁演练。

## 场景和模块

团队原先通过纸质交接单、SQL 截图和 Excel 交接。三处痛点：样本版本难统一、并发移交容易重复、超时后无法确认回执是否回滚。上游为样本接收记录，本站承接样本登记、移交申请、并发复核和证据导出，下游为理化检测室与复核员。

- 样本台账：批次、可用份数、保管位置，可录入。
- 流转申请：关联样本、指定数量和接收实验室。
- 并发执行：Redis 队列、独立 Python worker、可设锁等待上限。
- 冲突证据：Backend PID、SQLSTATE、回执回滚、数量守恒与 JSON 导出。
- 管理员审计：登录、写入和执行动作。

## 启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18242 ，后端 http://localhost:19242/docs ，数据库 localhost:17742。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@qingyu.test|Demo2026!|全业务写入、执行、审计|
|operator|operator@qingyu.test|Demo2026!|创建样本、申请、执行、导出|
|viewer|viewer@qingyu.test|Demo2026!|只读、导出；写 API 返回403|

## 一天剧本

09:00 operator 登录；09:05 复核 `QY-261117-A` 近海水质留样 120 份，位置冷藏间 A-03；09:10 查看申请 `MOVE-1117-01`，接收理化检测室 30 份；09:15 新建 `shift-20261117` 流转申请；09:20 以该申请发起 250ms 并发演练；09:25 查看 A 提交 30、B SQLSTATE 55P03 回滚，90+30=120；09:30 导出 `trial-1.json`；09:40 viewer 复核证据；10:00 admin 查看审计。

## 算法与业务边界

这是**真实数据库行锁的受控演练**。每次以样本可用量建立独立 `trial_balances` 镜像行，不扣减原台账。A 使用 `SELECT FOR UPDATE` 持锁并写未提交回执；Python threading.Event 确保 B 在 A 持锁期间运行。B 先写自己的回执，再以指定 `lock_timeout` 请求同一行锁；PostgreSQL 返回 SQLSTATE 55P03 后完整 ROLLBACK。B 结束事件释放 A，A 扣减 remaining、增加 received 并 COMMIT。没有随机结果、sleep 假等待或假 SQL 计划。

输入：流转申请、50–2000ms 锁等待上限。输出：实际 PID、错误码、等待耗时、剩余量、接收量、回执数和 SHA256。口径为 `remaining + received == initial`、remaining非负、仅A回执一条；B回执必须为零。等待耗时受主机负载影响，SHA包含PID和实际耗时因此每次不同。算法输入和最终不变量可复现。失败态包括申请量超限、数据库错误、线程协调超时、队列不可用；页面显示错误并允许创建新演练。

单 worker 顺序运行任务，无生产重试/崩溃恢复调度；worker中断时 running 任务需要人工重建，不能用作生产样本库存扣减。

## 回执按月迁移

演练回执多了以后，旧回执按月归档到 `receipt_ledger` 的 LIST 分区（`receipt_ledger_YYYY_MM`），分区键取**演练发起月份**（`trials.period`，启动时回填），不看提交时间——跨月才提交的回执仍归原月。

- 发起：管理员在「冲突证据」页点「发起迁移」，状态置为 `migrating`，游标从 0 开始。
- 回填：按 `receipts.id` 游标分页，每页（默认 200 行）在一个事务内写入分表并推进游标；中断后再次发起会从游标续跑。写入是 `(period, trial_id, actor)` 唯一键上的 upsert，重试不会重复入账。
- 核齐：逐行比较旧表与分表的 `(月份, 演练, 经办, 数量)` 多重集合，缺/多都为 0 才允许「核齐后切换」；否则拒绝并记录原因。
- 切换/退回：切换把读取源切到分表；退回把读取源切回旧表。两个动作都在排他 advisory 锁下执行，回执写入持共享锁，因此末批回填撞上并发提交时，切换会等对方提交或回滚：成功的回执不会漏，回滚的不会混入。切换后旧表继续影子写入，退回后新账本期间提交的回执照样查得到。
- 读取：证据页每次只读当前 active 的一张账，不做 UNION；单场演练的回执始终来自同一份完整的账。新回执在新账启用期间写入分表（同时影子写旧表），旧表启用期间直接写旧表。

`GET /api/receipts/migration` 查看状态与对账；`POST /api/receipts/migration/{start,batch,run,switch,return}` 为管理员操作；`run` 会连续回填至核齐。切换、退回与整轮回填会写审计。

## 工程说明

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|4业务页+登录，反向代理 /api/|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、持久化|
|worker|Python3.11/psycopg|真实双连接锁竞争|
|db|PostgreSQL16|业务数据、隔离镜像账本|
|redis|Redis7|持久队列|

目录：`frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`；`backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}`；`backend/scripts/seed_demo.py`；`backend/migrations/001_initial.sql`。
核心实体 Sample、Transfer、Trial 各有模型、schema、服务、路由、API、store与页面；共享 table/badge/frame 组件。共享枚举 roles/run states 位于后端 `src/constants/enums.py`、模型/权限引用和前端 `src/constants/enums.js`、frame/badge；状态枚举 queued/running/completed/failed。两个后端中间件：request_id/errors。回执与迁移状态见 `backend/src/services/receipts.py`：ReceiptLedger 分区表、ReceiptMigration 单行状态、回执写入/分页回填/核齐/切换/退回，前端对应 `pages/evidence.js` 的迁移面板与 `stores/migration.js`。

环境变量见 `.env.example`：COMPOSE_PROJECT_NAME、三端口、DB_PASSWORD、JWT_SECRET、SEED_DEMO、ARTIFACT_DIR（worker 证据输出目录，默认 `/artifacts`）。默认密码仅用于本地演示；JWT8小时有效。seed按自然键仅插入缺失行，不覆盖修改，并补齐 `trials.period` 与迁移状态行。所有5服务有健康检查，backend/worker依赖健康数据库/Redis，frontend依赖backend；db/redis/artifacts使用命名卷。停止及清空本项目数据：`docker compose down -v --remove-orphans`。

本地开发：Python3.11安装 backend/requirements.txt；设置 DATABASE_URL/REDIS_URL 后 `uvicorn src.main:app --reload`（backend目录）；worker `python -m src.worker`；前端用Nginx同源代理后端。前端双字体经Google Fonts加载，离线使用浏览器字体回退；业务不依赖外部字体。

## License
MIT，见 LICENSE。测试脚本、截图与实际结果位于源码外 `../../test-artifacts/gb-4103/`。
