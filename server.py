"""
OpenAI 兼容的 HTTP/WebSocket 服务端

提供两种接口：
- POST /v1/audio/transcriptions  非流式识别（Whisper 兼容）
- WebSocket /v1/realtime          实时流式识别（OpenAI Realtime API 兼容）
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Security,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

app = FastAPI(title="Doubao ASR - OpenAI Compatible")

API_KEY = os.environ.get("API_KEY", "")
CREDENTIAL_PATH = os.environ.get("CREDENTIAL_PATH", "./credentials.json")

security = HTTPBearer(auto_error=False)


def verify_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    """验证 Bearer Token"""
    if not API_KEY:
        return ""
    if credentials is None or credentials.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return credentials.credentials


def _gen_id(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


_asr_config = None


def get_config():
    """获取或初始化 ASR 配置（单例）"""
    global _asr_config
    if _asr_config is None:
        from doubaoime_asr import ASRConfig
        _asr_config = ASRConfig(credential_path=CREDENTIAL_PATH)
    return _asr_config


def build_session_object(
    session_id: str,
    model: str,
    input_format: str = "audio/pcm",
    rate: int = 24000,
    language: str = "zh",
) -> dict:
    """构建 OpenAI Realtime 转写会话对象"""
    return {
        "id": session_id,
        "object": "realtime.session",
        "type": "transcription",
        "model": model,
        "modalities": ["text"],
        "audio": {
            "input": {
                "format": {"type": input_format, "rate": rate},
                "noise_reduction": {"type": "near_field"},
                "transcription": {
                    "model": model,
                    "language": language,
                    "prompt": "",
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
            },
        },
        "input_audio_format": "pcm16",
        "input_audio_transcription": {"model": model},
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
        },
    }


# ── 基础路由 ──────────────────────────────────────────────


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "doubaoime-asr",
        "endpoints": ["/v1/audio/transcriptions", "/v1/realtime"],
    }


@app.get("/v1/models")
async def list_models(key: str = Security(verify_key)):
    return {
        "object": "list",
        "data": [
            {
                "id": "doubao-asr",
                "object": "model",
                "created": 1700000000,
                "owned_by": "bytedance",
            }
        ],
    }


# ── 非流式识别 ────────────────────────────────────────────


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default="doubao-asr"),
    language: str = Form(default=None),
    response_format: str = Form(default="json"),
    key: str = Security(verify_key),
):
    """Whisper 兼容的非流式语音识别"""
    try:
        audio_bytes = await file.read()
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            from doubaoime_asr import transcribe as asr_transcribe
            config = get_config()
            result = await asr_transcribe(tmp_path, config=config)
        finally:
            os.unlink(tmp_path)

        if response_format == "text":
            return result
        elif response_format in ("srt", "vtt", "verbose_json"):
            return JSONResponse(
                {"text": result, "segments": [], "language": language or "zh"}
            )
        else:
            return JSONResponse({"text": result})

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 转写会话创建 ──────────────────────────────────────────


@app.post("/v1/realtime/transcription_sessions")
async def create_transcription_session(key: str = Security(verify_key)):
    """创建转写会话，返回 client_secret 供 WebSocket 认证"""
    session_id = _gen_id("sess")
    return JSONResponse({
        "id": session_id,
        "object": "realtime.transcription_session",
        "model": "doubao-asr",
        "client_secret": {
            "value": API_KEY,
            "expires_at": int(time.time()) + 3600,
        },
        **build_session_object(session_id, "doubao-asr"),
    })


# ── 实时流式识别 WebSocket ────────────────────────────────


@app.websocket("/v1/realtime")
async def realtime_asr(
    ws: WebSocket,
    model: str = Query(default="doubao-asr"),
    api_key: str = Query(default=None),
    intent: str = Query(default="transcription"),
):
    """
    OpenAI Realtime API 兼容的 WebSocket 端点

    客户端事件：
      input_audio_buffer.append / commit / clear
      session.update / transcription_session.update
    服务端事件：
      session.created / updated
      input_audio_buffer.speech_started / speech_stopped / committed / cleared
      conversation.item.input_audio_transcription.delta / completed / failed
      error
    """
    # ── 认证 ──
    key = api_key
    if not key:
        auth_header = ws.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            key = auth_header[7:]
    if not key:
        protocol = ws.headers.get("sec-websocket-protocol", "")
        for p in (x.strip() for x in protocol.split(",")):
            if p.startswith(("fu-", "sk-")):
                key = p
                break

    if API_KEY and key != API_KEY:
        await ws.close(code=4001, reason="Invalid or missing API key")
        return

    subprotocol = None
    if "realtime" in ws.headers.get("sec-websocket-protocol", ""):
        subprotocol = "realtime"
    await ws.accept(subprotocol=subprotocol)

    # ── 会话状态 ──
    session_id = _gen_id("sess")
    current_item_id = _gen_id("item")
    total_audio_ms = 0
    audio_start_ts = 0
    input_rate = 24000
    language = "zh"

    await ws.send_json({
        "event_id": _gen_id(),
        "type": "session.created",
        "session": build_session_object(session_id, model, rate=input_rate, language=language),
    })

    audio_queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()
    transcription_task = None

    async def audio_producer():
        """从队列中读取音频块，供 transcribe_realtime 消费"""
        while not stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(audio_queue.get(), timeout=30.0)
                if chunk is None:
                    break
                yield chunk
            except asyncio.TimeoutError:
                break

    async def run_transcription():
        """运行实时转写，将结果通过 WebSocket 发回"""
        nonlocal current_item_id, audio_start_ts, total_audio_ms
        try:
            from doubaoime_asr import transcribe_realtime, ResponseType
            config = get_config()
            content_index = 0

            async for response in transcribe_realtime(audio_producer(), config=config):
                try:
                    if response.type == ResponseType.VAD_START:
                        current_item_id = _gen_id("item")
                        audio_start_ts = total_audio_ms
                        await ws.send_json({
                            "event_id": _gen_id(),
                            "type": "input_audio_buffer.speech_started",
                            "audio_start_ms": audio_start_ts,
                            "item_id": current_item_id,
                        })

                    elif response.type == ResponseType.INTERIM_RESULT:
                        await ws.send_json({
                            "event_id": _gen_id(),
                            "type": "conversation.item.input_audio_transcription.delta",
                            "item_id": current_item_id,
                            "content_index": content_index,
                            "delta": response.text,
                        })

                    elif response.type == ResponseType.FINAL_RESULT:
                        await ws.send_json({
                            "event_id": _gen_id(),
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": current_item_id,
                            "content_index": content_index,
                            "transcript": response.text,
                        })
                        content_index += 1

                    elif response.type == ResponseType.SESSION_FINISHED:
                        await ws.send_json({
                            "event_id": _gen_id(),
                            "type": "input_audio_buffer.speech_stopped",
                            "audio_end_ms": total_audio_ms,
                            "item_id": current_item_id,
                        })
                        await ws.send_json({
                            "event_id": _gen_id(),
                            "type": "input_audio_buffer.committed",
                            "item_id": current_item_id,
                        })

                    elif response.type == ResponseType.ERROR:
                        await ws.send_json({
                            "event_id": _gen_id(),
                            "type": "conversation.item.input_audio_transcription.failed",
                            "item_id": current_item_id,
                            "content_index": content_index,
                            "error": {
                                "type": "transcription_error",
                                "code": "asr_error",
                                "message": response.error_msg or "ASR transcription failed",
                            },
                        })

                except (WebSocketDisconnect, Exception):
                    break
        except Exception as e:
            try:
                await ws.send_json({
                    "event_id": _gen_id(),
                    "type": "error",
                    "error": {
                        "type": "server_error",
                        "code": "internal_error",
                        "message": str(e),
                    },
                })
            except Exception:
                pass

    try:
        transcription_task = asyncio.create_task(run_transcription())

        while True:
            try:
                raw = await ws.receive()
            except WebSocketDisconnect:
                break

            if raw["type"] == "websocket.disconnect":
                break

            # 二进制音频帧
            if "bytes" in raw:
                await audio_queue.put(raw["bytes"])
                bytes_per_ms = (input_rate * 2) / 1000
                total_audio_ms += int(len(raw["bytes"]) / bytes_per_ms)
                continue

            if "text" not in raw:
                continue

            try:
                msg = json.loads(raw["text"])
            except json.JSONDecodeError:
                await ws.send_json({
                    "event_id": _gen_id(),
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_json",
                        "message": "Failed to parse JSON message",
                    },
                })
                continue

            msg_type = msg.get("type", "")

            if msg_type == "input_audio_buffer.append":
                audio_b64 = msg.get("audio", "")
                if audio_b64:
                    try:
                        audio_bytes = base64.b64decode(audio_b64)
                        await audio_queue.put(audio_bytes)
                        bytes_per_ms = (input_rate * 2) / 1000
                        total_audio_ms += int(len(audio_bytes) / bytes_per_ms)
                    except Exception:
                        pass

            elif msg_type == "input_audio_buffer.commit":
                await audio_queue.put(None)
                await ws.send_json({
                    "event_id": _gen_id(),
                    "type": "input_audio_buffer.committed",
                    "item_id": current_item_id,
                })

            elif msg_type == "input_audio_buffer.clear":
                while not audio_queue.empty():
                    try:
                        audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await ws.send_json({
                    "event_id": _gen_id(),
                    "type": "input_audio_buffer.cleared",
                })

            elif msg_type in ("session.update", "transcription_session.update"):
                session_data = msg.get("session", {})
                audio_input = session_data.get("audio", {}).get("input", {})
                if audio_input:
                    fmt = audio_input.get("format", {})
                    if "rate" in fmt:
                        input_rate = fmt["rate"]
                    tcfg = audio_input.get("transcription", {})
                    if "language" in tcfg:
                        language = tcfg["language"]
                if "input_audio_transcription" in session_data:
                    tcfg = session_data["input_audio_transcription"]
                    if "language" in tcfg:
                        language = tcfg["language"]

                await ws.send_json({
                    "event_id": _gen_id(),
                    "type": "session.updated",
                    "session": build_session_object(
                        session_id, model, rate=input_rate, language=language
                    ),
                })

            elif msg_type == "response.create":
                await ws.send_json({
                    "event_id": _gen_id(),
                    "type": "response.created",
                })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({
                "event_id": _gen_id(),
                "type": "error",
                "error": {
                    "type": "server_error",
                    "code": "internal_error",
                    "message": str(e),
                },
            })
        except Exception:
            pass
    finally:
        stop_event.set()
        await audio_queue.put(None)
        if transcription_task:
            transcription_task.cancel()
            try:
                await transcription_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
