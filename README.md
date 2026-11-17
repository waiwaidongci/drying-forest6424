# 槐序干燥时长试算台

RandomForest回归、训练内特征子集交叉验证与留出集MAE。为槐序研究协作组的12位分析与业务人员提供从样本核对、独立worker计算到批准快照交付的完整工作流。

## 真实业务场景

物料初始湿度/厚度/温度→干燥时长估计→工艺试验备忘；解释用留出置换重要性，禁止用留出集选参。团队原先将 `drying-forest-202611-01.csv` 复制到多份人工表格，复核时再逐项对照。三个痛点是口径不可追溯、重复计算易错、业务说明与结果脱节。上游提供合成业务观察，系统承担研究和复核，下游业务人员读取批准快照。全部演示数据是固定随机种子生成的模拟数据。

## 业务模块

1. 业务总览：批次、样本、任务与批准交付账册。
2. 干燥研究：创建/修改来源说明、查看样本、用真实业务字段追加记录；列表分页。
3. 分析配置：冻结来源、参数和全部样本，进入Redis队列。
4. 设备采样：设备日志流、试件工序窗、报齐线、读数补报与封窗批次；时间加权、隔离与生效依据。
5. 完整结果：真实算法摘要、专属诊断、全部CSV导出和可恢复失败态。
6. 工艺试验备忘：分析师草案→负责人批准→阅读者下载批准快照。
7. 审计：登录、来源修改、设备读数/冲突/批次、任务worker完成、业务说明批准等事件。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 可重复执行，不覆盖人工修改
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18248)；[API文档](http://localhost:19248/docs)。数据库仅演示用途映射 `17748`。首次准备依赖和初始化数据库需要等待健康状态：`docker compose ps`，五个服务均需healthy。

| 组件 | 技术与职责 |
|---|---|
| frontend | 原生 HTML / CSS / JS ES Modules，Nginx代理 `/api/` |
| backend | Python3.11 / FastAPI / SQLAlchemy / JWT |
| worker | 独立Python进程，Redis BLPOP队列，持久化结果/CSV/心跳 |
| db | PostgreSQL16，命名卷持久化 |
| redis | Redis7，AOF命名卷 |

恰好五服务，均配置healthcheck；后端/worker依赖数据库与Redis健康，前端依赖后端健康；worker与后端共享 `artifacts` 命名卷。重置演示环境：`docker compose down -v --remove-orphans`，会删除本项目卷。

## 账号与权限

所有演示账号密码：`Research2026!`。

| 邮箱 | 角色 | 权限 |
|---|---|---|
| admin@drying-forest.demo | admin | 全部录入/执行、批准业务说明 |
| analyst@drying-forest.demo | analyst | 录入来源/样本、执行、提交说明，无批准权 |
| viewer@drying-forest.demo | viewer | 只读、导出结果及已批准快照 |

时间在数据库/API中以UTC存储，总览与审计页面明确转换为Asia/Shanghai北京时间。

JWT有效期8小时；PBKDF2盐化哈希保存密码；角色每请求从数据库读取。访客写操作403，匿名访问业务数据401，重复样本409，非法业务字段422，重复批准409。每客户端/路径写请求90次/分钟，超限429。生产使用需修改JWT及演示数据库凭据。

## 设备日志采样与样本生成

手录样本与设备日志并存：`drying_samples` 仍是人工录入的原始样本，设备样本只存在于已封批次 `generated_samples`。研究任务可选手录口径或某个完整封窗批次；手录来源不受设备状态影响，仍可继续研究。

**工序窗**：每个试件登记 `reference`、工序 `started_at`/`ended_at`（ISO 8601，必须带时区），以及初始湿度、厚度、风量、实测时长；温度由设备日志计算。时间统一转 UTC 保存，页面按北京时间显示。

**时间加权（`LOCF_TIME_WEIGHTED_V1`）**：

- 窗前前值：取工序开始前（含开始时刻）最近一条读数，从工序开始沿用到首个窗内读数。
- 窗内读数按发生时刻覆盖到下一读数，最后一条覆盖到工序结束。
- 工序结束后的读数不补窗内缺口；窗内没有覆盖到结束的读数时，用最后一条窗内读数覆盖到结束。
- 加权温度 = Σ(读数 × 该值覆盖秒数) ÷ 工序窗总秒数。逐点平均会把采样疏密当成权重，因此不使用逐点平均。

**隔离**（不进入研究，批次中保留行与依据）：

- `no_prior_reading`：窗前无值；工序开始前没有任何可用读数，不能向前取值。
- `gap_exceeded`：断采超限；窗内最大覆盖间隔超过设备的 `gap_limit_seconds`（默认 300 秒）。

**报齐与封窗**：

- 设备用 `reported_through` 确认“已报齐至某时刻”，只能前进。任何工序结束晚于报齐线的试件都显示“待报齐”，且不允许封窗。
- 同刻异值（同一设备同一时刻两个不同温度）进入待裁定并阻止封窗；裁定值必须来自候选值。相同值的重复上报按幂等处理。
- 批次在创建时固定工序窗集合；封窗时整批重新计算并在一个事务里写入全部样本。封窗后新登记的工序窗属于下一批次。
- 封窗后收到更早的迟到值（落在已封窗内、或晚于已封窗前值）只写入 `sample_disputes` 争议记录，不改动已封样本；早于已封前值且不改变任何窗口的迟到值也留设备级争议痕迹。
- 封窗接口需幂等键；同一键重试返回同一批次、不重复落样本。写入中断的批次状态为 `writing`，可用 `POST /batches/{id}/resume` 恢复；研究任务只接受 `sealed` 且行数与工序窗数一致的完整批次。

**设备接口**（写入需 admin/analyst，读取需登录）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/devices` | 设备日志流列表 / 登记 |
| GET | `/devices/{id}` | 工序窗预览、隔离依据、冲突、争议、批次 |
| POST | `/devices/{id}/windows` | 登记试件工序窗 |
| POST | `/devices/{id}/readings` | 批量接收读数，可乱序补报 |
| POST | `/devices/{id}/watermark` | 推进报齐线 |
| POST | `/devices/{id}/conflicts/{cid}/resolve` | 裁定同刻异值 |
| POST | `/devices/{id}/disputes/{sid}/resolve` | 复核迟到争议 |
| POST | `/devices/{id}/seal` | 封窗，生成整批样本 |
| GET | `/batches`、`/batches/{id}` | 批次列表 / 明细 |
| POST | `/batches/{id}/resume` | 恢复未完成批次 |

`POST /runs` 增加可选 `batch_id`；带该字段时只冻结批次中的生效样本，并把隔离清单一起写入任务快照，结果页可展开查看。

## 实体与字段

DryingStudy / DryingSample / RegressionRun / ProcessMemo 是核心实体；另有User和Audit。设备采样层还包括Device、SpecimenWindow、TemperatureReading、ReadingConflict、MaterializationBatch、GeneratedSample、SampleDispute。数据库实际表：`drying_studies` → `drying_samples` → `regression_runs` → `process_memos`，以及 `device_streams` → `specimen_windows` → `temperature_readings`、`sample_batches` → `generated_samples`、`reading_conflicts`、`sample_disputes`。样本以固定字段JSON保存，后端按以下字段严格验证，不接受未知字段替代必填值：初始湿度 %（`moisture`）、厚度 mm（`thickness`）、设定温度 ℃（`temperature`）、风量 m³/min（`airflow`）、实测时长 min（`minutes`）。

实体贯穿 `backend/src/models`、`schemas`、`services`、`controllers`、`routes`，以及 `frontend/src/api/research.js`、`stores/research.js` 和 `pages`。`Source.key` 与 `(Record.source_id, reference)` 为自然键；来源改名不会修改已冻结任务输入。业务说明在创建时保存结果快照，批准后从该快照导出。

## 算法与口径

RandomForest回归、训练内特征子集交叉验证与留出集MAE，全部计算位于 `backend/src/services/algorithm.py`，前端不拟合模型、不生成模拟结果。输入是冻结业务观察及参数，输出summary、专属诊断、逐行计算明细、seed与输入SHA256。CSV包含全部结果行；页面仅预览前30行。参数：

- `trees`：森林树数，默认 `60`。
- `depth`：最大深度，默认 `6`。

先固定留出集，特征选择与交叉验证仅在训练行完成；留出集只作最后一次评估。置换重要性是模型依赖度，不是因果贡献。

失败态：业务字段缺失/非有限数值、参数越界、样本不足、退化特征或结构错误均明确拒绝或将任务标failed，错误消息保存在任务中。可回到来源或配置修正后重新执行，历史失败记录保留。

已知边界：只比较三个预定义特征子集，超参数由分析前固定；留出集不用于选择。置换重要性来自最后留出集，不可反复依据它改模再宣称独立验证。不含预测区间，不用于外推工艺。

## 演示数据与一天剧本

固定seed=42，3个来源 ` HX-202611-01/02/03`，每来源240条，共720条业务记录；三个账号和首轮种子任务。另有设备日志流 `DEV-HX-01`：49 个工序窗（48 个已封窗、1 个待报齐）、约 440 条读数、1 个已封批次（46 生效 / 2 隔离）、1 条迟到争议。设备演示与手录数据并存，重复seed不会重复插入。自然键检查后仅插入缺失数据，连跑seed不重复、不覆盖用户修改。`SEED_DEMO=true` 时启动自动执行；失败记录日志但不阻塞服务。首轮任务同样由独立worker真实计算。

1. 09:00 负责人查看三份来源和720条样本。
2. 09:10 分析师选择 `HX-202611-01 秋季常规组`，核对 `drying-forest-202611-01.csv`。
3. 09:15 在“干燥研究”核对初始湿度 %（`moisture`）、厚度 mm（`thickness`）、设定温度 ℃（`temperature`）、风量 m³/min（`airflow`）、实测时长 min（`minutes`），追加编号 `HX-NEW-001` 的样本。
4. 09:17 打开“设备采样”，看到待报齐、隔离与迟到争议各自的依据；同刻异值未裁定时封窗按钮会返回阻止原因。
5. 09:20 新建“十一月复核研究”，样本口径选“设备封窗批次 #1”，按默认参数冻结46条生效样本（隔离2条随快照留档）。
6. 09:21 独立worker消费Redis队列，返回实算结果与输入校验和。
7. 09:25 分析师查看专属诊断及逐行结果，并下载CSV复算；结果页可展开本批隔离依据。
8. 09:30 提交“十一月业务复核意见”，负责人核对并批准。
9. 09:40 viewer下载批准快照，查看审计中的worker完成与批准事件。

## 目录与本地开发

```text
frontend/src/api, stores, types, components/common, hooks, pages, router, utils, constants
backend/src/routes, controllers, services, models, schemas, middlewares, utils, constants, config
backend/scripts/seed_demo.py
backend/migrations/001_initial.sql
```

本地后端：Python3.11虚拟环境安装 `backend/requirements.txt`，设置 `DATABASE_URL`、`REDIS_URL`、`ARTIFACT_DIR`，在backend目录运行 `uvicorn src.main:app --reload`；另开终端运行 `python -m src.worker`。原生前端无需编译，推荐以Compose Nginx访问，保证 `/api/` 代理。前端为Google Fonts双字体、oklch色板，离线字体回退serif；1280/768/390响应布局。

| 环境变量 | 默认与说明 |
|---|---|
| COMPOSE_PROJECT_NAME | `drying-forest`，隔离容器/网络/命名卷 |
| FRONTEND_PORT / BACKEND_PORT / DB_PORT | `18248 / 19248 / 17748` |
| SEED_DEMO | `true` |
| JWT_SECRET | `.env.example`演示值，部署前修改 |
| DATABASE_URL / REDIS_URL | Compose内部连接，定义在compose的env锚点 |
| ARTIFACT_DIR | `/artifacts`，后端worker共享 |
| — | 设备断采上限为设备字段 `gap_limit_seconds`，默认 300 秒，按设备配置 |

共享枚举：`backend/src/constants/enums.py` 与 `frontend/src/constants/enums.js` 都定义 admin/analyst/viewer、queued/running/completed/failed、draft/approved。后端service检查对应字符串；README为全量值说明。共享前端组件table/status/chart/business跨总览、结果、配置与复核复用；actions hook和format utils负责交互/格式。后端errors与rate_limit是两个中间件。

测试及截图在批根 `test-artifacts/gb-0044/`，不混入源码。独立算法oracle、HTTP完整流程和真实Playwright浏览器流程见该目录 `TESTING.md`；设备采样的无外部依赖验收脚本为 `verify_device_sampling.py`。License：MIT。
