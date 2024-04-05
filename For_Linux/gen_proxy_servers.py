import os
import re
import subprocess

# Global variables for proxy switch count
proxy_switch_count = 0
iface_ipv6_dict = {}

def is_root():
    return os.geteuid() == 0

def interface_usable(interface_name, skip_check=False, ipv6_address='2400:3200::1', max_retries=3):
    if skip_check:
        return True
    current_try = 0
    while current_try < max_retries:
        try:
            cmd_result = subprocess.run(["ping", "-c", "1", "-I", interface_name, ipv6_address], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            if cmd_result.returncode == 0:
                return True  # 成功ping通，直接返回True
        except subprocess.TimeoutExpired:
            print(f"Ping attempt {current_try + 1} of {max_retries} timed out. Retrying...")
        except subprocess.SubprocessError as e:
            # 捕获其他subprocess相关的异常
            print(f"An error occurred while trying to ping: {e}. Retrying...")
        current_try += 1
    return False  # 所有尝试后仍未成功，返回False

def get_existing_interfaces(base_interface='eth0'):
    cmd_result = subprocess.run(["ip", "addr", "show"], stdout=subprocess.PIPE)
    output = cmd_result.stdout.decode()
    
    # 匹配接口名称
    iface_pattern = re.compile(re.escape(base_interface) + r'_([0-9]+)@')
    iface_matches = iface_pattern.findall(output)
    
    # 构建完整的接口名称列表
    interfaces = [f"{base_interface}_{match}" for match in iface_matches]

    # 初始化字典来存储接口名称与其IPv6地址的映射
    iface_ipv6_dict = {}

    for iface in interfaces:
        # 对于每个接口，查找其IPv6地址，这里假设只提取第一个IPv6地址
        # 注意：需要确保只匹配特定接口的IPv6地址，因此使用iface作为正则表达式的一部分
        cmd_result = subprocess.run(["ip", "addr", "show", iface], stdout=subprocess.PIPE)
        output = cmd_result.stdout.decode()
        ipv6_pattern = re.compile(r"inet6\s+([0-9a-f:]+)\/\d+")
        ipv6_matches = ipv6_pattern.findall(output)
        
        # 过滤掉以"fe80"开头的IPv6地址
        ipv6_addresses = [addr for addr in ipv6_matches if not addr.startswith("fe80")]
        
        # 如果存在非链路本地的IPv6地址，只取第一个地址
        if ipv6_addresses:
            iface_ipv6_dict[iface] = ipv6_addresses[0]

    return iface_ipv6_dict

def execute_ip6tables_command(command):
    sudo_cmd = ["sudo"] if not is_root() else []
    cmd = sudo_cmd + command.split()
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def switch_proxy_server(mode='normal'):
    global proxy_switch_count
    global iface_ipv6_dict
    
    if mode == 'normal':
        if iface_ipv6_dict:
            proxy_switch_count += 1
            proxy_index = proxy_switch_count % len(iface_ipv6_dict)
            selected_interface = list(iface_ipv6_dict.keys())[proxy_index]
            ipv6_address = iface_ipv6_dict[selected_interface]
            # 清空自定义链
            execute_ip6tables_command('ip6tables -t nat -F FAKE_IPV6_CHAIN')
            # 添加SNAT规则
            execute_ip6tables_command(f'ip6tables -t nat -A FAKE_IPV6_CHAIN -j SNAT --to-source {ipv6_address}')
            
            print(f"Using interface: {selected_interface}, Connecting to: {ipv6_address}")

def create_ipv6_addresses(n, base_interface='eth0', delete_interface=True):
    sudo_cmd = ["sudo"] if not is_root() else []
    if delete_interface:
        delete_ipv6_addresses(base_interface)
    existing_interfaces = list(get_existing_interfaces(base_interface).keys())
    interfaces = []
    for i in range(1, n + 1):
        interface_name = f"{base_interface}_{i}"
        
        # Check if the interface exists, if yes, delete it first
        if interface_name in existing_interfaces:
            if interface_usable(interface_name):
                print(f"Interface {interface_name} already exists. Skipping creation.")
                interfaces.append(interface_name)
                continue
            else:
                subprocess.run(sudo_cmd + ["ip", "link", "delete", interface_name])
        
        # Now add the interface
        subprocess.run(sudo_cmd + ["ip", "link", "add", "link", base_interface, interface_name, "type", "macvlan", "mode", "bridge"])
        subprocess.run(sudo_cmd + ["ip", "link", "set", interface_name, "up"])
        #subprocess.run(sudo_cmd + ["dhclient", "-6", "-nw", interface_name])
        interfaces.append(interface_name)
    return interfaces

def delete_ipv6_addresses(base_interface='eth0'):
    sudo_cmd = ["sudo"] if not is_root() else []
    existing_interfaces = list(get_existing_interfaces(base_interface).keys())
    
    for interface_name in existing_interfaces:
        subprocess.run(sudo_cmd + ["ip", "link", "delete", interface_name])

def stop_proxy_servers(base_interface='eth0', delete_interface=True):
    # 删除流量重定向到自定义链
    execute_ip6tables_command('ip6tables -t nat -D POSTROUTING -j FAKE_IPV6_CHAIN')
    # 删除自定义链
    execute_ip6tables_command('ip6tables -t nat -X FAKE_IPV6_CHAIN')
    
    if delete_interface:
        print("正在关闭代理服务器...")
        print("删除IPv6地址...")
        delete_ipv6_addresses(base_interface)
        print("代理服务器已关闭.")
    else:
        print("正在关闭代理服务器...")
        print("代理服务器已关闭.")

def start_proxy_servers(n, mode='normal', base_interface='eth0', delete_interface=True):
    global iface_ipv6_dict
    
    interfaces = create_ipv6_addresses(n, base_interface, delete_interface)
    #获取生成的接口及IP
    iface_ipv6_dict = get_existing_interfaces(base_interface)

    if iface_ipv6_dict:
        # 删除流量重定向到自定义链
        execute_ip6tables_command('ip6tables -t nat -D POSTROUTING -j FAKE_IPV6_CHAIN')
        # 删除自定义链
        execute_ip6tables_command('ip6tables -t nat -X FAKE_IPV6_CHAIN')
        
        # 创建自定义链
        execute_ip6tables_command('ip6tables -t nat -N FAKE_IPV6_CHAIN')
        # 流量重定向到自定义链
        execute_ip6tables_command(f'ip6tables -t nat -A POSTROUTING -o {base_interface} -j FAKE_IPV6_CHAIN')
    
        if mode == 'normal':
            selected_interface = list(iface_ipv6_dict.keys())[0]
            ipv6_address = iface_ipv6_dict[selected_interface]
            # 添加SNAT规则
            execute_ip6tables_command(f'ip6tables -t nat -A FAKE_IPV6_CHAIN -j SNAT --to-source {ipv6_address}')
    
            print(f"Using interface: {selected_interface}, Connecting to: {ipv6_address}")
        elif mode == 'random':
            for index, (interface, ipv6_address) in enumerate(iface_ipv6_dict.items()):
                adjusted_probability = 1/(len(iface_ipv6_dict)-index)
                execute_ip6tables_command(f'ip6tables -t nat -A FAKE_IPV6_CHAIN -m statistic --mode random --probability {adjusted_probability} -j SNAT --to-source {ipv6_address}')