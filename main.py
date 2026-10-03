# -*- coding: utf-8 -*-

import sys
import json
import threading
import time
from pathlib import Path
from collections import defaultdict

# 导入第三方库
from loguru import logger
import requests
from ping3 import ping

# --- 全局变量和配置 ---

# 获取脚本所在目录
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "xiaomitv.log"

# 配置 loguru 日志记录器
logger.remove()  # 移除默认的控制台输出
logger.add(LOG_PATH, rotation="1 MB", retention="1 day", level="INFO", encoding="utf-8", format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")
logger.add(sys.stdout, level="INFO")
# --- 核心逻辑函数 ---

def load_config(path: Path) -> dict:
    """从 JSON 文件加载配置。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info("配置文件加载成功。")
        return config
    except FileNotFoundError:
        logger.error(f"错误：配置文件 {path} 未找到。脚本将退出。")
        exit(1)
    except json.JSONDecodeError:
        logger.error(f"错误：配置文件 {path} 格式无效。脚本将退出。")
        exit(1)

def is_host_online(host: str, timeout: int = 2) -> bool:
    """使用 ping 检测主机是否在线。"""
    try:
        # ping 函数在成功时返回延迟时间，失败或超时时返回 None 或 False
        ping_result = ping(host, timeout=timeout, unit='ms')
        if ping_result is None:
            return False
        elif ping_result == False:
            return False
        return True
    except Exception:
        # 捕获网络异常等情况
        return False

def is_host_service_online(host: str) -> bool:
    """检测小米电视遥控服务是否已启动。"""
    url = f"http://{host}:6095/request"
    params = {"action": "isalive"}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("status") == 0
    except (requests.exceptions.RequestException, ValueError):
        # 包括连接失败、超时、HTTP 错误及 JSON 解析失败
        return False

def start_app_on_tv(host: str, package_name: str) -> bool:
    """向小米电视发送启动应用的请求。"""
    url = f"http://{host}:6095/controller"
    params = {
        "action": "startapp",
        "type": "packagename",
        "packagename": package_name
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()  # 如果 HTTP 请求返回了错误状态码 (4xx 或 5xx), 则引发异常
        data = response.json()
        if data.get("status") == 0:
            logger.info(f"成功在 {host} 上启动软件包 '{package_name}'。")
            return True
        else:
            logger.warning(f"在 {host} 上启动软件包失败，返回消息: {data.get('msg')}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"无法连接到 {host}: {e}")
        return False

def handle_start_app_task(
    host: str,
    package_name: str,
    wait_time: int,
    host_states: dict,
    ping_failures: dict,
    starting_hosts: set,
    state_lock: threading.Lock,
):
    """在独立线程中执行延迟启动任务，并在成功后更新主机状态。"""
    logger.info(f"将在 {wait_time} 秒后为 {host} 启动软件包 '{package_name}'...")
    time.sleep(wait_time)

    started_successfully = start_app_on_tv(host, package_name)

    with state_lock:
        starting_hosts.discard(host)

        if started_successfully:
            # 仅在电视返回 {"status": 0, "msg": "success"} 后标记为在线
            host_states[host] = True
            ping_failures[host] = 0
            logger.info(f"{host} 的应用启动成功，状态更新为'在线'。")
        else:
            # 保持离线，下一个检测周期可再次尝试启动
            logger.warning(f"{host} 的应用启动失败，状态保持为'离线'。")

def main():
    """主函数，包含程序的核心循环。"""
    config = load_config(CONFIG_PATH)

    # 初始化主机状态
    initial_state = config.get("default", False)
    hosts = config.get("host", [])
    package = config.get("package")

    # 使用字典来跟踪每个主机的状态
    host_states = defaultdict(lambda: initial_state)
    # 使用字典来跟踪连续 ping 失败的次数
    ping_failures = defaultdict(int)

    # 正在等待或执行启动应用任务的主机，避免重复触发
    starting_hosts = set()
    state_lock = threading.Lock()

    logger.info(f"脚本启动。初始状态: {'在线' if initial_state else '离线'}")
    logger.info(f"监控主机: {hosts}")

    # 主循环
    while True:
        for host in hosts:
            with state_lock:
                current_state = host_states[host]

            is_online = is_host_online(host)

            if current_state:
                if is_online:
                    ping_failures[host] = 0
                else:
                    ping_failures[host] += 1
                    logger.warning(
                        f"{host} ping 不通，离线次数：{ping_failures[host]}"
                    )
                    if ping_failures[host] >= config.get("test", 5):
                        logger.warning(
                            f"{host} 在 {config.get('test', 5)} 个周期内 ping 不通，"
                            "状态从'在线'变更为'离线'。"
                        )
                        with state_lock:
                            host_states[host] = False
                        ping_failures[host] = 0
            else:
                if is_online:
                    if not is_host_service_online(host):
                        logger.warning(f"{host} 已在线，但遥控服务未启动。")
                        continue

                    with state_lock:
                        # 双重检查：防止启动线程刚好完成时重复创建任务
                        if host_states[host] or host in starting_hosts:
                            continue

                        # 先登记为正在启动，防止下一个轮询周期重复触发
                        starting_hosts.add(host)

                    logger.info(f"检测到 {host} 从'离线'变更为'在线'，准备启动软件包。")

                    task_thread = threading.Thread(
                        target=handle_start_app_task,
                        args=(
                            host,
                            package,
                            config.get("wait", 5),
                            host_states,
                            ping_failures,
                            starting_hosts,
                            state_lock,
                        ),
                        daemon=True,
                    )
                    task_thread.start()

        time.sleep(config.get("interval", 5))

if __name__ == "__main__":
    main()