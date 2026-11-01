# 历史脚本

用途：导航历史版本的源码、依赖与运行说明，区分不同版本的代理模块。

## V3 与 V3.5

| 原路径 | 归档路径 | 说明 |
| --- | --- | --- |
| `ctrip_flights_scraper_V3.py` | [ctrip_flights_scraper_V3.py](ctrip_flights_scraper_V3.py) | Edge 浏览器版本 |
| `Linux_version/ctrip_flights_scraper_V3.5.py` | [Linux_version/ctrip_flights_scraper_V3.5.py](Linux_version/ctrip_flights_scraper_V3.5.py) | Linux Chrome 版本 |
| `Linux_version/gen_proxy_servers.py` | [Linux_version/gen_proxy_servers.py](Linux_version/gen_proxy_servers.py) | 与 V3.5 相邻的 asyncio SOCKS5 / IPv6 代理 |

V3.5 从同目录导入 `gen_proxy_servers`，运行时须保留两者的目录关系。

[完整运行与架构分析](../docs/archive/legacy-reference/CON-001-v3-v35-architecture.md)是这三份归档源码的说明入口。V3 的依赖保存在 [requirements-v3.txt](requirements-v3.txt)，V3.5 的依赖和代理说明分别位于 [Linux_version/requirements.txt](Linux_version/requirements.txt) 与 [Linux_version/readme.md](Linux_version/readme.md)。根目录 requirements 对应新版本。

## 更早版本

本目录还包含 V1、V2、历史票价脚本和根级 `gen_proxy_servers.py`。V3.5 配套代理位于子目录 `Linux_version/`，与根级旧代理的实现及接口不同，不可互换。
