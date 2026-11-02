# 使用指南

> ID: GUI-002 · Status: Current
> Created: 2026-11-01 · Updated: 2026-11-03
> Purpose: 介绍当前单文件采集器的安装、配置、登录、恢复及 Excel 转换方法。

## 安装

需要 Python 3.12 或以上。Windows 与 Linux 使用同一个脚本；Linux 还需要 Camoufox 所需的系统运行库，参见 [Camoufox 安装说明](https://camoufox.com/python/installation/)。

### 下载完整项目

在项目目录执行：

```bash
python -m pip install -r requirements.txt
python -m camoufox fetch official/152.0.4-beta.29
python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"
python ctrip_flights_scraper.py --check-environment
```

### 单独下载脚本

只需下载根目录的 [ctrip_flights_scraper.py](../../ctrip_flights_scraper.py)，不需要维护源码或构建工具。按脚本声明的版本安装依赖：

```bash
python -m pip install camoufox==0.5.8 playwright==1.60.0 fpgen==1.3.0 filelock==4.2.6 psutil==7.2.2 tzdata==2026.6
python -m camoufox fetch official/152.0.4-beta.29
python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"
python ctrip_flights_scraper.py --check-environment
```

浏览器和指纹模型需要预先安装，程序不会在采集中自动下载或升级。查看参数帮助不需要安装第三方依赖：

```bash
python ctrip_flights_scraper.py --help
```

### HTTPS 证书

项目创建的浏览器上下文启用 `ignore_https_errors=True`，可访问因自签名证书或颁发机构不受信任而被浏览器拒绝的 HTTPS 页面。这适用于云环境代理使用自定义证书、浏览器信任库与环境不一致等情况。

该设置适用于项目上下文中的所有页面，包括登录与采集页面，并非只接受某一张证书。连接仍使用 HTTPS，但浏览器会忽略证书校验错误。程序不导入 CA、不修改系统信任库，也不改变其他浏览器上下文的设置；代理连接失败、登录验证及站点限流仍按各自规则处理。

## 采集设置

编辑脚本顶部的 `DEFAULT_CONFIG`。以下是常用项目：

| 配置 | 用途 |
| --- | --- |
| `crawl_citys` | 城市列表；任意两个不同城市会生成两个方向的任务 |
| `begin_date` | 首个出发日，格式为 `YYYY-MM-DD`；`None` 时按 `start_interval` 从当天推算 |
| `start_interval` | 相对起始日，默认 `1`，即从明天开始 |
| `crawl_days` | 出发日期范围长度；默认 60 天 |
| `end_date` | 指定结束日期；填写后优先于 `crawl_days` |
| `days_interval` | 出发日之间的间隔；默认每天采集 |
| `comft_flight` | 是否采集舒适度，默认 `False` |
| `comfort_timeout` | 舒适度补充上限，默认 15 秒；须为有限非负数，`0` 不主动触发详情 UI |
| `headless` | 是否隐藏浏览器窗口；人工处理验证时应使用有界面模式 |
| `output` | 输出目录；`None` 时使用脚本旁的 `data/` |
| `rename_col` | 默认 `True` 使用中文表头；`False` 使用英文表头 |
| `timezone` | 日期解释、采集日目录与新日志的时区，默认 `Asia/Shanghai` |

默认三个城市、60 个出发日会生成 360 个任务。首次运行建议先用两个城市和少量日期。当前仅处理不转机行程，可保留经停信息；不支持多段转机行程输出。

常用参数也可以从命令行覆盖：

```bash
python ctrip_flights_scraper.py --cities 上海 香港 --begin-date 2027-01-10 --days 2 --output ./data
python ctrip_flights_scraper.py --headed
python ctrip_flights_scraper.py --headless
```

指定日期时，请选择尚未过去的出发日。显式相对路径 `--output ./data` 按当前终端目录解析；不指定输出路径时，使用脚本所在目录。

### 匿名试跑

保持 `accounts` 为空时，可以显式使用匿名模式：

```bash
python ctrip_flights_scraper.py --anonymous --cities 上海 北京 --days 1
```

这会查询两个方向各一个出发日。匿名模式只适用于网站允许未登录访问的查询；要求登录或验证时，程序会停止并说明原因。匿名模式不能同时配置启用的账号。

### 舒适度与代理

将 `comft_flight` 设为 `True` 后，程序通过正常悬停和滚动触发页面详情。`comfort_timeout` 默认 15 秒，可按需要调整；实际补充时间取该值与本次采集剩余时间减去 10 秒收尾预留的较小值，不会突破任务截止时间。设为 `0` 时不主动触发详情 UI，也不额外等待；字段缺失仍会明确标记。新批次冻结该设置，恢复使用原值；旧计划缺少此字段时继续使用 15 秒。

航班较多时可能无法全部覆盖，来源缺失的机龄、座椅或电源信息不会被填造。普通详情失败可保留有效核心结果；HTTP 429 仍按站点限流停止当前操作并保存等待时间。详情和字段解释见 [舒适度处理规则](../concepts/CON-002-v4-design.md#94-舒适度)。

需要代理时，可填写一个外部 HTTP、HTTPS 或 SOCKS5 代理：

```python
'proxy': {'server': 'socks5://127.0.0.1:1080'},
```

需要认证的代理可增加独立的 `username`、`password` 字段。程序不创建网卡或自动轮换 IPv6。

新版批次固定代理的非认证连接配置，单独更新 `username`、`password` 后即可用普通 `--resume` 继续原批次；端点、协议、bypass 等其他设置不能随恢复改变。配置相同不代表代理服务分配的实际公网 IP 一定相同。

旧批次的整体代理摘要可能包含认证字段。只有在确认端点、协议、bypass 等非认证设置完全没变、此次仅刷新认证凭据时，才使用显式迁移：

```bash
python ctrip_flights_scraper.py --resume <run_id> --output ./data --refresh-proxy-credentials --recovery-reason "代理连接配置未变，仅更新认证凭据"
```

旧摘要不可逆，程序无法替执行者核实原始端点；不要用此选项切换代理或在直连与代理间转换。程序会保留原计划，在恢复记录中保存原摘要、新连接摘要、时间和原因，后续恢复继续严格核对新绑定。新版批次仅更新认证字段时无需此选项，不要手动修改 run.json 或其摘要。

## 登录与恢复

### 配置账号池

在 `DEFAULT_CONFIG` 中直接填写账号和明文密码：

```python
'accounts': [
    {'alias': 'account_1', 'username': '你的账号', 'password': '你的密码', 'enabled': True},
    {'alias': 'account_2', 'username': '另一个账号', 'password': '另一个密码', 'enabled': True},
],
'challenge_policy': 'switch',
```

每项的 `alias` 和登录账号必须唯一。已有有效会话时可以不提供密码；需要重新登录且没有密码时，使用有界面模式完成认证。个人凭据只保存在自己的脚本配置中，不应提交到公开仓库。

首次使用或需要重新验证某个账号时执行：

```bash
python ctrip_flights_scraper.py --login account_1 --output ./data
```

按照打开的网页完成登录和验证，然后使用同一个输出目录继续采集。

`challenge_policy='switch'` 会在当前账号需要人工验证时切换其他可用账号；`wait_then_switch` 配合 `--headed`，允许先等待人工处理。账号全部不可用时暂停并保留进度。站点限流会触发全局等待，换号或新建批次不会取消服务端要求的等待时间。

### 停止与继续

第一次按 `Ctrl+C` 后，程序停止派发新操作，等待当前有界操作收尾；再次中断会请求立即退出。

恢复时，将 `<run_id>` 替换为日志中的批次编号，并沿用原输出目录：

```bash
python ctrip_flights_scraper.py --resume <run_id> --output ./data
```

恢复使用原批次的城市、日期和查询设置，不按恢复当天重新生成日期。已完成任务不重新采集，当前未完成任务继续使用累计预算。普通恢复不会增加额度；预算已耗尽时可按下文显式追加，日期过期或文件冲突则仍需处理对应原因。

### 查看错误与暂停原因

`crawler.log` 的新日志按 `timezone` 设置记录带 UTC offset 的 ISO 8601 时间；既有日志不重写。默认时区为 `Asia/Shanghai`，恢复和观察记录中的持久时间仍为带时区 UTC。

批次的 `.runs/<run_id>/progress.json` 中，`last_error` 保存最近一次操作错误及子进程诊断，`pause_reason`、`pause_diagnostic` 单独记录最终暂停原因。诊断包括异常类型、阶段、脱敏消息、有限堆栈及必要的协议结构摘要，总量不超过 8 KiB；预算耗尽后仍可查看此前具体失败的位置。堆栈只含文件名、行号和函数名，不记录局部变量、完整响应或个人绝对路径。

`worker_error` 表示未知普通 Python 异常，程序会保留诊断并停止自动重试，应先检查具体错误。已知浏览器异常和超时按有限预算恢复。响应为 `finished=False` 且暂时没有航班列表时，会继续等待同一请求链；这本身既不是协议失败，也不是没有航班。

CLI 初始化阶段的未知异常会以 `unexpected_error` 在标准错误输出同样有界、脱敏的诊断。此时日志文件可能尚未创建，排查时应同时保留终端错误输出。

### 为当前任务追加有限额度

确认临时故障已解除后，可为实际耗尽次数或采集时间预算的当前任务追加一轮额度。先从暂停日志或 progress.json 核对从零开始的任务序号 `current.index` 和已登记尝试次数 `current.attempt_count`；例如当前任务序号为 0、已尝试 3 次：

```bash
python ctrip_flights_scraper.py --resume <run_id> --output ./data --retry-current 0:3 --recovery-reason "已确认临时故障解除，继续当前任务"
```

示例中的 `0:3` 必须替换为当前实际值；程序同时核对 cursor、任务序号和尝试次数。发起新一轮追加时若仍有自动额度，或任务已推进、token 过期、存在未完成的结果发布，会拒绝追加；重复同一已记录 token 则按下文的幂等规则处理。原因必须非空且不超过 256 字符，不要写入密码或其他凭据。

每轮增加 `max(3, 启用账号数)` 次采集机会及配置的 `task_budget` 秒工作预算，保留原有累计消耗。认证次数、认证预算、当前任务的账号阻塞、账号冷却及站点等待都不清零。程序不会跳过当前任务、删除旧结果或给后续任务追加额度。

追加记录先保存到当前任务的 `manual_recoveries`，成功后随结果 meta 保留原因、时间及新旧额度。新尝试尚未派发时，重复同一 token 不会重复加额度；新尝试一经派发、次数改变后，旧 token 就会被拒绝。

### 保留输出目录

航班 CSV 位于 `<出发日期>/<采集日期>/` 下。同目录的 `.meta.json` 用于核对结果，输出根目录还包含 `.runs/`、`accounts.json` 和 `sessions/` 等恢复与会话文件。

保留整个输出目录才能继续原批次。运行期间不要手动修改待发布文件；继续原批次不会重采已完成任务，也不会自动移动旧文件。新建批次若仍采集同一天的同一航线，应使用独立输出目录；不同批次写入已有结果路径时，程序报告冲突并保留原 CSV 与 meta。分享数据时可选择 CSV，账号状态、Cookie、日志及原始网络材料应留在本地。

价格缺失、舒适度不完整与“没有航班”是不同状态。完整字段来源、`*_full` 含义及旧字段兼容规则见 [数据说明](../concepts/CON-002-v4-design.md#9-数据处理与兼容合同)。

## 转换为 Excel

下载配套的 [csv_to_xlsx_converter.py](../../csv_to_xlsx_converter.py)，安装转换依赖后运行：

```bash
python -m pip install openpyxl==3.1.5
python csv_to_xlsx_converter.py --input ./data --output ./xlsx_output
```

工具按航线文件名合并 CSV，保留所有输入列的并集及原列名。某个旧文件没有的列留空，不补造新版状态或标识。

不传参数时，读取转换脚本旁的 `data/`，输出到脚本旁的 `xlsx_output/`。只转换指定出发日和采集日时：

```bash
python csv_to_xlsx_converter.py --input ./data_examples --output ./xlsx_examples --departure-start 2024-04-01 --departure-end 2024-04-01 --observed-date 2024-02-01
```

筛选依据 `<出发日期>/<采集日期>/<航线>.csv` 目录，不会用目录日期覆盖 CSV 内已有的日期。未指定日期筛选时，处理全部符合目录布局的文件。已有同名 XLSX 默认不覆盖，需要替换时显式加上 `--overwrite`。

为保留前导零、长标识、金额字符串和 JSON，单元格按文本写入，空值保留为空单元格。以 `=` 开头的内容也作为文本；需要数值运算时，应明确转换对应列的数据类型。超过 Excel 行数、列数或单元格长度限制时会报错，不会静默截断。
