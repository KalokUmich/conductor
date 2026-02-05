import asyncio
import websockets
import json

async def test():
    # 使用 WebView 显示的 roomId（完整的 UUID）
    room_id = "79ca2f40-7871-4a1e-a3a0-b4907c69d699"  # 从 WebView header 复制
    async with websockets.connect(f"ws://localhost:8000/ws/chat/{room_id}") as ws:
        # 先接收 history
        history = await ws.recv()
        print(f"History: {history}")
        
        # 发送消息
        await ws.send(json.dumps({
            "userId": "test-user",
            "role": "engineer",
            "content": "Hello from Python!"
        }))
        
        # 接收广播
        msg = await ws.recv()
        print(f"Received: {msg}")

asyncio.run(test())