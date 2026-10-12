"""一键启动：推荐双击 scripts\\start.bat，或执行 .venv\\Scripts\\python run.py"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 无论从哪个目录启动，都先切到项目根目录，保证 app 包可正常导入
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _open_when_ready(url: str):
    deadline = time.time() + 30
    while time.time() < deadline:
        if _port_open(config.PORT):  # noqa: F821
            webbrowser.open(url)
            return
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        import uvicorn

        from app import config
    except ImportError:
        print("=" * 56)
        print("缺少运行依赖，无法用当前 Python 启动。")
        print("请使用项目自带的虚拟环境：")
        print("  方法1：双击 scripts\\start.bat（推荐）")
        print("  方法2：.venv\\Scripts\\python run.py")
        print("  方法3：先运行 scripts\\install.bat 安装依赖")
        print("=" * 56)
        input("按回车键退出…")
        sys.exit(1)

    url = f"http://127.0.0.1:{config.PORT}"
    if _port_open(config.PORT):
        print("=" * 56)
        print(f"[提示] 端口 {config.PORT} 已被占用，可能服务已经在运行。")
        print(f"请直接访问：{url}")
        print("如果打不开，请先关闭占用该端口的程序，再重新启动。")
        print("=" * 56)
        webbrowser.open(url)
    else:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
        print("=" * 56)
        print("北华校园助手正在启动……")
        print(f"网页地址：{url}")
        print(f"管理后台：{url}/admin")
        print("提示：本窗口是服务窗口，请勿关闭；关闭窗口即停止服务。")
        print("=" * 56)
        uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT, reload=False)
