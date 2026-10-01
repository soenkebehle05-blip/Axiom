from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.schemas import ChatIn
from app.voice.pipeline import VoicePipeline
from app.voice.stt import DeepgramSTT
from app.web import browser_uid

if TYPE_CHECKING:  # avoids a circular import: main.py includes this router
    from app.main import Runtime

router = APIRouter()


@router.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    rt: Runtime = request.app.state.rt
    uid = browser_uid(request.cookies)
    agent = rt.sessions.get(body.session_id or "")
    if agent is not None and agent.owner != uid:
        raise HTTPException(404, "session_not_found")
    agent = agent or rt.new_agent(body.timezone, uid)
    tool_calls, reply = [], ""
    async for ev in agent.run_turn(body.text):
        if ev.type == "tool_call":
            tool_calls.append(ev.data)
        elif ev.type == "done":
            reply = ev.data
    return {"session_id": agent.session.id, "reply": reply, "tool_calls": tool_calls}


# --- WebSocket voice loop -------------------------------------------------------------
# Client -> server: {"type":"hello","timezone":...} | {"type":"listen_start"} | binary PCM frames while listening |
#                   {"type":"listen_stop"} | {"type":"user_partial","text":...} | {"type":"user_text","text":...} | {"type":"cancel"}
# Server -> client: JSON events {"type": "ready"|"transcript"|"ack"|"token"|"tool_call"|"tool_result"|"reply_audio_start"|
#                   "turn_end"|"audio_end"|"speculation"|"error"} and binary frames = 16-bit PCM reply audio.
@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    rt: Runtime = ws.app.state.rt

    async def raw_send(msg):
        if isinstance(msg, bytes):
            await ws.send_bytes(msg)
        else:
            await ws.send_text(json.dumps(msg))

    pipeline: VoicePipeline | None = None
    stt: DeepgramSTT | None = None

    async def on_final(text: str) -> None:
        if pipeline is not None:
            await pipeline.on_final(text)
            # Speculation may replace the agent; keep HTTP continuation and ownership checks current.
            rt.sessions[pipeline.agent.session.id] = pipeline.agent

    async def on_transcript(event: tuple) -> None:
        """Deepgram events -> UI updates + pipeline turns (same path as the browser recogniser)."""
        if pipeline is None:
            return
        kind = event[0]
        if kind == "partial":
            await raw_send({"type": "transcript", "final": False, "text": event[1]})
            await pipeline.on_partial(event[1])
        elif kind == "final" and event[1].strip():
            await raw_send({"type": "transcript", "final": True, "text": event[1], "speech_end_ago_ms": event[2]})
            await on_final(event[1])
        elif kind == "error":
            await raw_send({"type": "error", "message": f"speech recognition: {event[1]}"})

    async def stop_stt() -> None:
        nonlocal stt
        session, stt = stt, None
        if session is not None:
            await session.stop()

    try:
        while True:
            packet = await ws.receive()
            if packet.get("type") == "websocket.disconnect":
                break
            if packet.get("bytes") is not None:
                if stt is not None:
                    await stt.send_audio(packet["bytes"])
                continue
            msg = json.loads(packet.get("text") or "{}")
            kind = msg.get("type")
            if kind == "hello":
                await stop_stt()
                if pipeline is not None:
                    await pipeline.aclose()
                    pipeline = None
                try:
                    agent = rt.new_agent(msg.get("timezone"), browser_uid(ws.cookies))
                except HTTPException as exc:
                    if exc.status_code == 409:  # no calendar yet: the UI shows the upload panel and re-sends hello
                        await raw_send({"type": "calendar_required"})
                        continue
                    raise
                pipeline = VoicePipeline(
                    raw_send,
                    agent,
                    rt.tts,
                    rt.ack_audio,
                    rt.settings.ack_enabled,
                    rt.settings.speculation_enabled,
                    rt.settings.ack_delay_ms / 1000,
                )
                rt.warm_in_background()
                rt.spawn(agent.refresh_snapshot())  # so the first turn does not wait for the calendar read
                await raw_send(
                    {
                        "type": "ready",
                        "session_id": agent.session.id,
                        "tts": "cloud" if rt.tts else "browser",
                        "tts_provider": rt.tts.name if rt.tts else "browser",
                        "stt": rt.stt_provider,
                        "sample_rate": rt.settings.tts_sample_rate,
                        "model": rt.llm.model,
                        "provider": type(rt.llm).__name__.replace("LLM", "").replace("Compatible", "").lower(),
                        "speculation": rt.settings.speculation_enabled,
                    }
                )
            elif pipeline is None:
                continue
            elif kind == "listen_start" and rt.stt_provider == "deepgram":
                await stop_stt()
                stt = DeepgramSTT(
                    rt.settings.deepgram_api_key,
                    on_transcript,
                    rt.settings.deepgram_stt_model,
                    int(msg.get("sample_rate") or rt.settings.tts_sample_rate),
                    rt.settings.stt_endpointing_ms,
                    final_grace_ms=rt.settings.stt_final_grace_ms,
                )
                try:
                    await stt.start()
                except Exception as exc:
                    stt = None
                    await raw_send({"type": "error", "message": f"speech recognition unavailable: {exc}"})
            elif kind == "listen_stop":
                await stop_stt()
            elif kind == "user_partial":
                await pipeline.on_partial(msg.get("text", ""))
            elif kind == "user_text":
                await on_final(msg["text"])
            elif kind == "cancel":
                pipeline.on_cancel()
    except WebSocketDisconnect:
        pass
    except HTTPException as exc:
        await raw_send({"type": "error", "message": exc.detail})
    finally:
        await stop_stt()
        if pipeline is not None:
            await pipeline.aclose()
