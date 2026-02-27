"""
真实 ASR 测试：连接实际的 doubaoime-asr 服务，验证流式返回的 delta 是增量

用法：
  python tests/test_realtime.py [ws_url] [api_key] [pcm_file]

默认连接 ws://localhost:8000/v1/realtime
如果提供 pcm_file，使用该文件作为音频源（24kHz 16bit mono PCM）
否则用 edge-tts 生成测试音频
"""

import asyncio
import json
import sys
import os
import base64


async def run_test(ws_url: str, api_key: str = "", pcm_file: str = ""):
    from websockets.asyncio.client import connect as ws_connect

    # 准备音频数据
    if pcm_file and os.path.exists(pcm_file):
        print(f"使用音频文件: {pcm_file}")
        with open(pcm_file, "rb") as f:
            pcm_data = f.read()
    else:
        print("未提供 PCM 文件，尝试用 edge-tts 生成...")
        import subprocess
        subprocess.run(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural",
                       "--text", "你好世界，这是一段测试语音",
                       "--write-media", "/tmp/_test_asr.mp3"], check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-y", "-i", "/tmp/_test_asr.mp3",
                       "-ar", "24000", "-ac", "1", "-f", "s16le",
                       "/tmp/_test_asr.pcm"], check=True, capture_output=True)
        pcm_file = "/tmp/_test_asr.pcm"
        with open(pcm_file, "rb") as f:
            pcm_data = f.read()

    print(f"PCM 大小: {len(pcm_data)} bytes ({len(pcm_data)/48000:.1f}s)")

    extra_headers = {}
    if api_key:
        extra_headers["Authorization"] = f"Bearer {api_key}"

    print(f"连接: {ws_url}")

    deltas = []
    transcripts = []

    async with ws_connect(
        ws_url,
        additional_headers=extra_headers,
        subprotocols=["realtime"],
    ) as ws:
        msg = json.loads(await ws.recv())
        assert msg["type"] == "session.created"
        print(f"✓ session.created\n")

        # 分块发送（每 100ms 一块）
        chunk_size = 24000 * 2 // 10  # 100ms
        chunks = [pcm_data[i:i+chunk_size] for i in range(0, len(pcm_data), chunk_size)]
        print(f"分 {len(chunks)} 块发送\n")

        done = asyncio.Event()

        async def receiver():
            while not done.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    msg = json.loads(raw)
                    t = msg["type"]

                    if t == "input_audio_buffer.speech_started":
                        print(f"  🎤 speech_started")
                    elif t == "conversation.item.input_audio_transcription.delta":
                        deltas.append(msg["delta"])
                        print(f"  📝 delta: \"{msg['delta']}\"")
                    elif t == "conversation.item.input_audio_transcription.completed":
                        transcripts.append(msg["transcript"])
                        print(f"  ✅ completed: \"{msg['transcript']}\"")
                    elif t == "input_audio_buffer.speech_stopped":
                        print(f"  🔇 speech_stopped")
                    elif t == "input_audio_buffer.committed":
                        print(f"  📦 committed")
                        done.set()
                    elif t == "error":
                        print(f"  ❌ error: {msg}")
                except asyncio.TimeoutError:
                    print("  ⏰ 超时")
                    done.set()

        recv_task = asyncio.create_task(receiver())

        for chunk in chunks:
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))
            await asyncio.sleep(0.1)

        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await asyncio.wait_for(done.wait(), timeout=15.0)
        recv_task.cancel()

    # 分析
    print(f"\n{'='*50}")
    print(f"delta 事件数: {len(deltas)}")
    print(f"completed 事件数: {len(transcripts)}")

    if deltas:
        joined = "".join(deltas)
        final = transcripts[0] if transcripts else ""
        print(f"\ndelta 拼接: \"{joined}\"")
        print(f"最终文本:   \"{final}\"")

        if joined == final:
            print(f"\n✅ delta 是增量模式")
        elif len(deltas) > 1 and all(deltas[-1].startswith(d) for d in deltas[:-1]):
            print(f"\n❌ delta 是完整文本模式（需要修复）")
        else:
            print(f"\n⚠️ 需要人工检查")

        print(f"\ndelta 序列:")
        for i, d in enumerate(deltas):
            print(f"  [{i}] \"{d}\"")


if __name__ == "__main__":
    ws_url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8000/v1/realtime"
    api_key = sys.argv[2] if len(sys.argv) > 2 else ""
    pcm_file = sys.argv[3] if len(sys.argv) > 3 else ""

    print("=== 真实 ASR 测试 ===\n")
    asyncio.run(run_test(ws_url, api_key, pcm_file))
