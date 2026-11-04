# 长任务托管与无人值守恢复

> ID: OPS-001 · Status: Current
> Created: 2026-11-05 · Updated: 2026-11-05
> Purpose: 使用固定批次标识及操作系统进程管理器运行同一采集脚本，说明自动恢复、停止、日志与宿主生命周期边界。

本指南使用同一个 `ctrip_flights_scraper.py`。安装依赖、浏览器、指纹模型以及账号登录方式见[使用指南](GUI-002-usage.md)。部署模板是可选的启动配置，单文件本身不依赖这些模板。

## 1. 先固定批次与输出目录

每个正在运行的批次使用一个固定名称和一个持久输出目录，例如：

```bash
python ctrip_flights_scraper.py --output /var/lib/ctrip-crawler/data --run-id batch-001
```

`--run-id` 在该名称首次运行时创建计划；同名再次启动时恢复同一计划。批次已完成时返回 0，不创建新计划、不重采已有任务。首次生成的城市、日期和业务选项随计划固定，跨天恢复不会重新计算“明天”。输出锁同时约束创建、恢复和登录，不能让多个启动器同时写同一个输出目录。

进程重启、机器重启和故障重启均保持相同名称、相同输出目录。不要在每次启动时动态生成名称，否则进程恢复会变成新批次。新一轮观察需明确选择新名称，并按[输出与恢复说明](GUI-002-usage.md)处理同日同航线文件冲突。

默认 `auto_recovery_rounds=12`、`auto_recovery_cooldown=300`。网络或 worker 故障耗尽当前额度后，程序在有限轮次内冷却并追加一轮额度；代理连接故障先进入冷却。轮次和最早重试时间持久保存，重启不清零。登录验证、配置、协议与存储问题仍可能要求人工处理；程序不会用无限重试覆盖这些错误。

恢复发现尚未结算的预留操作时，以 `process_interrupted` 记录事实并保守扣除预留预算。这只表示操作没有正常结算，不能据此认定发生了 SIGKILL、OOM 或其他特定宿主事件。

## 2. Linux：使用 systemd 管理主进程

### 2.1 准备运行账号与持久目录

在有 systemd 的主机上，使用专用普通账号运行采集器。示例约定：

| 项目 | 示例位置 |
| --- | --- |
| 程序与虚拟环境 | `/opt/ctrip-crawler/` |
| 运行账号 | `ctrip` |
| 批次、结果与会话 | `/var/lib/ctrip-crawler/data/` |

依赖、Camoufox 及指纹模型应由该运行账号准备，并用同一账号执行一次环境检查。浏览器缓存通常随账号区分；管理员账号下检查通过，不等于服务账号下也已安装。需要短信或图形验证时，在可交互环境中先完成登录，再启动无头服务。

```bash
sudo -u ctrip -H /opt/ctrip-crawler/.venv/bin/python /opt/ctrip-crawler/ctrip_flights_scraper.py --check-environment
```

### 2.2 审阅并安装模板

仓库提供 [ctrip-crawler.service](../../deploy/ctrip-crawler.service)。先修改账号、路径、输出目录和固定批次名称，再由主机管理员安装：

```bash
sudo install -m 0644 deploy/ctrip-crawler.service /etc/systemd/system/ctrip-crawler.service
sudo systemd-analyze verify /etc/systemd/system/ctrip-crawler.service
sudo systemctl daemon-reload
sudo systemctl enable --now ctrip-crawler.service
```

这些是部署步骤；仓库中的模板不会自行安装或启用服务。容器内若没有运行 systemd，应使用宿主或平台提供的进程托管能力，并保持同样的固定批次与退出策略。

模板中的核心策略是：

```ini
Restart=on-failure
RestartPreventExitStatus=2 3 4 130
RestartForceExitStatus=SIGTERM SIGHUP
RestartSec=30
KillMode=mixed
TimeoutStopSec=960
```

`RestartPreventExitStatus` 排除明确的受控停止；管理员执行 `systemctl stop` 也不会触发自动重启。`RestartForceExitStatus` 还覆盖程序尚未安装信号处理器时的外部 SIGTERM/SIGHUP，避免它们被当作正常信号退出而漏掉恢复；程序已处理 SIGTERM 并返回 143 时，按失败退出策略重启。重新启动仍使用原命令中的固定批次名称。这些行为见 [systemd.service 官方手册](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)；[官方重启判断实现](https://github.com/systemd/systemd/blob/main/src/core/service.c)先排除管理员停止，再检查强制重启状态。

`KillMode=mixed` 在停止时先向主进程发送 SIGTERM，最终强制清理涉及整个服务 cgroup。程序收到 SIGTERM 后停止派发，等待当前有界操作收尾并保存中断状态；超过停止期限后，systemd 可以强制终止剩余进程。模板的 960 秒覆盖默认最长 900 秒操作及关闭开销；提高应用操作或关闭超时时，必须相应提高停止期限。详见 [systemd.kill 官方手册](https://www.freedesktop.org/software/systemd/man/252/systemd.kill.html)。

模板还设置了 600 秒内最多 12 次启动的速率限制，避免主进程持续崩溃时不断创建新进程。该限制属于 systemd，应用内的自动恢复轮次另行落盘；两者不能互相清零。速率限制语义见 [systemd.unit 官方手册](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml)。

### 2.3 看清“完成”“等待”和“停止”

| 主程序结果 | 服务处理 | 后续动作 |
| --- | --- | --- |
| 0：固定批次完成 | 不重启 | 需要新观察时明确创建新批次 |
| 2：配置或环境错误 | 不重启 | 修正配置或运行账号的环境 |
| 3：需要人工处理或自动恢复额度耗尽 | 不重启 | 阅读暂停原因，再决定受支持的恢复操作 |
| 4：程序、存储或状态错误 | 不重启 | 保留证据，处理错误后再恢复 |
| 130：用户中断 | 不重启 | 保持原计划，按需重新启动 |
| 143：收到 SIGTERM 并有序中断 | 外部终止后重启；`systemctl stop` 不重启 | SIGTERM 本身不证明人工意图 |
| 未被排除的异常进程终止 | 限速重启 | 同一批次保守恢复，不生成新计划 |

应用在自动冷却期间仍是运行中的服务，不需要外部反复执行 `--resume`。正常完成后 systemd 显示 inactive 并不表示采集失败，应同时查看程序退出码及 `progress.json` 的 completed 状态。

```bash
systemctl status ctrip-crawler.service
systemctl show ctrip-crawler.service -p MainPID -p ExecMainCode -p ExecMainStatus -p Result -p NRestarts
journalctl -u ctrip-crawler.service --since today
```

模板同时把 stdout、stderr 写入 journal，因此 Python 启动失败、依赖检查失败和程序日志初始化之前的错误也有去处。采集器的 `crawler.log` 负责业务日志，不能代替启动器的完整 stderr。

需要停止时使用 `systemctl stop ctrip-crawler.service`。排除故障后使用 `systemctl start ctrip-crawler.service`，保持相同名称和输出目录。若触发了 systemd 启动速率限制，先确认原因，再执行 `systemctl reset-failed ctrip-crawler.service`；这不会重置采集器保存的任务预算或自动恢复轮次。

## 3. Windows：任务计划运行同一个 Python 文件

Windows 使用同一虚拟环境、同一采集脚本及固定 `--run-id`。先在计划任务将使用的账号下完成环境检查和必要登录，再配置后台无头运行；交互式短信验证不应依赖无人登录的后台桌面。

任务计划的“失败后重新启动”按任务失败执行，没有 systemd 那样的指定退出码排除列表，见 Microsoft 的 [RestartOnFailure 定义](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-restartonfailure-settingstype-element)。仓库提供一个只负责退出码转换的 [PowerShell 启动桥接](../../deploy/run-ctrip-task.ps1)：

- 原始 Python 退出码追加到输出目录的 `task-launcher.log`；该记录指向每次启动独立保存的原始 stdout/stderr 文件，避免启动诊断被后续运行覆盖。
- 完成及受控停止码 0、2、3、4、130 映射为调度器的 0，防止无限重启需要人工处理的任务。
- 143 仍映射为调度器的 1：SIGTERM 不说明是谁终止进程，不能把平台终止误当作人工暂停。
- 其他非正常退出映射为调度器的 1，由任务计划执行有限重启。

这里的调度器 0 仅表示“无需再次启动”。批次是否完成，应查日志中的 `native_exit` 和采集器的 progress，不能仅看“上次运行结果”。桥接不包含采集、日期展开或恢复逻辑。

在“任务计划程序”中创建任务：

1. 选择准备过浏览器和会话的运行账号，按需要设置“无论用户是否登录都运行”。
2. 触发器选择一次启动或系统启动；正在恢复同一批次时保持名称不变。
3. 操作的程序填写 `powershell.exe`，也可使用已安装的 `pwsh.exe`；“起始于”填写项目目录；参数示例如下，路径按自己的安装位置修改。
4. 设置“如果任务已在运行，则不启动新实例”；启用失败后每 1 分钟重启、最多 12 次；按预计批次时长调整或取消运行时间上限。

```text
-NoProfile -File "C:\CtripCrawler\deploy\run-ctrip-task.ps1" -Python "C:\CtripCrawler\.venv\Scripts\python.exe" -Script "C:\CtripCrawler\ctrip_flights_scraper.py" -Output "C:\CtripCrawler\data" -RunId batch-001
```

启动桥接需符合主机的 PowerShell 脚本执行策略。策略拒绝加载时，由管理员按本机规范授权或签名；模板不会修改系统执行策略。脚本尚未加载时不会生成 `task-launcher.log`，应先查任务历史中的启动失败原因。

Microsoft 的 [任务设置参考](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset)说明了 RestartCount、RestartInterval、ExecutionTimeLimit 和 IgnoreNew。任务计划的运行时限默认可能为三天，应明确核对，避免长任务被时限结束。

需要人工停止时，使用任务计划的“结束”或 Stop-ScheduledTask；如不再允许后续触发，同时禁用任务。只结束 Python 进程会被桥接视为应恢复的进程故障。任务计划停止可能直接杀死进程，不能当作 POSIX SIGTERM 的优雅停止。再次启动仍保持相同名称，由保存的计划和预留预算恢复。诊断任务未启动或启动后立即退出时，查看 `task-launcher.log` 指向的 stderr、任务历史和 Event Viewer 中的 `Microsoft-Windows-TaskScheduler/Operational`，而不是只看采集器日志。

## 4. 保留运行证据与宿主边界

故障后保留原批次目录、实际启动命令、各次进程 PID/创建时间、退出码、完整 stdout/stderr，以及对应时间段的内核或平台记录。`process_interrupted` 不是宿主退出原因；OOM 需要内核/cgroup 证据，平台会话回收需要平台记录。

systemd 或任务计划只在其自身仍存活且允许启动时提供进程恢复。整个容器、虚拟机或宿主被停止、磁盘丢失、平台禁用任务时，脚本不能自行保证继续运行。输出目录应放在平台提供的持久存储上，宿主启动策略应由平台配置。应用的 `memory_limit_mb` 只约束监测到的 worker 进程树，不等于容器总内存上限，不能替代宿主资源规划。

仓库内的本地终止恢复验收用于检查固定批次、信号与文件恢复链路，不代表已经在目标主机安装、启用或完成了系统服务的运行验收。
