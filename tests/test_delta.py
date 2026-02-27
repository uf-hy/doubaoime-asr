"""
Mock 测试：验证 WebSocket 流式返回的 delta 是增量而非完整文本

模拟 ASR 返回序列：
  INTERIM "你"  → delta 应为 "你"
  INTERIM "你好" → delta 应为 "好"
  INTERIM "你好世界" → delta 应为 "世界"
  FINAL   "你好世界" → transcript 应为 "你好世界"（完整）

启动方式：python tests/test_delta.py
"""

import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, patch, MagicMock
from dataclasses import dataclass
from enum import Enum, auto

# 把项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 模拟 ASR 响应类型 ──

class MockResponseType(Enum):
    TASK_STARTED = auto()
    SESSION_STARTED = auto()
    SESSION_FINISHED = auto()
    VAD_START = auto()
    INTERIM_RESULT = auto()
    FINAL_RESULT = auto()
    HEARTBEAT = auto()
    ERROR = auto()
    UNKNOWN = auto()


@dataclass
class MockASRResponse:
    type: MockResponseType
    text: str = ""


# ── 模拟 ASR 流式返回序列 ──

MOCK_SEQUENCE = [
    MockASRResponse(type=MockResponseType.VAD_START),
    MockASRResponse(type=MockResponseType.INTERIM_RESULT, text="你"),
    MockASRResponse(type=MockResponseType.INTERIM_RESULT, text="你好"),
    MockASRResponse(type=MockResponseType.INTERIM_RESULT, text="你好世界"),
    MockASRResponse(type=MockResponseType.FINAL_RESULT, text="你好世界"),
    MockASRResponse(type=MockResponseType.SESSION_FINISHED),
]


async def mock_transcribe_realtime(audio_gen, config=None):
    """模拟 transcribe_realtime，按序列返回结果"""
    for resp in MOCK_SEQUENCE:
        yield resp
        await asyncio.sleep(0.05)


async def run_test():
    """启动 server 并通过 WebSocket 连接测试 delta 增量"""
    from websockets.asyncio.client import connect as ws_connect

    # Mock 掉 ASR 模块
    mock_module = MagicMock()
    mock_module.transcribe_realtime = mock_transcribe_realtime
    mock_module.ResponseType = MockResponseType
    mock_module.ASRConfig = MagicMock()

    with patch.dict("sys.modules", {"doubaoime_asr": mock_module}):
        # 重新 import server（确保用 mock）
        if "server" in sys.modules:
            del sys.modules["server"]

        # 清除可能缓存的 config
        import server
        server._asr_config = None
        server.API_KEY = ""  # 关闭认证

        # patch get_config 返回 mock
        mock_config = MagicMock()
        original_get_config = server.get_config
        server.get_config = lambda: mock_config

        # patch transcribe_realtime 在 server 内部的 import
        import importlib

        # 启动 uvicorn
        config = __import__("uvicorn").Config(server.app, host="127.0.0.1", port=19876, log_level="warning")
        srv = __import__("uvicorn").Server(config)
        task = asyncio.create_task(srv.serve())

        await asyncio.sleep(1.0)  # 等服务启动

        try:
            # 连接 WebSocket
            deltas = []
            transcripts = []

            async with ws_connect("ws://127.0.0.1:19876/v1/realtime") as ws:
                # 读取 session.created
                msg = json.loads(await ws.recv())
                assert msg["type"] == "session.created", f"期望 session.created，收到 {msg['type']}"
                print(f"  ✓ session.created (id={msg.get('session', {}).get('id', '?')})")

                # 发一点假音频触发转写
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": "AAAA",  # 假 base64
                }))

                # 收集所有事件
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        msg = json.loads(raw)
                        msg_type = msg["type"]

                        if msg_type == "input_audio_buffer.speech_started":
                            print(f"  ✓ speech_started")
                        elif msg_type == "conversation.item.input_audio_transcription.delta":
                            delta = msg["delta"]
                            deltas.append(delta)
                            print(f"  ✓ delta: \"{delta}\"")
                        elif msg_type == "conversation.item.input_audio_transcription.completed":
                            transcript = msg["transcript"]
                            transcripts.append(transcript)
                            print(f"  ✓ completed: \"{transcript}\"")
                        elif msg_type == "input_audio_buffer.speech_stopped":
                            print(f"  ✓ speech_stopped")
                        elif msg_type == "input_audio_buffer.committed":
                            print(f"  ✓ committed")
                            break
                        elif msg_type == "error":
                            print(f"  ✗ error: {msg}")
                            break
                    except asyncio.TimeoutError:
                        print("  ⚠ 超时，停止接收")
                        break

            # ── 验证结果 ──
            print("\n=== 验证结果 ===")

            expected_deltas = ["你", "好", "世界"]
            if deltas == expected_deltas:
                print(f"  ✅ delta 增量正确: {deltas}")
            else:
                print(f"  ❌ delta 不正确!")
                print(f"     期望: {expected_deltas}")
                print(f"     实际: {deltas}")

            if transcripts == ["你好世界"]:
                print(f"  ✅ transcript 完整文本正确: {transcripts}")
            else:
                print(f"  ❌ transcript 不正确: {transcripts}")

            # 总结
            all_pass = (deltas == expected_deltas and transcripts == ["你好世界"])
            print(f"\n{'🎉 全部通过!' if all_pass else '💥 有测试失败!'}")
            return all_pass

        finally:
            srv.should_exit = True
            await task


if __name__ == "__main__":
    print("=== Mock 测试：delta 增量验证 ===\n")
    result = asyncio.run(run_test())
    sys.exit(0 if result else 1)
