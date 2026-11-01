# Ctrip-Crawler

**批量采集携程航班与报价，把多条航线、多个出发日的数据整理成 CSV 和 Excel。**

适合做航线对比、票价观察和航班数据分析。支持国内、国际的不转机行程，Windows 和 Linux 使用同一个 Python 脚本。

[快速开始](#快速开始) · [下载采集脚本](https://raw.githubusercontent.com/Suysker/Ctrip-Crawler/main/ctrip_flights_scraper.py) · [历史数据样例](data_examples/) · [使用指南](docs/guides/GUI-002-usage.md)

## 为什么用它

- **一次配置，批量采集**：填写城市和日期范围，自动遍历各个方向的航线。
- **航班与报价一起保存**：航司、机场、航站楼、起降时间，以及经济舱和商务舱报价，放在同一张表里。
- **为长任务保留进度**：复用登录状态，处理浏览器异常，中断后可以继续原批次。
- **支持多个账号**：当前账号需要验证时，可切换其他可用账号；全部不可用时保存进度并暂停。
- **一个文件就能运行**：采集脚本可单独下载，无需依赖仓库中的其他 Python 文件。

## 快速开始

### 1. 准备环境

需要 **Python 3.12 或以上**。下载[项目压缩包](https://github.com/Suysker/Ctrip-Crawler/archive/refs/heads/main.zip)，解压后在项目目录运行：

```bash
python -m pip install -r requirements.txt
python -m camoufox fetch official/152.0.4-beta.29
python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"
```

这一步用于安装依赖、浏览器和指纹模型，只需准备一次。单文件用户可参考[单独安装说明](docs/guides/GUI-002-usage.md#单独下载脚本)。

项目浏览器会忽略 HTTPS 证书错误，作用范围见 [HTTPS 证书说明](docs/guides/GUI-002-usage.md#https-证书)。

### 2. 填写账号和航线

打开 `ctrip_flights_scraper.py`，在顶部 `DEFAULT_CONFIG` 中修改这些项目，其余先保持默认：

```python
'crawl_citys': ['上海', '北京'],
'begin_date': None,
'crawl_days': 7,
'accounts': [
    {'alias': 'account_1', 'username': '你的账号', 'password': '你的密码', 'enabled': True},
],
```

按默认日期间隔，这会采集**从明天开始的 7 个出发日、上海与北京两个方向，共 14 个任务**。密码直接填写即可；添加更多账号时，每个账号使用不同的 `alias`。

### 3. 登录并开始采集

首次使用，建议先打开登录窗口，按网页提示完成验证：

```bash
python ctrip_flights_scraper.py --login account_1
```

然后开始采集，后续会复用有效的登录状态：

```bash
python ctrip_flights_scraper.py
```

指定出发日期、匿名试跑、显示浏览器等用法，见[采集设置](docs/guides/GUI-002-usage.md#采集设置)。

## 你会得到什么

结果默认保存到脚本旁的 `data/`，按出发日、采集日和航线整理：

```text
data/
└── 出发日期/
    └── 采集日期/
        ├── 上海-北京.csv
        └── 北京-上海.csv
```

每行是一条航班行程，经济舱和商务舱报价并排保存。默认采用中文表头，共 **90 列**，也保留了旧版样例中的字段。

| 数据 | 主要内容 |
| --- | --- |
| 航班信息 | 航班号、航司、起降时间、机场、航站楼、机型、实际承运航班 |
| 舱位报价 | 经济舱与商务舱的票面价、税费、含税价、币种 |
| 舒适度（可选） | 机龄、座椅尺寸、餐食、电源及延误信息 |
| 采集记录 | 采集时间、行程标识，以及价格和舒适度是否完整 |

需要舒适度信息时，将 `comft_flight` 设为 `True`；这会增加页面请求和采集时间。网站未提供的内容会留空并标明缺失，字段含义与旧版兼容说明见[数据说明](docs/concepts/CON-002-v4-design.md#9-数据处理与兼容合同)。

### 导出 Excel

使用配套的 [CSV 转 Excel 工具](csv_to_xlsx_converter.py)，每条航线合并成一个工作簿，保留全部列：

```bash
python -m pip install openpyxl==3.1.5
python csv_to_xlsx_converter.py --input ./data --output ./xlsx_output
```

Excel 中保留原始文本，方便保存完整标识和编码；需要数值计算时，再转换相应列的类型。[更多转换选项 →](docs/guides/GUI-002-usage.md#转换为-excel)

## 中断后继续

按 `Ctrl+C` 可停止派发，让当前操作收尾。继续采集时，保留原输出目录，将日志里的批次编号填入：

```bash
python ctrip_flights_scraper.py --resume <run_id> --output ./data
```

遇到登录失效或人工验证，先处理对应账号，再恢复批次。具体操作见[登录与恢复](docs/guides/GUI-002-usage.md#登录与恢复)。

## 更多资料

- [使用指南](docs/guides/GUI-002-usage.md)：安装、账号池、日期设置、恢复和 Excel 转换。
- [开发指南](docs/guides/GUI-003-development.md)：修改源码、生成单文件及运行测试。
- [架构与运行合同](docs/concepts/CON-002-v4-design.md)：调度、账号状态、数据处理与故障恢复。
- [历史版本](history_version/README.md)：V3、V3.5 及更早版本的源码与说明。

欢迎通过 [Issues](https://github.com/Suysker/Ctrip-Crawler/issues) 反馈问题或提出建议。分享日志前，请移除账号、密码和会话信息。

本项目采用 [MIT License](LICENSE)。
