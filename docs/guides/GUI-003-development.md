# 开发与验证指南

> ID: GUI-003 · Status: Current
> Created: 2026-11-01 · Updated: 2026-11-02
> Purpose: 说明维护源码、生成独立单文件和运行自动化验证的方法。

## 维护源码与分发文件

维护源码位于 `source_sections/`，根目录的 `ctrip_flights_scraper.py` 是生成后的完整单文件。仓库修改应编辑相应片段后重新生成，避免两份实现独立演变。单独下载脚本的使用者可以直接修改成品顶部配置。

```bash
python -m pip install -r requirements-dev.txt
python scripts/build_single_file.py
python scripts/build_single_file.py --check
```

生成顺序及检查规则由 `scripts/build_single_file.py` 定义。生成文件固定使用 LF 换行，Git 属性用于保持跨平台检出后的构建一致性。各模块职责见 [源码导航](../concepts/CON-002-v4-design.md#17-源码导航)。

## 自动化检查

运行常规测试：

```bash
python -m pytest tests -q
```

浏览器检查需要先按 [安装说明](GUI-002-usage.md#安装) 准备固定版本的 Camoufox，再设置 `CTRIP_BROWSER_TESTS=1`。

PowerShell：

```powershell
$env:CTRIP_BROWSER_TESTS = '1'
python -m pytest tests -q
```

Linux shell：

```bash
CTRIP_BROWSER_TESTS=1 python -m pytest tests -q
```

这些检查覆盖数据转换、账号状态、受控页面交互、进程监督和文件中断恢复。真实站点的响应与登录行为可能变化，受控测试不能替代必要的真实业务核对。验证范围与故障矩阵见 [验证标准](../concepts/CON-002-v4-design.md#16-验证方法与可靠性标准)。

`tests/test_browser_tls.py` 使用临时自签名证书和本机 HTTPS 服务，验证项目上下文可访问该服务，而同一浏览器的严格校验上下文仍拒绝它。此检查还需要 OpenSSL：将 `openssl` 加入 PATH，或将 `CTRIP_TEST_OPENSSL` 设置为可执行文件路径；缺少时该项会明确跳过。证书和密钥只生成在测试临时目录，不写入系统信任库，不提交到仓库。

## 独立单文件验证

将 `ctrip_flights_scraper.py` 复制到仓库外，在没有 `source_sections/` 和项目模块的目录中检查：

```bash
python ctrip_flights_scraper.py --help
python ctrip_flights_scraper.py --check-environment
```

按实际使用方式检查配置、浏览器启动、任务提交及恢复。Windows 与 Linux 均应验证分发成品，不能只测试维护片段。

## 受控故障恢复

`tests/e2e/run_incident_recovery.py` 使用真实 Camoufox、生产父子进程和 CLI 入口，在本地拦截全部页面请求，无需账号或访问携程。它将当前单文件复制到空验证目录，依次验证正常任务提交、协议错误暂停、导航错误重试耗尽、普通恢复不扩额度，以及显式追加额度后继续原批次。

```bash
python tests/e2e/run_incident_recovery.py --output artifacts/recovery-check
```

每次使用新的空目录。`acceptance.json` 保存成品摘要、各阶段退出码和验收结果，`events.jsonl` 保存受控请求事件；同时检查缺列表的未完成通知能够继续等待、已完成 CSV 和 meta 不变、原始计划不变、人工恢复记录进入结果凭据。该场景用于验证恢复链路，不代表真实代理或账号已通过验收。

## 受控长跑

`tests/e2e/run_soak.py` 使用真实 Camoufox 和生产调度逻辑，通过受控响应避免持续请求真实网站。

先把成品复制到仓库外，将环境变量 `CTRIP_ACCEPTANCE_APP` 设置为该副本的绝对路径，再运行：

```bash
python tests/e2e/run_soak.py --output <新的验证目录> --tasks 10000 --hours 24
```

输出目录中，`acceptance.json` 记录任务、持续时间与运行结果，`metrics.jsonl` 保存资源采样。脚本检查任务提交、行数和完成后恢复；内存低水位、峰值、线程与打开资源的增长仍需根据采样单独核对。短时运行不等于通过长期稳定性标准，运行结果为 `passed` 也不代表资源指标已经全部达标。

测试报告、截图、认证状态和原始响应保存在本地 `artifacts/` 等忽略目录，不作为源码提交。公开分享问题复现材料时，应先移除凭据和会话信息。
