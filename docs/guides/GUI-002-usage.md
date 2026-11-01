# 使用指南

> ID: GUI-002 · Status: Current
> Created: 2026-11-01 · Updated: 2026-11-02
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
| `headless` | 是否隐藏浏览器窗口；人工处理验证时应使用有界面模式 |
| `output` | 输出目录；`None` 时使用脚本旁的 `data/` |
| `rename_col` | 默认 `True` 使用中文表头；`False` 使用英文表头 |

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

将 `comft_flight` 设为 `True` 后，程序通过正常悬停和滚动触发页面详情，在最多 15 秒的补充时间内采集。航班较多时可能无法全部覆盖，来源缺失的机龄、座椅或电源信息也不会被填造。详情和字段解释见 [舒适度处理规则](../concepts/CON-002-v4-design.md#94-舒适度)。

需要代理时，可填写一个外部 HTTP、HTTPS 或 SOCKS5 代理：

```python
'proxy': {'server': 'socks5://127.0.0.1:1080'},
```

需要认证的代理可增加 `username`、`password`。程序不创建网卡或自动轮换 IPv6。

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

恢复使用原批次的城市、日期和查询设置，不按恢复当天重新生成日期。已用任务预算不会清零；如果程序提示预算已耗尽、出发日期已过期或文件冲突，应先检查原因，不能通过反复恢复强行继续。

### 保留输出目录

航班 CSV 位于 `<出发日期>/<采集日期>/` 下。同目录的 `.meta.json` 用于核对结果，输出根目录还包含 `.runs/`、`accounts.json` 和 `sessions/` 等恢复与会话文件。

保留整个输出目录才能继续原批次。运行期间不要手动修改待发布文件；不同批次写入同一个结果路径时，程序会报告冲突而不会覆盖旧结果。分享数据时可选择 CSV，账号状态、Cookie、日志及原始网络材料应留在本地。

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
