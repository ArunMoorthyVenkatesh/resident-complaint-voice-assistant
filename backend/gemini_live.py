"""
gemini_live.py — Real-time voice proxy between the browser and Google Gemini Live.

Audio flow:
  Browser mic (PCM Int16 16kHz) → binary WS frames → Gemini Live
  Gemini Live (PCM Int16 24kHz) → binary WS frames → Browser speaker

When Gemini calls save_complaint() the backend saves to DynamoDB and sends a
JSON complaint_saved event to the browser so the UI can show the summary.

SDK: google-genai v2.x  (session.receive() iterator, send_realtime_input, send_tool_response)
"""

import os
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from dynamodb import save_complaint as db_save

logger = logging.getLogger(__name__)
SGT = timezone(timedelta(hours=8))

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

COMPLAINT_TYPES = (
    "Air-conditioning, Buildings, Cleaning, Electrical, Lighting, "
    "Fire Fighting Systems, Mechanical, Security, Parking, "
    "Water and Plumbing, Pest Control, Audio Visual, Horticulture, Others"
)

MAYA_SYSTEM_PROMPT = f"""You are Maya, a friendly AI for STE BuildCare Singapore. Talk like a real person — short, warm, natural. No robotic phrasing.

GREETING: Say "Hi, thank you for calling STE BuildCare. I'm Maya, how can I help you today?" then wait.

COLLECT in this order (one question per turn, never ask what you already know):
1. Description — any building/facility problem counts: aircon, toilet, bathroom, plumbing, electrical, lights, lift, cleaning, pest, leak, parking, etc.
2. Location — ANY mention of where the problem is counts as the location, even something vague like "living room", "kitchen", "toilet", or "my unit" — treat it as fully sufficient and do NOT ask again or press for anything more specific. Example: "the aircon in my living room is leaking" already contains the location ("living room") — skip straight to the next missing item, do not ask "where is this located?". Only ask "Where is this located?" if the caller's description contains zero location info at all. Floor is a nice-to-have — accept it if mentioned, don't ask separately for it. NEVER ask for block number or unit number.
3. Name — ask once, accept any refusal → store "Anonymous"

COMPLAINT TYPE: Auto-assign silently from description. NEVER ask the caller. Pick the closest from: {COMPLAINT_TYPES}. If unsure, use "Others".

Once all collected: say "Shall I log that for you?" → on confirmation, your very next action MUST be to call the save_complaint() function. Do NOT say "logged", "saved", "done", or speak any reference number until save_complaint() has actually been called and returned a real ID — never invent or guess a reference number. Once the function returns, speak the real ID in one short sentence.

RULES: ALWAYS reply in the exact language the caller just spoke — English, Malay, Mandarin, Tamil, Thai, Vietnamese, Indonesian, or any other language. This is critical: never default to English just because it's the most common case — match the caller's actual language every single turn, including Tamil and other less-common languages. Singlish counts as English — stay in English. Max 2 short sentences per turn. Never ask for phone number. Never ask for block or unit number. Never re-ask info already given. Yes/ok/correct/ya all count as confirmation.""".strip()

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="save_complaint",
                description="Save confirmed complaint to database. Call immediately when resident confirms the readback.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "name":           types.Schema(type=types.Type.STRING, description="Resident full name in English, or 'Anonymous'"),
                        "complaint_type": types.Schema(type=types.Type.STRING, description="Category from approved list in English"),
                        "description":    types.Schema(type=types.Type.STRING, description="Problem description in English"),
                        "location":       types.Schema(type=types.Type.STRING, description="Location/area in English, floor if mentioned"),
                    },
                    required=["name", "complaint_type", "description", "location"],
                ),
            )
        ]
    )
]

GEMINI_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=MAYA_SYSTEM_PROMPT,
    tools=TOOLS,
    thinking_config=types.ThinkingConfig(thinking_budget=0),
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
        )
    ),
    realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            silence_duration_ms=100,
            prefix_padding_ms=20,
        )
    ),
)

MODEL = "gemini-3.1-flash-live-preview"


async def handle_gemini_ws(websocket: WebSocket, user_email: str | None = None):
    """
    Proxy WebSocket between browser and Gemini Live.

    Binary frames  → raw PCM Int16 audio (browser→Gemini: 16kHz | Gemini→browser: 24kHz)
    Text frames    → JSON events: complaint_saved | error
    """
    await websocket.accept()

    if not GOOGLE_API_KEY:
        await websocket.send_text(json.dumps({"type": "error", "message": "GOOGLE_API_KEY not configured."}))
        await websocket.close(code=4500)
        return

    client = genai.Client(api_key=GOOGLE_API_KEY)

    try:
        async with client.aio.live.connect(model=MODEL, config=GEMINI_CONFIG) as session:

            # Trigger greeting; delay mic forwarding so greeting isn't barged-in on
            await session.send_client_content(
                turns=[{"role": "user", "parts": [{"text": "[Call connected]"}]}],
                turn_complete=True,
            )

            async def browser_to_gemini():
                """Forward browser mic audio to Gemini continuously."""
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            logger.info("Browser disconnected cleanly")
                            break
                        if "bytes" in msg and msg["bytes"]:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=msg["bytes"],
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )
                except WebSocketDisconnect:
                    logger.info("Browser disconnected")
                except Exception as e:
                    logger.error(f"browser_to_gemini error: {type(e).__name__}: {e}")
                    raise

            async def gemini_to_browser():
                """Forward Gemini audio to browser; handle function calls and interruptions."""
                try:
                    while True:
                        async for response in session.receive():
                            # Barge-in: Gemini detected user speaking — tell browser to stop playback
                            if response.server_content and response.server_content.interrupted:
                                await websocket.send_text(json.dumps({"type": "interrupted"}))

                            # Audio → forward as binary
                            if response.data:
                                await websocket.send_bytes(response.data)

                            if response.tool_call:
                                for fc in response.tool_call.function_calls:
                                    if fc.name == "save_complaint":
                                        args = dict(fc.args)
                                        complaint_id = await _do_save(args, user_email)
                                        await session.send_tool_response(
                                            function_responses=[
                                                types.FunctionResponse(
                                                    id=fc.id,
                                                    name=fc.name,
                                                    response={"complaint_id": complaint_id},
                                                )
                                            ]
                                        )
                                        await websocket.send_text(json.dumps({
                                            "type":           "complaint_saved",
                                            "complaint_id":   complaint_id,
                                            "complaint_data": args,
                                        }))
                        await asyncio.sleep(0.01)
                except WebSocketDisconnect:
                    logger.info("Browser disconnected")
                except Exception as e:
                    logger.error(f"gemini_to_browser error: {type(e).__name__}: {e}")
                    raise

            await asyncio.gather(browser_to_gemini(), gemini_to_browser())

    except Exception as e:
        logger.error(f"Gemini Live session error: {e}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": "Service temporarily unavailable. Please try again in a moment."}))
        except Exception:
            pass
        try:
            await websocket.close(code=1000)  # clean close so browser doesn't see "network lost"
        except Exception:
            pass


async def _do_save(args: dict, user_email: str | None = None) -> str:
    try:
        data = {
            **args,
            "email":      user_email or "",
            "created_at": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
            "source":     "gemini-live-web",
        }
        complaint_id = db_save(data)
        logger.info(f"Complaint saved via Gemini Live: {complaint_id}")
        return complaint_id
    except Exception as e:
        logger.error(f"Failed to save complaint: {e}")
        return "CMP-ERROR"
