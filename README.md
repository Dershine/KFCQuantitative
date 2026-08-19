# KFCQuant：A股双时段机会研究与运行管理器

个人使用、可审计的A股研究与影子组合系统。系统在08:30生成盘前观察名单，在14:40全市场重扫尾盘机会，用可验证资讯和公开公告控制风险，并维护10万元模拟组合。运行管理器独立负责版本、健康、更新和回滚。

> 不连接券商，不执行真实交易，不构成投资建议。学习模式使用免费公开数据适配器，不具备商业数据服务的稳定性；网页会明确显示数据来源和降级状态。

## 默认免费学习模式

| 功能 | 默认供应商 | 密钥/费用 |
|---|---|---|
| 历史日线、交易日历、历史ST和交易状态 | BaoStock | 无需密钥，免费 |
| 14:40/14:45实时快照、5分钟行情 | AKShare公开源 | 无需密钥，免费 |
| 公告 | AKShare东方财富公告镜像 | 无需密钥，免费 |
| 公开新闻 | AKShare财联社、新浪 | 无需密钥，免费 |
| 风险抽取、报告 | DeepSeek OpenAI兼容API | 按量付费 |

Tushare仍作为可选供应商保留，但默认运行不要求`TUSHARE_TOKEN`。

公告镜像只提供公告日期而不保证精确发布时间。系统采用保守边界：当日公告不会进入14:40信号，收盘后才以“入场后事件”处理。公告镜像接口不可用时，不生成影子买单。

## 安装

```powershell
Set-Location E:\WebProjects\KFCQuantitative
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
notepad .env
```

`.env`中只需填写DeepSeek Key：

```dotenv
LLM_API_KEY=你的DeepSeek_API_Key

KFCQUANT_DATA_PROFILE=learning
KFCQUANT_MARKET_PROVIDER=baostock
KFCQUANT_LIVE_PROVIDER=akshare
KFCQUANT_NEWS_PROVIDER=akshare

KFCQUANT_LLM_PROVIDER=deepseek
KFCQUANT_LLM_BASE_URL=https://api.deepseek.com
KFCQUANT_LLM_EXTRACT_MODEL=deepseek-v4-flash
KFCQUANT_LLM_REPORT_MODEL=deepseek-v4-pro
```

密钥只保存在本地`.env`，不写入数据库、原始快照或日志。

## 首次运行

### 一键启动（推荐）

双击项目根目录的 `Start-KFCQuant.cmd`。也可以右键选择“以管理员身份运行”，但脚本本身不要求管理员权限。

启动器会把专用虚拟环境、pytest临时文件、缓存和覆盖率文件放到当前Windows账户的：

```text
%LOCALAPPDATA%\KFCQuantitative\<当前用户SID>\
```

该目录不会与Codex沙箱账户共用，因此不会受到沙箱所创建临时目录的权限影响。首次启动或源码变化时会自动安装依赖并运行测试，之后会执行本地配置检查并打开网页。它不会删除或重建`.env`、`data/`、DuckDB、Parquet或报告。

如果`.env`还不存在，启动器会从模板创建并用记事本打开。填写DeepSeek Key并保存后，再次双击启动器即可。

### 1. 检查本地配置

```powershell
.\.venv\Scripts\kfcquant.exe doctor
```

### 2. 检查免费数据源和DeepSeek连接

```powershell
.\.venv\Scripts\kfcquant.exe doctor --online
```

在线检查会读取少量BaoStock/AKShare数据，并用极少Token验证DeepSeek JSON输出。任何一项失败都应先解决，不要注册定时任务。

### 3. 初始化约400个自然日历史数据

```powershell
$endDate = (Get-Date).ToString("yyyy-MM-dd")
$startDate = (Get-Date).AddDays(-400).ToString("yyyy-MM-dd")
.\.venv\Scripts\kfcquant.exe sync-eod --start $startDate --end $endDate
```

BaoStock按股票读取整个历史区间并分批写入DuckDB和Parquet。首次全市场初始化会明显慢于付费批量接口；中断后可以重复执行，写入保持幂等。

成功输出应满足：

- `trading_days`大于120；
- `bars`大于0；
- `securities`大于0。

每个新持久化的证券、交易日历、日线或实时报价批次都会生成不可覆盖的Parquet快照，并在DuckDB的`ingestion_manifests`中记录实际Provider、采集时间、Schema版本、文件SHA-256、行数和质量报告。业务行与清单在同一数据库事务写入；旧快照保持原样，不会在升级时改写或删除。

每个新Published Signal Run还会通过Point-in-time Data Gateway复核证券、日线、实时报价、风险事件和前序信号的截止时间，并把实际送入Strategy的精确DataFrame保存为`data/raw/run-inputs/`下的内容寻址Parquet；相同内容自动复用。DuckDB的`run_manifests`会原子记录源码SHA及dirty状态、项目/Python/依赖锁版本、Strategy参数、输入快照和上游采集批次、候选结果Hash。截止时间后的数据会使运行失败关闭，不会产生Published Run或模拟买单。

风险资讯抽取还会记录版本化Prompt、Prompt/Input/Response SHA-256、请求与实际模型、耗时和失败类型；调用记录不保存API Key或原文Prompt。文档和风险事件通过显式关系关联一个或多个证券，并保留Provider/确定性名称匹配来源和相关度；旧版单证券记录继续兼容。这样可从Run的风险事件输入快照继续定位到对应LLM抽取版本，失败抽取仍会保持受影响证券不可交易。

### 4. 启动网页

```powershell
.\.venv\Scripts\kfcquant.exe serve
```

默认地址为`http://localhost:8501`。网页包含今日候选、候选详情、影子组合、交易记录、前向评估、数据健康、运行日志和收盘报告。

### 5. 做一次安全研究运行

在交易日执行：

```powershell
.\.venv\Scripts\kfcquant.exe run-preclose --research-only
```

早间观察名单可以独立验证：

```powershell
.\.venv\Scripts\kfcquant.exe run-morning --research-only
```

08:30结果只用于观察，不创建影子订单；14:40仍会扫描全市场。

窗口外研究绝不创建模拟订单。正式影子订单只能在交易日14:35至14:43生成，14:43至14:50捕获模拟成交。

### 6. 预热公告并测试报告

```powershell
.\.venv\Scripts\kfcquant.exe run-postclose
```

这会同步最近公告、处理风险事件并验证DeepSeek报告。报告保存在`reports/`。

## 定时任务

确认数据已初始化、`doctor --online`全部通过且研究运行结果合理后：

```powershell
.\scripts\register_tasks.ps1
```

注册脚本会先读取应用的类型化调度配置并执行完整校验，再创建Windows计划任务；Linux Scheduler使用同一个配置源。非法的时间顺序、窗口或选股上限会在启动/注册前被拒绝，不会以部分配置继续运行。

每个研究任务都会持有默认15分钟的数据库租约，并在关键处理阶段续租。同名任务的有效租约会拒绝竞争启动；worker异常退出后，Scheduler下次启动会把过期任务标记为失败并允许安全重跑。Operations只让有效租约阻止部署，无法验证租约Schema或缺少租约的旧任务仍按失败关闭处理。可通过`KFCQUANT_JOB_LEASE_SECONDS`调整租约，但不应低于单次Provider阻塞调用的最大预期时间。

CLI、Scheduler和既有应用日志使用单行JSON输出，并统一携带可用的`job_run_id`、`signal_run_id`、Strategy身份、Provider、阶段、源码SHA和information cutoff。Bearer Token、密钥字段及URL中的敏感查询参数会在输出前脱敏。运行指标与告警审计分别追加到`runtime/observability-metrics.jsonl`和`runtime/observability-alerts.jsonl`；它们不进入研究数据库，也不参与订单事务。

系统会记录Job耗时/终态、Provider耗时/失败、Quote年龄、EOD滞后、官方资讯积压、LLM抽取失败、候选数、拒单、数据库锁等待和worker心跳年龄。14:40任务失败、官方资讯异常、锁超时和worker心跳异常会生成带冷却去重的告警。默认只保留本地审计；如需主动通知，可配置通用JSON Webhook：

```dotenv
KFCQUANT_ALERT_WEBHOOK_URL=https://你的告警接收端/kfcquant
KFCQUANT_ALERT_WEBHOOK_BEARER_TOKEN=可选Bearer令牌
KFCQUANT_ALERT_COOLDOWN_SECONDS=900
KFCQUANT_WORKER_HEARTBEAT_STALE_SECONDS=180
KFCQUANT_OFFICIAL_NEWS_BACKLOG_THRESHOLD=100
```

Webhook投递失败不会改变Research Run、订单或成交结果，但会写入结构化`alert_delivery_failed`事件。worker进程无法在自身完全停止后继续发信，因此生产环境仍应由Operations健康探针或外部服务定期执行`kfcquant health --json`；该检查会度量心跳年龄并分发已配置告警。

- 交易时段每5分钟：检查已有持仓退出；
- 08:00：确认交易日历；
- 08:30：生成盘前观察名单；
- 14:35：评价早间名单；
- 14:40：生成候选和模拟订单；
- 14:45：模拟成交；
- 18:10：同步BaoStock正式日线（免费源发布时间晚于商业数据）；
- 20:30：同步资讯并生成复盘。

如需调整默认时刻或候选上限，使用嵌套环境变量，并同步调整相关触发点与安全窗口。例如：

```dotenv
KFCQUANT_SCHEDULE__PRECLOSE_RUN_AT=14:40
KFCQUANT_SCHEDULE__PRECLOSE_WINDOW_START=14:35
KFCQUANT_SCHEDULE__PRECLOSE_WINDOW_END=14:43
KFCQUANT_SCHEDULE__FILL_AT=14:45
KFCQUANT_SCHEDULE__FILL_WINDOW_START=14:43
KFCQUANT_SCHEDULE__FILL_WINDOW_END=14:50
KFCQUANT_SELECTION__TOP_N=10
KFCQUANT_SELECTION__CANDIDATE_LIMIT=100
KFCQUANT_SELECTION__MINIMUM_OPPORTUNITY_SCORE=0
```

`MINIMUM_OPPORTUNITY_SCORE`先过滤低于阈值的候选；随后同一选择Policy按“未阻断优先、机会分降序、股票代码升序”确定排名。`TOP_N`同时约束早盘连续性、评估、报告和候选订单，且不能小于最大持仓数；`CANDIDATE_LIMIT`不能小于`TOP_N`。可用 `kfcquant schedule-plan --json`查看校验后的实际注册计划。

本机必须保持开机和联网。错过的窗口记录为`missed`，不会事后伪造成交。网页关闭不影响计划任务。

## 可选Tushare模式

如以后采购Tushare权限，可切换：

```dotenv
TUSHARE_TOKEN=你的Token
KFCQUANT_MARKET_PROVIDER=tushare
KFCQUANT_NEWS_PROVIDER=tushare
```

数据库、评分、组合和网页无需改动。

Tushare日线模式同时依赖`daily`、`adj_factor`、`stk_limit`、`suspend_d`和`stock_st`权限，以便按交易日还原成交单位、复权因子、涨跌停、停牌和历史ST状态。任一安全状态接口不可用或返回数据不满足版本化Schema时，同步会失败关闭，不会把“未知”默认为可交易；切换前应先运行`doctor --online`并完成一次离线/测试库同步验证。

## 研究规则摘要

- 仅沪深主板，排除创业板、科创板、北交所、B股、ST、停牌、退市和不足120个交易日股票；
- 20日成交额中位数至少1亿元；
- 显示机会评分，不伪造上涨概率；
- 量化评分不由大模型修改，正面新闻不加分；
- 模拟组合最多5只、单股20%、100股整数倍；
- 净止盈1.5%、净止损2%、T+1、最多持有5个交易日；
- 实时行情超过60秒或公告源失效时不产生模拟买单。

## 测试

```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\python.exe -m pytest --cov=kfcquant
```

免费数据适合学习和前向影子验证，不适合据此宣称策略已经具备稳定收益。至少连续运行60个交易日后，再评估是否购买专业数据。

## Linux服务器原生部署

生产服务器不要求Docker。标准形态为Git工作区、Python虚拟环境、三个systemd服务和Nginx：

- 研究网页：`https://<服务器IP或域名>/research/`；
- 运行管理器：`https://<服务器IP或域名>/ops/`；
- `kfcquant-worker`是DuckDB唯一写入者；
- `kfcquant-web`以只读模式访问数据库，并且只监听`127.0.0.1:8501`；
- `kfcops`只接受通过`main`工作流验证的40位Git SHA；
- 数据、报告和备份存放在`/var/lib/kfcquant/`，不会被Git更新覆盖。

服务器需要Python 3.12或更高版本。生产依赖固定在`requirements.lock`；GitHub Actions会在Python 3.12上安装锁定依赖、执行Ruff和Pytest，并保存wheel构建产物。

### 首次初始化服务器

在Ubuntu 24.04服务器克隆私有仓库后执行：

```bash
sudo bash deploy/bootstrap_server.sh
```

初始化脚本会：

1. 安装Git、Python、Nginx、Basic Auth和证书工具；
2. 将现有Git仓库安装到`/opt/kfcquant/app`；
3. 创建生产虚拟环境并安装`requirements.lock`；
4. 创建`kfcquant-worker`、`kfcquant-web`和`kfcops`服务；
5. 配置HTTPS、证书续期和受限的服务控制命令。

随后填写并保护两个配置文件：

```text
/etc/kfcquant/research.env  # DeepSeek密钥、供应商和持久化路径
/etc/kfcquant/ops.env       # GitHub只读令牌和运行管理器配置
```

私有仓库还需要给`kfcops`系统用户配置只读Git deploy key，并把`/opt/kfcquant/app`的`origin`设为对应SSH地址。`KFCOPS_GITHUB_TOKEN`只用于读取提交和Actions状态，不会被写进Git remote。

填写配置后执行：

```bash
sudo systemctl restart kfcquant-worker kfcquant-web kfcops
sudo kfcquant-admin doctor --online

end_date=$(date +%F)
start_date=$(date -d '400 days ago' +%F)
sudo kfcquant-admin sync-eod --start "$start_date" --end "$end_date"
```

### 发布更新

不传参数时部署远端`main`的最新提交：

```bash
sudo bash /opt/kfcquant/app/deploy/deploy_server.sh
```

也可以部署明确的已测试版本：

```bash
sudo bash /opt/kfcquant/app/deploy/deploy_server.sh <完整40位commit SHA>
```

发布管理器依次验证GitHub Actions、检查交易窗口和运行中任务、拉取Git提交、停止研究服务、备份DuckDB、安装锁定依赖、迁移、启动并执行健康检查。失败时自动恢复上一提交和部署前数据库备份，默认保留最近7份备份。

Research与Operations配置在进程启动前统一校验。Operations的`KFCOPS_SESSION_SECRET`必须是至少32字符的非默认值；矛盾的保护窗口、非法Provider、无效费用/仓位比例或缺少所选Tushare模式的Token都会使启动失败关闭。

手动回滚会消费当前的回滚点；回滚成功后，下一次正常发布会重新建立新的数据库备份和上一版本记录。

交易日08:15–15:10从网页提交的部署只登记为待处理；命令行部署会直接拒绝。服务器不会执行任意分支名、标签或Shell片段。

### 运行诊断

```bash
sudo systemctl status kfcquant-worker kfcquant-web kfcops
sudo journalctl -u kfcquant-worker -u kfcquant-web -n 200
sudo kfcquant-admin version --json
sudo kfcquant-admin health --json
```

`health`会报告数据库版本、worker心跳、数据供应商和磁盘余量；`migrate`可重复运行。
