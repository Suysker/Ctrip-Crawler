# Ctrip-Crawler

用途：介绍航班采集脚本的安装、账号配置、运行及恢复方式。

通过 Camoufox 的真实浏览器页面获取携程航班搜索响应，按城市对和出发日期输出航班、经济舱与商务舱报价 CSV。根目录的 [ctrip_flights_scraper.py](ctrip_flights_scraper.py) 是完整单文件：可以单独下载，不需要仓库中的其他 Python 文件。

## 安装

需要 Python 3.12 或以上，以及 Camoufox 所需的系统运行库。Windows 与 Linux 使用同一个脚本。

只下载脚本时，按脚本头部的版本安装：

```bash
python -m pip install camoufox==0.5.8 playwright==1.60.0 fpgen==1.3.0 filelock==4.2.6 psutil==7.2.2 tzdata==2026.6
python -m camoufox fetch official/152.0.4-beta.29
python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"
python ctrip_flights_scraper.py --check-environment
```

下载完整仓库时也可使用 `python -m pip install -r requirements.txt`。浏览器构建需要单独安装；脚本不在采集中自动下载或升级依赖。

## 配置账号池

在单文件顶部编辑 `DEFAULT_CONFIG`。每项账号的别名必须唯一；不要将自己的凭据提交到公开仓库。

```python
'accounts': [
    {'alias': 'account_1', 'username': '你的账号', 'password': '你的密码', 'enabled': True},
    {'alias': 'account_2', 'username': '另一个账号', 'password': '另一个密码', 'enabled': True},
],
'challenge_policy': 'switch',
```

直接在 `username` 和 `password` 中填写账号与明文密码，无需设置环境变量。已有有效登录状态时可以复用状态；密码留空且需要重新登录时，使用有界面模式完成认证。

脚本持续复用当前可用账号。需要人工验证而暂时无法完成时，冷却该账号并切换下一候选；不会因为切账号而重置当前任务的尝试预算。账号全部不可用时暂停，保留原因和恢复位置。

需要人工认证时：

```bash
python ctrip_flights_scraper.py --login account_1
```

`switch` 不等待人工输入；`wait_then_switch` 需要有界面模式，允许短暂人工处理。明确站点限流是整个输出目录的等待约束，新批次或换号不会取消该等待。

## 采集与恢复

修改顶部城市、日期和输出参数后直接执行：

```bash
python ctrip_flights_scraper.py
```

城市列表按有方向的城市对生成任务；两个城市会查询往返两个方向。日期范围内按 `days_interval` 取出发日。默认配置为三个城市、60 个出发日，共 360 个任务，首次使用可先缩小城市和日期范围。

也可覆盖常用参数：

```bash
python ctrip_flights_scraper.py --cities 上海 香港 --begin-date 2027-01-10 --days 2 --output ./data
python ctrip_flights_scraper.py --resume <运行日志中的批次标识> --output ./data
```

未配置账号时，可以显式用 `--anonymous` 执行不需要认证的查询；网站要求登录或验证时会明确停止，不伪装成无航班。

`--help` 不需要安装第三方依赖。`--headed` 和 `--headless` 控制浏览器显示模式。第一次 Ctrl+C 停止派发并等待当前有界操作收尾，再次中断会请求立即退出。

恢复使用原批次的日期、查询选项和城市身份，不按当天重新生成日期。任务预算已经耗尽时，resume 不会自动清零。只有确认属于本次任务且摘要匹配的完成文件才会用于恢复。

## 输出与数据语义

```text
<data>/
├── .runs/<run_id>/             # 计划、恢复位置与资源归属
├── accounts.json              # 账号池运行状态
├── sessions/                  # 私有认证状态
└── <出发日期>/<采集日期>/
    ├── <出发城市>-<到达城市>.csv
    └── <出发城市>-<到达城市>.meta.json
```

CSV 使用 UTF-8，以行程为行，经济舱与商务舱报价为并列字段，含采集时间、行程身份及实际承运航班。直飞筛选不会把转机行程截成首段输出，当前未支持多段结果格式。

默认中文表头固定为 90 列，覆盖 `data_examples/` 旧样例的全部 56 个列名，并保留新版标识、价格状态和舒适度字段。航站楼、航司与联盟、城市编码、原始日期时间、经停列表及旧接口标记按响应原值导出；缺失或 null 留空，`0`、`False` 保留。对象和列表保存为 JSON 文本，显式空列表为 `[]`，不与未返回混同。起降时间使用 `HH:MM:SS`，原始日期时间另保留在 `departureDateTime` / `arrivalDateTime`。

`economy_full` / `bussiness_full` 沿用旧映射，保存各舱位所选报价的 `miseryIndex` 原值；它不参与最低价选择，不解释为标准全价，也不替代含税总价。原始 `operateFlightNo` 与新版“实际承运航班号”同时保留。字段来源与英文列名规则见 [数据处理合同](docs/concepts/CON-002-v4-design.md#9-数据处理与兼容合同)。

每舱全部候选报价的税费和含税总价信息完整、币种明确且一致时，按含税总价选择最低报价，比较口径为 `total_fare`；基础价、税费和总价始终来自同一报价。若候选缺少税费、总价或币种，保留可用票面价，缺失字段留空，该舱和整体价格状态标为 `partial`，改按来源 `adultPrice` 数值选择。币种均明确时口径为 `base_fare`；存在未知币种时为 `base_fare_currency_unknown`，只表示同一来源 `adultPrice` 数值最小，不能解释为已确认同币种最低价或含税最低价。已知币种冲突时拒绝结果。

CSV 包含每舱的价格状态、比较口径、币种，以及行程整体的价格状态和不完整原因。整舱没有报价标记 `not_available`；有航班但两舱均无目标报价仍保留行程并标记 `partial`，与没有航班的 `empty` 结果区分。完成凭据保存任务级 `price_status` 和 `price_reason`，运行汇总中的 `price_partial` 统计价格不完整的任务数；任务采集成功不表示价格字段全部完整。

将 `DEFAULT_CONFIG` 中的 `comft_flight` 设为 `True` 可采集舒适度（默认关闭）。程序监听当前页面的详情响应，并在最多 15 秒的补充预算内依次悬停航班号／机型，让页面正常请求详情；不会自行调用或重放接口。页面仍会产生额外的详情网络请求，不能把监听理解为没有请求成本。大量航班不保证在此预算内全部覆盖。

舒适度按实际承运航班匹配；已有舱位按其票面报价判断，不依赖税费是否完整。未收到有效详情标为 `unavailable`，只取得部分字段标为 `partial`；来源未提供的廊桥率、电源等字段保持空白。普通详情错误或信息不足仍可保存核心报价。HTTP 429 属于站点级限流，会停止当前操作并保存全局等待时间；在舒适度阶段收到限流也不能继续请求或换号绕过等待。不存在航班与响应错误是不同结果。

座椅倾斜度为零、宽度或间距按来源页面取整后为零时，页面含义是“暂无数据”，CSV 留空并标记信息不完整。餐食以 `mealMsg` 的文字描述为准；`hasMeal` 只保留接口原值，不能直接将 `False` 解释为无餐食。为避免误读，中文列名使用“经济舱餐食标记（原值）／商务舱餐食标记（原值）”，替代原先的“是否有餐食”；英文列名 `Y_has_meal` / `C_has_meal` 保持不变。

正式 CSV 先写完临时文件，再写摘要凭据和推进恢复点。不同批次的目标路径冲突时停止，不覆盖历史结果。数据目录应为本程序独占写入；不要在运行时用其他程序修改待发布文件。

默认数据目录位于脚本旁的 `data/`；显式相对 `--output` 按当前终端目录解析。账号状态、Cookie、日志和网络诊断材料均为私有运行数据。

## 转换为 Excel

[csv_to_xlsx_converter.py](csv_to_xlsx_converter.py) 是独立转换脚本，按航线文件名将多个日期的 CSV 合并为 XLSX。它保留输入列的并集及原列名，不筛选成固定少数列、不将票面价改名为“票价”，也不覆盖已有的日期字段。

```bash
python -m pip install openpyxl==3.1.5
python csv_to_xlsx_converter.py --input ./data --output ./xlsx_output
python csv_to_xlsx_converter.py --input ./data_examples --output ./xlsx_examples --departure-start 2024-04-01 --departure-end 2024-04-01 --observed-date 2024-02-01
```

不传参数时，读取脚本旁的 `data/`，写入脚本旁的 `xlsx_output/`。日期筛选依据 `<出发日期>/<采集日期>/<航线>.csv` 目录；未指定筛选时处理全部匹配文件。既有输出默认不覆盖，需要替换时显式使用 `--overwrite`。

为保留前导零、长标识、金额字符串、JSON 及空值，单元格按文本写入，不自动猜测类型；以 `=` 开头的原值也按文本保留。需要 Excel 数值运算时，可明确转换相应数值列。旧、新 CSV 可以一起转换，某文件没有的列留空；超过 Excel 的行数、列数或单元格长度限制时明确报错，不静默截断。此工具只负责转换，不会为旧数据补造新版状态或标识。

## 开发与历史版本

维护源码位于 `source_sections/`，按固定顺序生成可读的单文件；使用者运行成品不需要这些片段。

```bash
python -m pip install -r requirements-dev.txt
python scripts/build_single_file.py
python scripts/build_single_file.py --check
python -m pytest tests -q
```

仓库维护请修改权威片段并重新生成成品，不同时维护第二份逻辑。完整验收包括仓库外单文件运行、文件中断恢复、父子进程故障、账号池状态和浏览器业务场景。

设置环境变量 `CTRIP_BROWSER_TESTS=1` 后运行 pytest，可启用需要已安装 Camoufox 的浏览器测试。长跑使用 `tests/e2e/run_soak.py`：先把成品单独复制到仓库外，将 `CTRIP_ACCEPTANCE_APP` 指向该副本，再执行 `python tests/e2e/run_soak.py --output <新的验收目录> --tasks 10000 --hours 24`。该场景使用真实浏览器与生产调度逻辑，通过受控响应避免向网站持续发送测试请求；结果及资源采样分别保存在 `acceptance.json` 和 `metrics.jsonl`。任务数量、持续时间与资源稳定性均需核对，不能把短时测试当作长期稳定性结论。

- [架构与运行合同](docs/concepts/CON-002-v4-design.md)
- [文档索引](docs/README.md)
- [历史版本索引](history_version/README.md)
- [V3 原使用说明与教程入口](docs/archive/legacy-reference/GUI-001-v3-readme.md)

本项目采用 [MIT License](LICENSE)。浏览器与第三方依赖按各自许可分发。
