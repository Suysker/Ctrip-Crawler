## 模块简介

`gen_proxy_servers.py` 是一个基于 Python 的 asyncio 实现的 SOCKS5 代理服务模块，专门用于在多个 macvlan 接口上切换出站 IPv6 地址。
 模块支持两种代理模式：

- **随机模式 (random)**
   每个出站连接随机选择一个预先创建的 IPv6 接口作为出口。该模式下不启用控制接口。
- **Normal 模式 (normal)**
   所有连接统一使用当前选中的 IPv6 地址，可以通过命令行控制接口（使用 telnet 或 netcat）或直接在 Python 内部调用 `switch_ipv6()` 函数自动切换（该函数每次调用自动累加索引取余，从而切换到下一个接口）。

模块通过异步 I/O（asyncio）在单线程内高效处理大量并发连接，同时在程序退出时利用 `atexit` 自动清理创建的 macvlan 接口，确保环境干净。

**内部原理**
 本模块在指定的物理网卡（例如 `eth0`）上生成多个 macvlan 子接口，并等待每个接口动态分配 IPv6 地址。在每个出站连接建立前，通过调用 `socket.bind()` 将连接绑定到选定的 IPv6 接口上，从而实现多出口切换。
 与旧版本不同，旧版本依赖于内核支持 NAT66（通过 ip6tables 实现 SNAT），要求内核启用 NAT66 特性，而本版本完全在用户空间实现，不依赖内核 NAT66 支持。

更多详细原理请参阅博客：https://blog.suysker.xyz/archives/365
 旧版本源码：[https://github.com/Suysker/Ctrip-Crawler/blob/main/history_version/gen_proxy_servers.py](https://github.com/Suysker/Ctrip-Crawler/blob/main/history_version/gen_proxy_servers.py)

------



## 使用方法

### 1. Linux 命令行下使用

#### 启动代理服务

将本模块保存为 `gen_proxy_servers.py` 后，你可以直接使用 `sudo` 运行，并通过命令行参数指定相关配置。例如（启动随机模式、创建 10 个 macvlan 接口，并在启动前删除已有接口）：

```bash
sudo python3 gen_proxy_servers.py --mode random --port 1080 --bind-address 0.0.0.0 --base-interface eth0 --num-interfaces 10 --delete-iface
```

各参数说明：

- `--mode`：设置代理模式，取值为 `random` 或 `normal`。
- `--port` 与 `--bind-address`：指定 SOCKS5 代理监听的端口和地址。
- `--base-interface`：物理网卡名称（例如 `eth0`）。
- `--num-interfaces`：需要创建的 macvlan 接口数量。
- `--delete-iface`：启动前先删除已有接口，确保环境干净。
- `--control-port` 与 `--control-bind`：仅在 normal 模式下有效，用于控制接口切换；在随机模式下不启用控制接口。

启动后，你会看到类似以下输出：

```makefile
=== 创建 macvlan 接口 ===
可用 IPv6 接口及地址：
0: eth0_1 -> 240e:38d:8c64:8a01:xxxx:xxxx:xxxx:xxxx
1: eth0_2 -> 240e:38d:8c64:8a01:yyyy:yyyy:yyyy:yyyy
...
SOCKS5 代理运行在 0.0.0.0:1080，模式：random
随机模式下，不启用控制接口。
```

#### 测试代理

使用 curl 测试代理是否生效：

```bash
curl --socks5 127.0.0.1:1080 https://ipv6.itdog.cn/
```

执行后页面返回的 IPv6 地址即为出站使用的地址。

#### 切换 IP（仅 normal 模式）

如果使用 normal 模式启动（`--mode normal`），模块会启动一个控制接口（默认监听在 `127.0.0.1:1081`）。在 Linux 命令行中，可以通过 telnet 或 netcat 发送命令切换出站 IPv6 地址。

- 使用 telnet：

  ```bash
  telnet 127.0.0.1 1081
  ```

  连接后输入：

  ```cpp
  switch 1
  ```

  表示切换到接口列表中索引为 1 的 IPv6 地址。

- 使用 netcat：

  ```bash
  echo "switch 1" | nc 127.0.0.1 1081
  ```

#### 关闭代理服务

当你结束使用后，通过 Ctrl+C 中断程序，模块会自动通过 `atexit` 注册的回调函数清理创建的 macvlan 接口。如果需要手动清理，也可执行：

```bash
sudo python3 -c "import gen_proxy_servers; gen_proxy_servers.delete_ipv6_addresses('eth0')"
```

------



### 2. 在其他 Python 程序中调用

你可以将此模块作为库导入到你的 Python 程序中，直接调用 `run_proxy()` 函数启动代理服务。示例如下：

```python
import threading
import time
import gen_proxy_servers

# 启动随机模式代理服务（以线程方式启动，daemon 模式保证主程序退出时代理关闭）
proxy_thread = threading.Thread(target=lambda: gen_proxy_servers.run_proxy(
    mode="random",
    port=1080,
    bind_address="0.0.0.0",
    base_interface="eth0",
    num_interfaces=10,
    delete_iface=True
), daemon=True)
proxy_thread.start()
print("随机模式 SOCKS5 代理服务已启动。")
time.sleep(5)  # 等待代理服务启动

# 在你的 HTTP 请求中（例如 bill_fast）指定代理参数：
# proxies = {"http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080"}

# 如果使用 normal 模式，你可以直接调用内部接口切换函数
# 例如：gen_proxy_servers.switch_ipv6() —— 每次调用自动切换到下一个接口

# 程序退出时，模块会自动通过 atexit 清理所有创建的 macvlan 接口，无需额外调用清理代码
```

### Python 内部直接切换接口（仅 normal 模式）

模块提供了 `switch_ipv6()` 函数，用于在 Python 内部直接切换 IPv6 出口。每次调用该函数会自动累加索引并取余，从而切换到下一个接口。示例如下：

```python
import gen_proxy_servers

# 当代理模式为 normal 时，直接调用该函数即可切换到下一个出口IPv6地址
gen_proxy_servers.switch_ipv6()
```

如果当前代理模式为随机模式，该函数会提示“当前代理模式为随机模式，无法直接切换。”

------



## 与旧版本的区别

旧版本的 `gen_proxy_servers.py` 依赖于内核支持 NAT66，并使用 ip6tables（NAT66）对出站流量进行 SNAT 操作。此方式要求内核启用 NAT66 特性，配置较为复杂。

而本版本的原理是利用应用层 SOCKS5 代理，在每个出站连接建立前调用 `socket.bind()` 指定本地 IPv6 地址，从而实现多出口切换，无需内核 NAT66 支持。

更多详细原理请参阅：https://blog.suysker.xyz/archives/365
 旧版本源码：[https://github.com/Suysker/Ctrip-Crawler/blob/main/history_version/gen_proxy_servers.py](https://github.com/Suysker/Ctrip-Crawler/blob/main/history_version/gen_proxy_servers.py)

------



## 总结

- **Linux 命令行下**
  - **启动**：使用命令行参数启动代理服务，指定模式、端口、物理接口及接口数量。
  - **测试**：使用 `curl --socks5 127.0.0.1:1080 https://ipv6.itdog.cn/` 测试出站 IPv6 地址。
  - **切换（normal 模式）**：使用 telnet 或 netcat 连接控制接口（默认 127.0.0.1:1081），发送 `switch 1` 命令进行切换。
  - **关闭**：通过 Ctrl+C 中断，模块自动清理接口；也可手动调用删除接口函数。
- **Python 程序中调用**
  - 导入模块后，通过 `run_proxy()` 启动代理服务（可在独立线程中启动）。
  - 请求时指定 `proxies={"http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080"}`。
  - 如果使用 normal 模式，可直接调用 `switch_ipv6()` 实现自动切换。
  - 模块在退出时自动清理接口，无需额外代码。