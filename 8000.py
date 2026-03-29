import asyncio
import json
import time
import logging
import os
from collections import deque
from typing import Set, Dict, Any, List

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ==================== 配置日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('live2d.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("live2d")

# ==================== 加载Prompt ====================
PROMPT_FILE = "prompt_8000.txt"
try:
    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read().strip()
    logger.info(f"成功从 {PROMPT_FILE} 加载 System Prompt")
except Exception as e:
    logger.error(f"加载 Prompt 文件失败: {e}，使用默认提示")
    SYSTEM_PROMPT = """你是一个虚拟主播“八千代辉夜姬”的AI助手，你需要根据观众弹幕做出回应。
请以JSON格式返回，包含"reply"和"action"字段。
可用动作：泪珠, 眯眯眼, 眼泪, 笑咪咪, idle。"""

# ==================== 配置 ====================
DEEPSEEK_API_KEY = ""
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# 可用动作列表（根据模型实际表情文件）
AVAILABLE_ACTIONS = ["泪珠", "眯眯眼", "眼泪", "笑咪咪", "idle"]

# ==================== 直播间状态 ====================
class LiveRoom:
    def __init__(self):
        self.start_time = time.time()
        self.timeline: list = []
        self.timeline_lock = asyncio.Lock()
        self.recent_danmaku = deque(maxlen=20)
        self.connections: Set[WebSocket] = set()
        self.connections_lock = asyncio.Lock()
        self.danmaku_queue: asyncio.Queue = asyncio.Queue()
        self.ai_busy = False
        self.ai_lock = asyncio.Lock()
        self.last_ai_time = time.time()

        # ---------- 新增：对话历史（最大20条，每条为 {"role": "user"/"assistant", "content": str}） ----------
        self.conversation_history = deque(maxlen=20)
        self.history_lock = asyncio.Lock()          # 保护历史记录的锁

room = LiveRoom()
logger.info("直播间已创建，开始时间: %s", room.start_time)

# ==================== FastAPI 应用 ====================
app = FastAPI()

# ==================== WebSocket 端点 ====================
@app.websocket("/v1/live/8000")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info(f"新WebSocket连接: {client_host}")
    async with room.connections_lock:
        room.connections.add(websocket)
        logger.info(f"当前连接数: {len(room.connections)}")
    try:
        elapsed = time.time() - room.start_time
        async with room.timeline_lock:
            recent_actions = [ev for ev in room.timeline if ev["time"] >= elapsed - 30]
        init_msg = {
            "type": "init",
            "start_time": room.start_time,
            "elapsed": elapsed,
            "recent_actions": recent_actions,
            "recent_danmaku": list(room.recent_danmaku)
        }
        await websocket.send_json(init_msg)
        logger.info(f"向 {client_host} 发送 init 消息，elapsed={elapsed:.2f}")

        async for message in websocket.iter_text():
            logger.info(f"收到消息 from {client_host}: {message[:200]}")
            data = json.loads(message)
            if data.get("type") == "danmaku":
                danmaku = {
                    "text": data["text"],
                    "user": data.get("user", "匿名"),
                    "time": time.time()
                }
                room.recent_danmaku.append(danmaku)
                logger.info(f"弹幕入队: {danmaku['user']}: {danmaku['text']}")
                await room.danmaku_queue.put(danmaku)
                await broadcast_danmaku(danmaku)
    except WebSocketDisconnect:
        logger.info(f"WebSocket断开: {client_host}")
    except Exception as e:
        logger.error(f"WebSocket处理异常: {e}", exc_info=True)
    finally:
        async with room.connections_lock:
            room.connections.remove(websocket)
            logger.info(f"当前连接数: {len(room.connections)}")

# ==================== 广播函数 ====================
async def broadcast_danmaku(danmaku: dict):
    async with room.connections_lock:
        for conn in room.connections:
            try:
                await conn.send_json({"type": "danmaku", "data": danmaku})
            except:
                pass
    logger.info(f"广播弹幕: {danmaku['user']}: {danmaku['text']}")

async def broadcast_action(event: dict):
    async with room.connections_lock:
        for conn in room.connections:
            try:
                await conn.send_json({"type": "action", "data": event})
            except:
                pass
    logger.info(f"广播动作: time={event['time']:.2f}, action={event['action']}, text={event.get('text','')[:30]}")

# ==================== AI 调用函数（修改：支持历史上下文）====================
async def call_deepseek(history: List[dict], user_message: str) -> Dict[str, Any]:
    """
    调用 DeepSeek API，传入历史对话和当前用户消息
    :param history: 历史消息列表，每个元素为 {"role": "user"/"assistant", "content": str}
    :param user_message: 当前用户消息内容
    :return: 解析后的 JSON 响应
    """
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    # 构建 messages：system + 历史 + 当前用户消息
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)                     # 追加历史
    messages.append({"role": "user", "content": user_message})  # 当前弹幕

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.8,
        "max_tokens": 500
    }
    logger.info(f"调用DeepSeek API，当前用户消息: {user_message[:50]}... 历史长度: {len(history)}")
    start_time = time.time()
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(DEEPSEEK_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            result = resp.json()
            content = result["choices"][0]["message"]["content"]
            logger.info(f"DeepSeek返回: {content[:200]}")
            parsed = json.loads(content)
            logger.info(f"解析结果: reply={parsed.get('reply','')[:30]}, action={parsed.get('action','')}")
            return parsed
        except httpx.TimeoutException:
            logger.error("DeepSeek API 超时")
        except httpx.HTTPStatusError as e:
            logger.error(f"DeepSeek API HTTP错误: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"DeepSeek返回非JSON: {e}, 原始内容: {content if 'content' in locals() else '无'}")
        except Exception as e:
            logger.error(f"DeepSeek调用异常: {e}", exc_info=True)
    return {"reply": "嗯嗯，我在听呢~", "action": "idle"}

# ==================== 弹幕处理协程（修改：使用历史记录）====================
async def ai_worker():
    logger.info("AI工作线程启动")
    while True:
        danmaku = await room.danmaku_queue.get()
        logger.info(f"AI worker 取出弹幕: {danmaku['user']}: {danmaku['text']}")

        # 构建用户消息内容（保留用户名）
        user_content = f"{danmaku['user']}说：{danmaku['text']}"

        async with room.ai_lock:
            room.ai_busy = True

            # 获取当前历史（复制一份，避免外部修改）
            async with room.history_lock:
                current_history = list(room.conversation_history)

            # 调用 AI（传入历史 + 当前用户消息）
            ai_response = await call_deepseek(current_history, user_content)

            # 将当前用户消息和 AI 回复加入历史
            async with room.history_lock:
                room.conversation_history.append({"role": "user", "content": user_content})
                room.conversation_history.append({"role": "assistant", "content": ai_response.get("reply", "")})

            room.ai_busy = False
            room.last_ai_time = time.time()

        reply_text = ai_response.get("reply", "嗯嗯~")
        action_name = ai_response.get("action", "idle")
        if action_name not in AVAILABLE_ACTIONS:
            logger.warning(f"动作 '{action_name}' 不在允许列表中，降级为 idle")
            action_name = "idle"

        event = {
            "time": time.time() - room.start_time,
            "action": {
                "type": 1,
                "motion": action_name,
                "params": {}
            },
            "text": reply_text
        }
        async with room.timeline_lock:
            room.timeline.append(event)
            logger.info(f"时间轴添加事件: time={event['time']:.2f}, 当前事件总数={len(room.timeline)}")
        await broadcast_action(event)

# ==================== 空闲话题协程（修改：只有至少一个连接时才检测）====================
async def idle_talker():
    logger.info("空闲话题线程启动")
    while True:
        await asyncio.sleep(10)

        # ----- 新增：如果没有客户端连接，直接跳过本轮 -----
        async with room.connections_lock:
            if len(room.connections) == 0:
                logger.debug("当前无WebSocket连接，跳过空闲检测")
                continue
        # ---------------------------------------------

        now = time.time()
        if (now - room.last_ai_time > 30 and
            room.danmaku_queue.empty() and
            not room.ai_busy):
            logger.info("检测到空闲超过30秒，主动触发话题")

            async with room.ai_lock:
                room.ai_busy = True

                # 获取当前历史
                async with room.history_lock:
                    current_history = list(room.conversation_history)

                # 构造一个系统触发的用户消息（不加入历史）
                trigger_message = "现在直播间暂时安静，请你主动找一个有趣的话题聊聊，吸引观众互动。但是你可以不用也尽量不要提起“直播间没有人”这个事，只是说话题就好了"
                ai_response = await call_deepseek(current_history, trigger_message)

                # 仅将 AI 回复加入历史（主动发言，无对应用户消息）
                async with room.history_lock:
                    room.conversation_history.append({"role": "assistant", "content": ai_response.get("reply", "")})

                room.ai_busy = False
                room.last_ai_time = now

            reply_text = ai_response.get("reply", "大家怎么不说话呀？")
            action_name = ai_response.get("action", "idle")
            if action_name not in AVAILABLE_ACTIONS:
                logger.warning(f"空闲话题动作 '{action_name}' 不在列表中，降级为 idle")
                action_name = "idle"

            event = {
                "time": time.time() - room.start_time,
                "action": {
                    "type": 1,
                    "motion": action_name,
                    "params": {}
                },
                "text": reply_text
            }
            async with room.timeline_lock:
                room.timeline.append(event)
                logger.info(f"空闲话题添加事件: time={event['time']:.2f}")
            await broadcast_action(event)

# ==================== 启动事件 ====================
@app.on_event("startup")
async def startup():
    logger.info("启动后台任务")
    asyncio.create_task(ai_worker())
    asyncio.create_task(idle_talker())

# ==================== 健康检查 ====================
@app.get("/")
async def root():
    async with room.history_lock:
        history_len = len(room.conversation_history)
    return {
        "status": "running",
        "start_time": room.start_time,
        "connections": len(room.connections),
        "timeline_count": len(room.timeline),
        "queue_size": room.danmaku_queue.qsize(),
        "last_ai_time": room.last_ai_time,
        "history_len": history_len
    }

# ==================== 启动服务 ====================
if __name__ == "__main__":
    import uvicorn
    logger.info("启动 uvicorn 服务器，端口 55555")
    uvicorn.run(app, host="0.0.0.0", port=55555)