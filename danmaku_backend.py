import asyncio
import json
import time
import logging
from collections import deque
from typing import Set, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# ==================== 配置日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('danmaku.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("danmaku")

# ==================== 直播间类 ====================
class LiveRoom:
    def __init__(self, room_id: int):
        self.room_id = room_id
        self.start_time = time.time()
        self.timeline: list = []
        self.timeline_lock = asyncio.Lock()
        self.recent_danmaku = deque(maxlen=20)
        self.connections: Set[WebSocket] = set()
        self.connections_lock = asyncio.Lock()
        logger.info(f"创建新房间 {room_id}，开始时间: {self.start_time}")

# ==================== 全局房间管理 ====================
rooms: Dict[int, LiveRoom] = {}
rooms_lock = asyncio.Lock()

async def get_or_create_room(room_id: int) -> LiveRoom:
    async with rooms_lock:
        if room_id not in rooms:
            rooms[room_id] = LiveRoom(room_id)
        return rooms[room_id]

async def remove_connection_from_room(websocket: WebSocket):
    async with rooms_lock:
        for room in rooms.values():
            async with room.connections_lock:
                if websocket in room.connections:
                    room.connections.remove(websocket)
                    logger.info(f"从房间 {room.room_id} 移除连接，当前连接数: {len(room.connections)}")
                    break

# ==================== FastAPI 应用 ====================
app = FastAPI()

@app.websocket("/v1/live/danmaku")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info(f"新WebSocket连接: {client_host}")

    current_room: LiveRoom = None
    current_user: str = None

    try:
        async for message in websocket.iter_text():
            logger.info(f"收到消息 from {client_host}: {message[:200]}")
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "join":
                # 房间号处理
                room_id = data.get("room")
                try:
                    room_id = int(room_id)
                except (TypeError, ValueError):
                    await websocket.send_json({"type": "error", "message": "房间号必须是整数"})
                    continue

                # 如果已经加入过房间，先从原房间移除
                if current_room:
                    async with current_room.connections_lock:
                        current_room.connections.discard(websocket)

                # 加入新房间
                current_room = await get_or_create_room(room_id)
                current_user = data.get("user", "匿名")
                async with current_room.connections_lock:
                    current_room.connections.add(websocket)

                logger.info(f"客户端 {client_host} 加入房间 {room_id}，用户名 {current_user}")

                # 发送 init 消息（历史弹幕等）
                elapsed = time.time() - current_room.start_time
                init_msg = {
                    "type": "init",
                    "start_time": current_room.start_time,
                    "elapsed": elapsed,
                    "recent_danmaku": list(current_room.recent_danmaku)
                }
                await websocket.send_json(init_msg)
                continue

            elif msg_type == "danmaku":
                if current_room is None:
                    await websocket.send_json({"type": "error", "message": "请先发送 join 消息加入房间"})
                    continue

                text = data.get("text", "")
                user = data.get("user", current_user or "匿名")
                danmaku = {
                    "text": text,
                    "user": user,
                    "time": time.time()
                }

                # 记录到房间的最近弹幕和 timeline
                current_room.recent_danmaku.append(danmaku)
                async with current_room.timeline_lock:
                    current_room.timeline.append({
                        "time": time.time() - current_room.start_time,
                        "type": "danmaku",
                        "user": danmaku["user"],
                        "text": danmaku["text"]
                    })

                logger.info(f"弹幕 (房间 {current_room.room_id}): {danmaku['user']}: {danmaku['text']}")

                # 广播给同一房间的所有客户端
                await broadcast_danmaku(current_room, danmaku)
                continue

            else:
                logger.warning(f"未知消息类型: {msg_type}")
                await websocket.send_json({"type": "error", "message": f"未知消息类型: {msg_type}"})

    except WebSocketDisconnect:
        logger.info(f"WebSocket断开: {client_host}")
    except Exception as e:
        logger.error(f"WebSocket处理异常: {e}", exc_info=True)
    finally:
        if current_room:
            async with current_room.connections_lock:
                current_room.connections.discard(websocket)
                logger.info(f"从房间 {current_room.room_id} 移除连接，当前连接数: {len(current_room.connections)}")
        else:
            await remove_connection_from_room(websocket)
        logger.info(f"当前所有房间连接状态: {[(rid, len(room.connections)) for rid, room in rooms.items()]}")

async def broadcast_danmaku(room: LiveRoom, danmaku: dict):
    async with room.connections_lock:
        for conn in room.connections:
            try:
                await conn.send_json({"type": "danmaku", "data": danmaku})
            except:
                pass
    logger.info(f"广播弹幕到房间 {room.room_id}: {danmaku['user']}: {danmaku['text']}")

@app.get("/")
async def root():
    return {
        "status": "running",
        "rooms": [
            {
                "room_id": rid,
                "connections": len(room.connections),
                "timeline_count": len(room.timeline),
                "start_time": room.start_time
            } for rid, room in rooms.items()
        ]
    }

# ==================== 启动服务 ====================
if __name__ == "__main__":
    import uvicorn
    logger.info("启动 uvicorn 服务器，端口 55556")
    uvicorn.run(app, host="0.0.0.0", port=55556)