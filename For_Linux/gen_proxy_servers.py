import os
import re
import subprocess
import socket
import threading
import json

# Global variables for proxy switch count, flag, threads, and sockets
proxy_switch_count = 0
global_flag = 0
threads = []
sockets = []

def is_root():
    return os.geteuid() == 0

def interface_exists(interface_name):
    cmd_result = subprocess.run(["ip", "link", "show", interface_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return cmd_result.returncode == 0

def interface_usable(interface_name, ipv6_address='2400:3200::1', max_retries=3):
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
    cmd_result = subprocess.run(["ip", "link", "show"], stdout=subprocess.PIPE)
    output = cmd_result.stdout.decode()
    pattern = re.compile(r'\d+: ' + re.escape(base_interface) + r'_([0-9]+):')
    matches = pattern.findall(output)
    
    # 构建完整的接口名称列表
    interfaces = [f"{base_interface}_{match}" for match in matches]
    return interfaces

def switch_proxy_server():
    global global_flag  # Use the global flag variable
    global_flag = 1

def create_ipv6_addresses(n, base_interface='eth0', delete_interface=True):
    if delete_interface:
        delete_ipv6_addresses(base_interface)
    interfaces = []
    sudo_cmd = ["sudo"] if not is_root() else []
    for i in range(1, n + 1):
        interface_name = f"{base_interface}_{i}"

        # Check if the interface exists, if yes, delete it first
        if interface_exists(interface_name):
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
    existing_interfaces = get_existing_interfaces(base_interface)
    sudo_cmd = ["sudo"] if not is_root() else []
    
    for interface_name in existing_interfaces:
        subprocess.run(sudo_cmd + ["ip", "link", "delete", interface_name])

def forward_data(source, destination):
    try:
        while True:
            data = source.recv(4096)
            if len(data) == 0:
                break
            destination.send(data)
    finally:
        source.close()
        destination.close()

def handle_client_with_proxy_pool(client_socket, proxy_pool):
    global proxy_switch_count  # Use the global variable
    global global_flag  # Use the global flag variable
    
    if global_flag == 1:
        proxy_switch_count += 1
        global_flag = 0
    
    proxy_index = proxy_switch_count % len(proxy_pool)
    selected_proxy_key = list(proxy_pool.keys())[proxy_index]
    selected_proxy = proxy_pool[selected_proxy_key]
    target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target_socket.setsockopt(socket.SOL_SOCKET, 25, str(selected_proxy['interface'] + '\0').encode('utf-8'))
    target_socket.connect(('127.0.0.1', selected_proxy['port']))
    print(f"Switched to new proxy: {selected_proxy_key}")
    threading.Thread(target=forward_data, args=(client_socket, target_socket)).start()
    threading.Thread(target=forward_data, args=(target_socket, client_socket)).start()

def start_proxy_pool_server(bind_ip, bind_port, proxy_pool):
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((bind_ip, bind_port))
    server_socket.listen(5)
    sockets.append(server_socket)
    
    while True:
        client_socket, _ = server_socket.accept()
        thread = threading.Thread(target=handle_client_with_proxy_pool, args=(client_socket, proxy_pool))
        thread.daemon = True
        thread.start()
        threads.append(thread)

def stop_proxy_servers(base_interface='eth0', delete_interface=True):
    if delete_interface:
        print("正在关闭代理服务器...")
        print("删除IPv6地址...")
        delete_ipv6_addresses(base_interface)
        print("代理服务器已关闭.")
    else:
        print("正在关闭代理服务器...")
        print("代理服务器已关闭.")

def start_proxy_servers(n, start_port=20000, proxy_port=10000, base_interface='eth0', delete_interface=True):
    interfaces = create_ipv6_addresses(n, base_interface, delete_interface)
    proxy_pool = {}
    for i, interface_name in enumerate(interfaces):
        port = start_port + i
        proxy_pool[interface_name] = {"ip": "127.0.0.1", "port": port, "interface": interface_name}
    
    #print(json.dumps(proxy_pool, indent=4))
    
    thread = threading.Thread(target=start_proxy_pool_server, args=("0.0.0.0", proxy_port, proxy_pool))
    thread.daemon = True
    thread.start()
    threads.append(thread)
