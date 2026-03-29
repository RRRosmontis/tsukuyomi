import asyncio
import json
import logging
import sys
import threading
import io
import urllib.request
from PIL import Image, ImageDraw
import pystray
from winotify import Notification, audio
import websockets
import easygui

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("danmaku_client")

# 全局变量
WS_URL = None
exit_event = threading.Event()
current_websocket = None          # 当前活动的 WebSocket 连接
event_loop = None                 # 主事件循环引用

# 图标URL（若无法访问，将自动降级为默认图标）
ICON_URL = "https://cdn.rrrosmontis.icu/tsukuyomi/TKYM-Singl.png"

# -------------------- 托盘图标相关 --------------------
def create_default_icon():
    size = 64
    img = Image.new('RGB', (size, size), color='white')
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, size-8, size-8], fill='blue', outline='darkblue')
    return img

def get_icon_image():
    try:
        with urllib.request.urlopen(ICON_URL, timeout=5) as response:
            img_data = response.read()
            img = Image.open(io.BytesIO(img_data))
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            logger.info("成功加载远程图标")
            return img
    except Exception as e:
        logger.warning(f"加载远程图标失败: {e}，使用默认图标")
        return create_default_icon()

def on_quit(icon, item):
    """托盘退出回调：通知主循环退出并主动关闭连接"""
    logger.info("用户通过托盘菜单退出")
    exit_event.set()          # 设置退出标志

    # 如果存在活跃的 WebSocket 连接，主动关闭以唤醒阻塞的 recv
    global current_websocket, event_loop
    if current_websocket and event_loop:
        # 从其他线程安全地调度关闭协程
        asyncio.run_coroutine_threadsafe(current_websocket.close(), event_loop)

    icon.stop()               # 停止托盘图标

def setup_tray():
    icon_image = get_icon_image()
    menu = pystray.Menu(pystray.MenuItem("退出", on_quit))
    icon = pystray.Icon("danmaku_client", icon_image, "弹幕通知客户端", menu)
    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()
    return icon, tray_thread

# -------------------- 通知功能 --------------------
async def show_notification(user: str, text: str):
    # 交换标题和内容：标题显示弹幕内容，消息显示发送者
    toast = Notification(
        app_id="弹幕通知客户端",
        title=text,
        msg=f"来自 {user}",
        duration="short"
    )
    toast.set_audio(audio.Default, loop=False)
    toast.show()
    logger.info(f"通知: {user}: {text}")

def show_startup_notification():
    # 在启动通知中增加退出提示
    toast = Notification(
        app_id="弹幕通知客户端",
        title="弹幕通知客户端已启动",
        msg="正在接收弹幕... 右键托盘图标可退出",
        duration="short"
    )
    toast.set_audio(audio.Default, loop=False)
    toast.show()
    logger.info("已发送启动通知")

# -------------------- WebSocket 消息处理 --------------------
async def handle_message(message: str):
    try:
        data = json.loads(message)
        msg_type = data.get("type")
        if msg_type == "danmaku":
            danmaku = data.get("data", {})
            user = danmaku.get("user", "匿名")
            text = danmaku.get("text", "")
            await show_notification(user, text)
        elif msg_type == "init":
            logger.info("收到 init 消息（忽略）")
        else:
            logger.debug(f"未知消息类型: {msg_type}")
    except json.JSONDecodeError:
        logger.error(f"解析消息失败: {message}")
    except Exception as e:
        logger.error(f"处理消息异常: {e}")

async def websocket_client(room_id: int):
    """WebSocket 客户端主循环，带重连，支持主动退出"""
    global current_websocket, event_loop
    while not exit_event.is_set():
        try:
            async with websockets.connect(WS_URL) as websocket:
                current_websocket = websocket
                event_loop = asyncio.get_running_loop()
                logger.info(f"已连接到 {WS_URL}")

                # 发送 join 消息
                join_msg = {
                    "type": "join",
                    "room": room_id,
                    "user": "客户端用户"
                }
                await websocket.send(json.dumps(join_msg))

                # 接收 init 消息
                init_msg = await websocket.recv()
                logger.info(f"收到 init: {init_msg[:100]}")

                # 接收弹幕消息
                async for message in websocket:
                    if exit_event.is_set():
                        break
                    await handle_message(message)

        except websockets.exceptions.ConnectionClosed:
            if not exit_event.is_set():
                logger.warning("连接已关闭，5秒后重连...")
        except Exception as e:
            if not exit_event.is_set():
                logger.error(f"连接错误: {e}，5秒后重连...")
        finally:
            # 清理全局引用（下次重连或退出时重新赋值）
            current_websocket = None

        if not exit_event.is_set():
            await asyncio.sleep(5)

    logger.info("WebSocket 客户端已退出")

# -------------------- 主函数 --------------------
def main():
    if sys.platform != "win32":
        logger.error("此客户端仅在 Windows 上支持系统通知，请使用其他方式")
        sys.exit(1)

    # 使用 easygui 获取房间号
    room_input = easygui.enterbox("请输入弹幕订阅房间号 (1-100)", "房间号")
    if not room_input:
        logger.info("用户取消输入，退出")
        sys.exit(0)

    try:
        room_id = int(room_input.strip())
        if not (1 <= room_id <= 100):
            logger.error("房间号必须在1-100之间")
            sys.exit(1)
    except ValueError:
        logger.error("房间号必须为整数")
        sys.exit(1)

    # 节点选择
    choice = easygui.buttonbox(
        "请选择接入节点：",
        "节点选择",
        choices=["[主站] api.rrrosmontis.icu", "[副站] api2.rrrosmontis.icu"]
    )
    if not choice:
        logger.info("用户取消节点选择，退出")
        sys.exit(0)

    global WS_URL
    if choice == "[主站] api.rrrosmontis.icu":
        WS_URL = "wss://api.rrrosmontis.icu/v1/live/danmaku"
    else:
        WS_URL = "wss://api2.rrrosmontis.icu:49068/danmaku"

    # 显示启动通知
    show_startup_notification()

    # 启动托盘图标
    tray_icon, tray_thread = setup_tray()

    # 运行异步主循环
    try:
        asyncio.run(websocket_client(room_id))
    except KeyboardInterrupt:
        logger.info("收到中断信号，准备退出...")
    finally:
        exit_event.set()          # 确保退出标志设置
        if tray_thread.is_alive():
            tray_thread.join(timeout=2)
        logger.info("程序已完全退出")

if __name__ == "__main__":
    main()