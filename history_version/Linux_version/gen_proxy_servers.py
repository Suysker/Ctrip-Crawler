#!/usr/bin/env python3
import os
import re
import subprocess
import socket
import asyncio
import random
import sys
import atexit

#####################################
# 接口管理相关函数（保留原始逻辑）
#####################################
def is_root():
    return os.geteuid() == 0

def interface_usable(interface_name, test_addr='2400:3200::1', max_retries=3):
    """尝试 ping 指定地址，判断接口是否可用。"""
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-I", interface_name, test_addr],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
            )
            if result.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            print(f"[{interface_name}] Ping attempt {attempt+1} timed out, retrying...")
        except subprocess.SubprocessError as e:
            print(f"[{interface_name}] Error pinging: {e}")
    return False

def get_existing_interfaces(base_interface='eth0'):
    """
    解析 ip addr 输出，返回所有匹配的 macvlan 接口及其 IPv6 地址（排除链路本地地址）。
    接口名称格式为 "base_interface_<数字>@..."
    """
    output = subprocess.run(["ip", "addr", "show"], stdout=subprocess.PIPE).stdout.decode()
    iface_pattern = re.compile(re.escape(base_interface) + r'_([0-9]+)@')
    matches = iface_pattern.findall(output)
    interfaces = [f"{base_interface}_{num}" for num in matches]
    iface_ipv6 = {}
    for iface in interfaces:
        out = subprocess.run(["ip", "addr", "show", iface], stdout=subprocess.PIPE).stdout.decode()
        ipv6_matches = re.findall(r"inet6\s+([0-9a-f:]+)\/\d+", out)
        ipv6_addrs = [addr for addr in ipv6_matches if not addr.startswith("fe80")]
        if ipv6_addrs:
            iface_ipv6[iface] = ipv6_addrs[0]
    return iface_ipv6

def create_ipv6_addresses(n, base_interface='eth0', delete_interface=True):
    """创建 n 个基于 base_interface 的 macvlan 子接口，必要时先删除已有接口。"""
    sudo_cmd = ["sudo"] if not is_root() else []
    if delete_interface:
        delete_ipv6_addresses(base_interface)
    existing_ifaces = list(get_existing_interfaces(base_interface).keys())
    interfaces = []
    for i in range(1, n + 1):
        iface = f"{base_interface}_{i}"
        if iface in existing_ifaces:
            if interface_usable(iface):
                print(f"[{iface}] 已存在，跳过创建。")
                interfaces.append(iface)
                continue
            else:
                subprocess.run(sudo_cmd + ["ip", "link", "delete", iface])
        subprocess.run(sudo_cmd + ["ip", "link", "add", "link", base_interface, iface, "type", "macvlan", "mode", "bridge"])
        subprocess.run(sudo_cmd + ["ip", "link", "set", iface, "up"])
        interfaces.append(iface)
    return interfaces

def delete_ipv6_addresses(base_interface='eth0'):
    """删除所有匹配 base_interface 的 macvlan 接口。"""
    sudo_cmd = ["sudo"] if not is_root() else []
    for iface in list(get_existing_interfaces(base_interface).keys()):
        subprocess.run(sudo_cmd + ["ip", "link", "delete", iface])

#####################################
# SOCKS5代理及IPv6绑定部分
#####################################
# 全局变量：存储接口对应的 IPv6 地址及 normal 模式下当前选中索引
iface_ipv6_dict = {}  # 格式：{接口: ipv6_address}
current_normal_ipv6_index = 0
mode = "random"  # 可选 "random" 或 "normal"

def switch_proxy_server():
    """
    直接在 Python 中切换 normal 模式下使用的 IPv6 地址。
    每次调用自动累加索引并取余，切换到下一个接口。
    仅当 mode == "normal" 时有效；否则会提示当前为随机模式。
    """
    global current_normal_ipv6_index, mode, iface_ipv6_dict
    if mode != "normal":
        print("当前代理模式为随机模式，无法直接切换。")
    else:
        # 自动累加并取余
        current_normal_ipv6_index = (current_normal_ipv6_index + 1) % len(iface_ipv6_dict)
        print(f"内部切换：当前使用的IPv6地址：{select_ipv6_address()}")

def select_ipv6_address():
    """根据当前模式选择出站 IPv6 地址。"""
    global current_normal_ipv6_index, mode, iface_ipv6_dict
    ipv6_list = list(iface_ipv6_dict.values())
    if not ipv6_list:
        raise Exception("没有可用的 IPv6 地址！")
    if mode == "random":
        return random.choice(ipv6_list)
    elif mode == "normal":
        return ipv6_list[current_normal_ipv6_index]
    else:
        return ipv6_list[0]

async def create_connection_with_local_ipv6(dest_addr, dest_port, local_ipv6):
    """建立连接前绑定指定本地 IPv6 地址。"""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setblocking(False)
    sock.bind((local_ipv6, 0))
    await loop.sock_connect(sock, (dest_addr, dest_port))
    return await asyncio.open_connection(sock=sock)

async def handle_socks_connection(reader, writer):
    """处理 SOCKS5 握手、解析请求、建立出站连接及数据转发。"""
    try:
        header = await reader.readexactly(2)
        nmethods = header[1]
        methods = await reader.readexactly(nmethods)
        if 0x00 not in methods:
            writer.write(b"\x05\xFF")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        writer.write(b"\x05\x00")
        await writer.drain()
        req = await reader.readexactly(4)
        ver, cmd, _, atyp = req
        if ver != 5 or cmd != 1:
            writer.write(b"\x05\x07\x00\x01" + b'\x00'*6)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        if atyp == 1:
            dest_addr = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            addr_len = (await reader.readexactly(1))[0]
            dest_addr = (await reader.readexactly(addr_len)).decode()
        elif atyp == 4:
            dest_addr = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            writer.write(b"\x05\x08\x00\x01" + b'\x00'*6)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        dest_port = int.from_bytes(await reader.readexactly(2), "big")
        local_ipv6 = select_ipv6_address()
        print(f"请求：{dest_addr}:{dest_port}  使用本地 IPv6：{local_ipv6}")
        try:
            remote_reader, remote_writer = await create_connection_with_local_ipv6(dest_addr, dest_port, local_ipv6)
        except Exception as e:
            print(f"建立连接失败: {e}")
            writer.write(b"\x05\x05\x00\x01" + b'\x00'*6)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        reply = b"\x05\x00\x00\x04" + socket.inet_pton(socket.AF_INET6, local_ipv6) + (0).to_bytes(2, "big")
        writer.write(reply)
        await writer.drain()
        async def pipe(src, dst):
            try:
                while (data := await src.read(4096)):
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            dst.close()
            await dst.wait_closed()
        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))
    except Exception as e:
        print(f"SOCKS处理异常: {e}")
        writer.close()
        await writer.wait_closed()

async def handle_control(reader, writer):
    """
    控制接口（仅 normal 模式启用），用于手动切换当前出站 IPv6 地址。
    格式示例：发送 "switch 1" 切换到列表中第二个地址。
    """
    global current_normal_ipv6_index, iface_ipv6_dict
    try:
        data = await reader.readline()
        cmd = data.decode().strip()
        if cmd.startswith("switch"):
            parts = cmd.split()
            if len(parts) == 2:
                try:
                    idx = int(parts[1])
                    ipv6_list = list(iface_ipv6_dict.values())
                    if 0 <= idx < len(ipv6_list):
                        current_normal_ipv6_index = idx
                        writer.write(f"已切换至 {ipv6_list[idx]}\n".encode("utf-8"))
                        print(f"Normal 模式切换：{ipv6_list[idx]}")
                    else:
                        writer.write("索引无效\n".encode("utf-8"))
                except Exception:
                    writer.write("命令格式错误\n".encode("utf-8"))
            else:
                writer.write("用法：switch <index>\n".encode("utf-8"))
        else:
            writer.write("未知命令\n".encode("utf-8"))
        await writer.drain()
    except Exception as e:
        writer.write(f"错误: {e}\n".encode("utf-8"))
    finally:
        writer.close()
        await writer.wait_closed()

#####################################
# 主程序入口及统一接口
#####################################
async def _main(ns):
    global mode, iface_ipv6_dict
    mode = ns.mode
    print("=== 创建 macvlan 接口 ===")
    create_ipv6_addresses(ns.num_interfaces, ns.base_interface, delete_interface=ns.delete_iface)
    # 等待动态 IPv6 分配（最多10秒），直到获取到预期数量的接口
    for _ in range(10):
        iface_ipv6_dict = get_existing_interfaces(ns.base_interface)
        if len(iface_ipv6_dict) >= ns.num_interfaces:
            break
        await asyncio.sleep(1)
    if len(iface_ipv6_dict) < ns.num_interfaces:
        print("未获取到足够的 IPv6 地址，请检查接口配置。")
        return
    print("可用 IPv6 接口及地址：")
    for i, (iface, ip) in enumerate(iface_ipv6_dict.items()):
        print(f"{i}: {iface} -> {ip}")
    socks_server = await asyncio.start_server(handle_socks_connection, ns.bind_address, ns.port)
    print(f"SOCKS5 代理运行在 {ns.bind_address}:{ns.port}，模式：{ns.mode}")
    tasks = [socks_server.serve_forever()]
    if ns.mode == "normal":
        control_server = await asyncio.start_server(handle_control, ns.control_bind, ns.control_port)
        print(f"控制接口运行在 {ns.control_bind}:{ns.control_port}（例如：switch 1）")
        tasks.append(control_server.serve_forever())
    else:
        print("随机模式下，不启用控制接口。")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        print("停止服务，清理接口...")
        delete_ipv6_addresses(ns.base_interface)

def run_proxy(mode="random", port=1080, bind_address="0.0.0.0",
              base_interface="eth0", num_interfaces=3, delete_iface=False,
              control_port=1081, control_bind="127.0.0.1"):
    """
    启动 SOCKS5 代理服务，封装了命令行参数解析，使用默认值。
    如果不传入任何参数，则使用默认值；可通过关键字参数自定义设置。
    """
    class NS:
        pass
    ns = NS()
    ns.mode = mode
    ns.port = port
    ns.bind_address = bind_address
    ns.base_interface = base_interface
    ns.num_interfaces = num_interfaces
    ns.delete_iface = delete_iface
    ns.control_port = control_port
    ns.control_bind = control_bind
    
    # 注册退出时的自动清理接口函数
    atexit.register(lambda: delete_ipv6_addresses(ns.base_interface))
    
    try:
        asyncio.run(_main(ns))
    except KeyboardInterrupt:
        print("收到中断信号，退出。")
        sys.exit(0)

if __name__ == "__main__":
    # 如果直接在命令行运行，则解析命令行参数
    import argparse
    parser = argparse.ArgumentParser(description="SOCKS5 Proxy with Multiple IPv6 Outbound Interfaces")
    parser.add_argument("--mode", choices=["random", "normal"], default="random", help="代理模式：random 或 normal")
    parser.add_argument("--port", type=int, default=1080, help="SOCKS5 代理监听端口")
    parser.add_argument("--bind-address", type=str, default="0.0.0.0", help="SOCKS5 代理监听地址")
    parser.add_argument("--control-port", type=int, default=1081, help="控制接口端口（仅 normal 模式）")
    parser.add_argument("--control-bind", type=str, default="127.0.0.1", help="控制接口监听地址（仅 normal 模式）")
    parser.add_argument("--base-interface", type=str, default="eth0", help="物理网卡名称")
    parser.add_argument("--num-interfaces", type=int, default=3, help="生成的 macvlan 接口数量")
    parser.add_argument("--delete-iface", action="store_true", help="启动前删除已有接口")
    args = parser.parse_args()
    run_proxy(args.mode, args.port, args.bind_address, args.base_interface, args.num_interfaces, args.delete_iface, args.control_port, args.control_bind)