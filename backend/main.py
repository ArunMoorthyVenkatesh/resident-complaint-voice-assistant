"""
main.py — STE BuildCare backend

This is the FastAPI server that powers the Maya voice complaint system.

Key responsibilities (live paths, actually used by the deployed product):
  - /gemini-ws        : the real voice complaint line — proxies browser mic audio to
                         Gemini Live (see gemini_live.py) and streams the spoken reply back
  - /complaints       : read-only DynamoDB view, used by the admin dashboard

Legacy/unused paths (kept as fallback code, NOT called by the live frontend):
  - /ws               : old streaming text-chat WebSocket (Groq Llama LLM + TTS)
  - /transcribe, /speak, /process-command-unified/ : supporting endpoints for the /ws path

TTS priority chain (legacy /ws path only): Groq PlayAI (~100ms) → AWS Polly (~200ms) → edge-tts (~2s)
Language support: English, Singlish, Bahasa Melayu, Mandarin, Thai, Tamil, Indonesian, Vietnamese
"""

import os
from dotenv import load_dotenv
# Load .env from the same directory as this file, regardless of where the server is started
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import logging
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
from typing import Optional
from groq import Groq, AsyncGroq

import anthropic
import boto3
from dynamodb import init_table, save_complaint, get_all_complaints, clear_all_complaints, update_status, VALID_STATUSES
from patterns import detect_patterns
from meralion_client import MERaLiONClient
from gemini_live import handle_gemini_ws

import asyncio
import base64
import edge_tts
from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form, Body, WebSocket, WebSocketDisconnect
from botocore.exceptions import ClientError
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import uuid

# --- API keys and model config ---
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
API_KEY            = os.getenv("API_KEY")
MERALION_API_KEY   = os.getenv("MERALION_API_KEY")

GROQ_LLM_MODEL      = "llama-3.3-70b-versatile"
TRANSCRIPTION_MODEL = "whisper-large-v3-turbo"
MERALION_CLIENT     = MERaLiONClient(MERALION_API_KEY) if MERALION_API_KEY else None
# Initialize Groq clients eagerly at module level — startup events don't fire in Lambda (lifespan="off")
GROQ_CLIENT     = Groq(api_key=GROQ_API_KEY)      if GROQ_API_KEY else None  # sync — Whisper STT
GROQ_LLM_CLIENT = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None  # async — LLM

if not ANTHROPIC_API_KEY:
    print("Error: ANTHROPIC_API_KEY must be set in your .env file.")

CONVERSATION_SESSIONS = {}
SESSION_TIMEOUT = timedelta(seconds=300)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# --- Claude Configuration ---
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
claude_client = None

if ANTHROPIC_API_KEY:
    try:
        claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        logger.info(f"Claude model '{CLAUDE_MODEL}' initialized.")
    except Exception as e:
        logger.error(f"Error initializing Claude client: {e}", exc_info=True)

COMPLAINT_TYPES = (
    "Air-conditioning, Air-conditioning System, "
    "Buildings, Building Works - Internal, Building Works - External, Building Works - Water Leakage, "
    "Cleaning, Cleaning - Others, Cleaning - Toilet, "
    "Electrical, Electrical Works (Others), "
    "Lighting - Indoor Lighting, Lighting - External Lighting, "
    "Power - Low Voltage Electrical Installation, Power - High Voltage Electrical Installation, "
    "Fire Fighting Systems, "
    "Fire - Fire Fighting System (Hydrant, Hose Reel, Rising Mains and Sprinkler System), "
    "Fire - Fire Alarm and Detection System, "
    "Fire - Clean Agent, Halon and CO2 Extinguishing System, "
    "Mechanical Works (Others), "
    "Security, Electronic Parking System, "
    "Water and Plumbing - Toilet, Water and Plumbing - Others, "
    "Water Reticulation System, Water Dispenser System, "
    "Others, Audio Visual System, Horticulture / Grass cutting, Pest Control"
)




async def _transcribe_via_meralion(audio_data: bytes, filename: str) -> dict | None:
    """
    Transcribe using MERaLiON-2-10B-ASR (primary STT).
    Returns None on any failure so the caller can fall back to Groq Whisper.

    MERaLiON is trained on Singapore speech — handles Singlish, Malay-English, and
    Mandarin-English code-switching far better than generic Whisper.
    Rate-limited to 4 calls/min by the MERaLiON API; the client handles backoff.
    """
    if MERALION_CLIENT is None:
        return None
    try:
        suffix = os.path.splitext(filename)[1] or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: MERALION_CLIENT.transcribe(tmp_path, model="v2-asr")
        )
        os.unlink(tmp_path)
        if result.status_code != 200 or result.error:
            logger.warning(f"MERaLiON error {result.status_code}: {result.error}")
            return None
        raw_text = result.text.strip()
        if not raw_text:
            return {"text": "", "language": "unknown"}
        logger.info(f"MERaLiON transcribed: {raw_text[:80]!r}  ({result.latency_ms:.0f}ms)")
        return {"text": raw_text, "language": "unknown"}
    except Exception as e:
        logger.warning(f"MERaLiON transcription failed, will fall back to Groq: {e}")
        return None


async def _transcribe_via_groq(audio_data: bytes, filename: str, language: str = None, relaxed: bool = False) -> dict:
    """
    Transcribe using Groq Whisper (fallback when MERaLiON is unavailable or rate-limited).

    Filters out hallucinations (Whisper's infamous "thank you for watching" etc.) and
    very low-speech segments using the no_speech_prob scores from verbose_json.
    `relaxed=True` loosens the thresholds for non-English audio where Whisper scores higher
    even on real speech.
    """
    if GROQ_CLIENT is None:
        return {"error": "No STT client available"}
    try:
        from io import BytesIO
        audio_file = BytesIO(audio_data)
        audio_file.name = filename
        kwargs = dict(file=audio_file, model=TRANSCRIPTION_MODEL, response_format="verbose_json", temperature=0.0)
        if language:
            kwargs["language"] = language
        loop = asyncio.get_event_loop()
        transcription = await loop.run_in_executor(
            None, lambda: GROQ_CLIENT.audio.transcriptions.create(**kwargs)
        )
        segments = getattr(transcription, 'segments', []) or []
        if segments:
            no_speech_probs = [
                getattr(s, 'no_speech_prob', s.get('no_speech_prob', 0) if isinstance(s, dict) else 0)
                for s in segments
            ]
            avg_no_speech = sum(no_speech_probs) / len(no_speech_probs)
            max_no_speech = max(no_speech_probs)
            avg_thresh = 0.6 if relaxed else 0.3
            max_thresh = 0.95 if relaxed else 0.8
            if avg_no_speech > avg_thresh or max_no_speech > max_thresh:
                return {"text": "", "language": "unknown"}

        raw_text = transcription.text.strip()

        HALLUCINATIONS = {
            "thank you for watching", "thanks for watching", "thank you for listening",
            "thanks for listening", "thank you.", "thanks.", "bye.", "goodbye.",
            "please subscribe", "like and subscribe", "see you next time",
            "subtitles by", "transcribed by", "captions by",
            "음악", "♪", "🎵", "[ silence ]", "[silence]", "[music]", "[ music ]",
            "[blank_audio]", "[ blank_audio ]",
        }
        if not raw_text or raw_text.lower() in HALLUCINATIONS:
            return {"text": "", "language": "unknown"}
        if len(raw_text) <= 3 and re.match(r'^[^a-zA-Z0-9\u4e00-\u9fff]+$', raw_text):
            return {"text": "", "language": "unknown"}

        return {
            "text":     raw_text,
            "language": getattr(transcription, 'language', 'unknown'),
        }
    except Exception as e:
        logger.error(f"Groq transcription error: {e}")
        return {"error": f"Transcription failed: {str(e)}"}


async def transcribe_audio(audio_data: bytes, filename: str = "audio.mp3", language: str = None, relaxed: bool = False) -> dict:
    """Try MERaLiON first; fall back to Groq Whisper."""
    MERALION_LANGS = {None, "en", "sg", "ms", "zh", "th", "ta", "id", "vi"}
    if language in MERALION_LANGS:
        result = await _transcribe_via_meralion(audio_data, filename)
        if result is not None:
            return result
        logger.info("Falling back to Groq Whisper for transcription")
    else:
        logger.info(f"Skipping MERaLiON for lang={language} — using Groq Whisper directly")
    return await _transcribe_via_groq(audio_data, filename, language=language, relaxed=relaxed)


LANG_INSTRUCTIONS = {
    "ms": (
        "Respond ENTIRELY in Bahasa Melayu. "
        "Use natural, warm, conversational Malay — the kind a helpful service staff would use. "
        "Use 'saya' for I, 'anda' for you, 'tolong' for please, 'terima kasih' for thank you. "
        "Keep it simple and friendly, not overly formal."
    ),
    "zh": (
        "Respond ENTIRELY in Simplified Chinese (普通话/Mandarin). "
        "Use natural, warm, conversational Mandarin — the kind a helpful service staff would use in Singapore. "
        "Address the resident as '您' (formal you). Refer to yourself as '我'. "
        "Once you have the resident's name, address them ONLY as '先生' (Sir) or '女士' (Ma'am) — NEVER use their actual name again. "
        "Keep sentences short (10–15 characters per sentence). "
        "Use polite softeners like '好的', '没问题', '请问', '不好意思'. "
        "Do NOT mix in English words unless they are building/technical terms with no Chinese equivalent. "
        "Do NOT use '哈利' when referring to yourself — just say '我'. "
        "Sound human and caring, not robotic. "
        "NAME COLLECTION: Chinese names have no spaces. Ask for 姓 (surname) and 名字 (given name) in two separate steps. "
        "First ask '请问您贵姓？' (What is your surname?). Once given, ask '请问您的名字是？' (What is your given name?). "
        "Then combine surname + given name as the full name (e.g. surname 王 + given name 明 = 王明). "
        "When storing in complaint_data, transliterate to pinyin with a space between surname and given name (e.g. 王明 → 'Wang Ming', 李小华 → 'Li Xiao Hua'). A surname alone is not sufficient — always collect both. "
        "IMPORTANT: If the resident has already given a description with enough detail (any sentence describing what the problem is and where), do NOT ask for more detail — treat it as sufficient and move on. "
        "When resident confirms with '是', '是的', '对', '对的', '没错', '正确' or similar — immediately set save_complaint=true and give the closing message. Do NOT ask for confirmation again."
    ),
    "en": (
        "Respond in clear, warm, conversational English. "
        "Be empathetic and professional without being stiff."
    ),
    "sg": (
        "Respond in Singlish — the casual, warm Singapore English creole. "
        "Use natural Singlish particles like 'lah', 'lor', 'leh', 'ah', 'hor', 'sia', 'can' naturally — don't overdo it. "
        "Address the resident as 'boss', 'bro', or 'sis' after you have their name. "
        "Be friendly, direct, and efficient — Singaporean style."
    ),
}

CLOSING_TEMPLATES = {
    "ms": "Terima kasih. Aduan anda telah direkodkan. Nombor rujukan anda ialah {complaint_id}. Seseorang akan menghubungi anda tidak lama lagi. Terima kasih kerana melaporkan perkara ini, dan semoga hari anda menyenangkan.",
    "zh": "谢谢您的反馈。您的投诉已成功记录，参考编号是 {complaint_id}。我们会尽快跟进处理。如有其他问题，欢迎随时来电。祝您今天愉快！",
    "en": "Thank you. Your complaint has been logged. Your reference number is {complaint_id}. Someone will follow up with you shortly. Thank you for bringing this to our attention, and have a good day.",
    "sg": "Okay noted lah. Your complaint is logged already. Your reference number is {complaint_id}. Someone will follow up with you soon one. Thank you for letting us know, you take care ah!",
    "ta": "நன்றி. உங்கள் புகார் பதிவு செய்யப்பட்டது. உங்கள் குறிப்பு எண் {complaint_id}. விரைவில் யாரோ தொடர்பு கொள்வார்கள். நன்றி.",
    "id": "Terima kasih. Keluhan Anda telah dicatat. Nomor referensi Anda adalah {complaint_id}. Seseorang akan menghubungi Anda segera. Terima kasih telah melaporkan ini.",
    "vi": "Cảm ơn bạn. Khiếu nại của bạn đã được ghi nhận. Số tham chiếu của bạn là {complaint_id}. Sẽ có người liên hệ với bạn sớm. Cảm ơn bạn đã thông báo.",
}


def create_maya_prompt(statement: str, conversation_context: str, lang: str = "en") -> str:
    """
    Build the full LLM prompt for Maya.

    The prompt is rebuilt fresh every turn — it includes the full call transcript,
    what's already been collected, what's still missing, and the current user input.
    Language is detected naturally from the conversation; Maya follows the resident's lead.
    """
    LANG_NAMES = {
        "en": "English", "sg": "Singlish", "ms": "Bahasa Melayu",
        "zh": "Mandarin Chinese", "th": "Thai (ภาษาไทย)", "ta": "Tamil (தமிழ்)",
        "id": "Bahasa Indonesia", "vi": "Vietnamese (Tiếng Việt)",
    }
    lang_name = LANG_NAMES.get(lang, "English")
    return f"""
{conversation_context}

You are **Maya**, a sharp and friendly AI building assistant for the **Building Complaint Line**. You speak like a real person — short, warm, direct. No filler words, no long explanations.

**LANGUAGE — STRICT RULE: The resident's current message is in {lang_name}. Reply ONLY in {lang_name}. Ignore prior conversation language. Follow the resident's current language every single turn.**

If they switch languages, you switch immediately. Never stay in a previous language.

- **English:** Warm, conversational. Be empathetic and professional without being stiff.
- **Singlish:** Casual Singapore English creole — use 'lah', 'lor', 'leh', 'ah', 'hor' naturally (don't overdo it). Be friendly and direct.
- **Bahasa Melayu:** Reply ENTIRELY in Malay — natural, warm service language. Use 'saya', 'anda', 'tolong', 'terima kasih'. Never mix in English.
- **Mandarin (普通话):** Reply ENTIRELY in Simplified Chinese. Short sentences (10-15 chars). Address as 您. Do NOT use '哈利' — just say '我'. Name collection: ask for 姓 first, then 名字, combine as pinyin (王明 → Wang Ming). When resident confirms with 是/对/没错 — immediately set save_complaint=true.
- **ภาษาไทย (Thai):** Reply ENTIRELY in Thai. Warm, polite, concise. Use ครับ/ค่ะ naturally. When resident confirms with ใช่/ถูกต้อง/โอเค — immediately set save_complaint=true.
- **தமிழ் (Tamil):** Reply ENTIRELY in Tamil. Respectful, warm tone. Use நன்றி, தயவுசெய்து naturally. When resident confirms with ஆம்/சரி/ஒப்புக்கொள்கிறேன் — immediately set save_complaint=true.
- **Bahasa Indonesia:** Reply ENTIRELY in Indonesian. Polite, professional. Use saya/Anda/tolong/terima kasih. When resident confirms with ya/benar/setuju — immediately set save_complaint=true.
- **Tiếng Việt (Vietnamese):** Reply ENTIRELY in Vietnamese. Warm, respectful. Use tôi/bạn/vui lòng/cảm ơn. When resident confirms with vâng/đúng/xác nhận — immediately set save_complaint=true.
- **Code-switching:** If they mix languages naturally, mirror that style.

Set **"reply_lang"** in your JSON to: `"en"`, `"sg"`, `"ms"`, `"zh"`, `"th"`, `"ta"`, `"id"`, or `"vi"` — whichever you replied in.

Your sole role is to help residents log building complaints. The only required piece of information is a clear **description** of the problem. Name is optional — ask once and move on if they decline.

**YOUR GOAL:** Collect a description of the problem, then confirm and save. Name is nice to have but not required. The complaint type is auto-detected from the description; never ask the resident for it.

**COMPLAINT TYPE AUTO-DETECTION:** Once you have a description, silently map it to the closest category from this list: {COMPLAINT_TYPES}. If no category closely matches, set complaint_type to "Others (original text)". Never ask the resident about the complaint type.

**DECISION LOGIC — follow this exactly on every turn:**
0. **READ THE FULL TRANSCRIPT ABOVE FIRST.** Extract every piece of information the resident has already stated — name, description, anything. Never ask for something that appears anywhere in the transcript.
1. Check what you already have: name, description, location (from the full transcript and current input).
2. Extract anything present in the current message — do NOT ignore information the resident has already provided.
3. If description is missing → ask for it. This is the only mandatory field.
4. If description is present but name has not been asked yet → ask for their name once. If they decline, say they prefer to remain anonymous, or give a dismissal (e.g. "skip", "no", "don't want to") → accept it, store name as "Anonymous", and move on immediately. Do NOT ask again.
5. Once description (5+ words) is collected → check if a location was already mentioned anywhere (in the description itself or elsewhere in the transcript) and use that — do NOT ask again. Only if no location appears anywhere → ask "Where is this located?". Floor is a nice-to-have — accept it if mentioned, never ask for it separately. NEVER ask for block number or unit number.
6. Once all collected (or skipped) → read back whatever was collected and ask "Is all this information correct?" (this is the confirmation step). Do NOT ask "anything else" first — go straight to confirmation.
7. Once resident confirms → set save_complaint=true and give the closing message.

**EXAMPLES of correct short replies:**
- Resident: "Hi I'm Sarah Tan, the aircon in my office on level 4 has been leaking water for 3 days" → location already given (level 4) → reply: "Got it, thanks Sarah. Anything else, or shall I log this?"
- Resident: "the toilet on the 5th floor is clogged" → location already given (5th floor) → reply: "Got it. May I have your name?"
- Resident: "Hello" → reply: "Hi! What's the issue you'd like to report?"
- Resident: "there's a water leak on level 3" → reply: "May I have your name?"
- Resident: "I don't want to give my name" → reply: "No worries. Anything else to add, or shall I log this?"
- Resident confirms → reply: "Done! Your reference number is {{complaint_id}}. We'll follow up soon."
BAD (too long): "Thank you for sharing that! I understand you're experiencing an aircon issue. Could you please tell me your name so I can log this complaint properly?"
GOOD: "May I have your name?"


**CRITICAL RULES:**

**Collecting information:**
- **NEVER ask for the complaint type — detect it yourself from the description.**
- **NEVER ask for a phone number or contact number. Phone collection has been removed from this system entirely.**
- **NEVER ask for block number or unit number. Just ask "Where is this located?" — floor is optional, accept it only if the resident volunteers it, never ask for it separately. If a location is already mentioned anywhere — including inside the problem description itself (e.g. "the aircon on level 4 is leaking") — do NOT ask for location at all, it's already collected.**
- **NEVER ask for information the resident has already provided. Extract it yourself and move on.**
- Check conversation history first. Never re-ask for information already collected.
- Only ask for the FIRST missing piece of information, then stop.
- Once you have the resident's name, do NOT use it or any formal address (no "Sir", "Ma'am", "boss", "bro", "sis", "Encik", "Puan", "先生", "女士"). Just speak naturally and conversationally.
- **A description is SUFFICIENT if it is 5 or more words AND conveys what the problem is. Once a sufficient description is given, do NOT ask technical follow-up questions like "where is it located" or "how long has it been occurring" if those details are already in the description. Proceed to step 5 (ask for location).**
- **If and only if the description is fewer than 5 words (e.g. "bulb spoil", "leaking", "got problem") — ask for more detail: where it is, how long it has been happening, or how severe it is. Ask once only.**
- **If the description is very long (over 50 words) — summarise it into a clear, concise professional sentence of 20–30 words when populating the description field. Do not paste the resident's entire paragraph.**

**Name handling (optional):**
- **Name is optional. Ask once. If the resident declines, says "skip", "anonymous", "don't want to", or anything dismissive → store name as "Anonymous" and move on immediately. Never insist.**
- If a name is given, accept it as-is — any name, single word or multiple words (e.g. "John", "Sarah Tan", "Encik Ali"). Do NOT ask for first name or last name separately. Accept and move on immediately.
- Accept hyphenated names, names with "bin"/"binte"/"s/o"/"d/o", and multi-part names as valid.
- **If the resident provides clearly non-human input for a name (e.g. "98765432", "john@email.com") — politely ask once if they have a name to share, or if they'd prefer to remain anonymous.**
- Chinese names: ask for surname then given name in two steps. Store as pinyin (e.g. 王明 → Wang Ming).

**Identity and contact:**
- **If the resident asks whether you are a real person or a bot (e.g. "are you real?", "is this a bot?", "am I talking to a human?") — answer honestly: you are an AI assistant. Then continue the complaint intake flow without dwelling on it.**
- **If the resident asks to speak to a real person, a human agent, or a manager (e.g. "I want to talk to someone", "can I speak to a manager?") — explain that this is an unmanned AI complaint line available 24/7. Let them know they can reach a staff member through the following channels: (1) Email the building management office, (2) Visit the management office in person during office hours, (3) Call the main office line during staffed hours. Then offer to continue lodging the complaint on their behalf.**

**Off-topic and cancellation:**
- **Your ONLY function is to log building complaints. You cannot answer questions about anything else — not weather, news, recipes, general knowledge, building rules, policies, fees, opening hours, or any topic outside of collecting a complaint description.**
- **If the resident says or asks ANYTHING unrelated to lodging a building complaint — do NOT engage with it at all. Give a single short sentence declining (e.g. "I can only help with building complaints.") and immediately ask them to describe their building issue. Do this every single time they digress, no matter how many times it happens.**
- **Never get drawn into a conversation. Never explain, elaborate, or answer off-topic questions even partially. Always redirect back.**
- **When redirecting off-topic: ALWAYS set save_complaint=false and set all complaint_data fields to null. NEVER set save_complaint=true unless the resident has confirmed an actual building complaint.**
- **If the resident says they want to cancel, stop, or no longer want to lodge a complaint (e.g. "never mind", "cancel", "forget it", "I don't want to") — acknowledge politely and give a brief closing message. Do NOT set save_complaint=true. Do NOT ask for more information.**

**Gibberish handling:**
- **If the resident's input appears to be gibberish (random characters, keyboard mashing, meaningless strings with no real words — e.g. "asdfghjkl", "zxcvbnm", "qqqqqq") — set is_gibberish=true in your response. Ask them to please type their message clearly. After 3 consecutive gibberish inputs (tracked in GIBBERISH COUNT above), set end_conversation=true and give a polite closing message saying you were unable to assist.**
- A short or blunt message (e.g. "yes", "ok", "hi", "aircon") is NOT gibberish — only random character strings with no meaning count.

**After a complaint is saved:**
- **If the resident says they have another complaint after one has already been saved (e.g. "I have another issue", "one more thing") — treat it as a fresh complaint. Ask for a description of the new problem. Do NOT re-ask for their name — it is already known.**

**Confirmation handling:**
- **Do NOT treat "yes", "correct", "ok", or similar words as confirmation unless you have already asked the confirmation question in the immediately preceding turn. If you have not yet asked "Is all this information correct?", a "yes" from the resident is just an acknowledgement — continue the flow normally.**
- Never loop or repeat a question you have already asked. If information was provided, treat it as collected and proceed.
- Once description and location are collected, go straight to the confirmation step — do NOT ask "is there anything else".
- When summarising for confirmation, clearly state only the fields that were actually collected (skip name if Anonymous).
- After the resident confirms, set save_complaint=true and give the closing message below.
- **BREVITY — most important rule:** Max **1-2 short sentences per reply**. Never pad. No "I understand", no "Thank you for sharing", no "Great!". Ask exactly ONE thing per turn and stop. Silence is better than filler.

**OUTPUT FORMAT — return ONLY valid JSON. ALL fields are mandatory. reply_lang MUST be the first field:**
{{
  "reply_lang": "en | sg | ms | zh | th | ta | id | vi",
  "reply": "YOUR RESPONSE IN THE RESIDENT'S LANGUAGE",
  "openEndedValue": null,
  "save_complaint": false,
  "is_gibberish": false,
  "end_conversation": false,
  "complaint_data": {{
    "name": "Resident full name translated/romanised into ENGLISH, else null",
    "complaint_type": "Complaint type from the approved list in ENGLISH, else null",
    "description": "ALL description details in ENGLISH — translate from any language. Combine everything mentioned across the conversation into one clear summary. Never set to null if any problem details have been mentioned.",
    "location": "Location/area in ENGLISH, floor if volunteered, else null"
  }}
}}

CRITICAL STORAGE RULE: Every field in complaint_data MUST be in ENGLISH regardless of the language spoken. Translate descriptions, names, and locations word-for-word into English before storing. The reply field stays in the resident's language — ONLY complaint_data must be in English.
IMPORTANT: Always populate complaint_data with whatever has been collected so far — even if only the name is known. Never set complaint_data to null once any field has been provided.
IMPORTANT: Never ask the resident to describe something they have already described. If description is already in the collected fields above, do NOT ask for it again — move to the next missing field or go to confirmation.

When the resident has confirmed all details, set save_complaint=true and give a **single short closing sentence** in the resident's language. Include the literal text `{{complaint_id}}` as a placeholder — the backend replaces it with the real reference number. Do NOT invent a number.

Example closing (one sentence only):
- English: "All done — your reference number is {{complaint_id}}, and someone will follow up soon."
- Singlish: "Okay logged lah — reference number {{complaint_id}}, they will follow up one."
- Malay: "Selesai — nombor rujukan anda {{complaint_id}}, kami akan hubungi anda."
- Chinese: "完成了，参考编号是 {{complaint_id}}，我们会尽快跟进。"
- Thai: "เสร็จแล้วค่ะ — หมายเลขอ้างอิงของคุณคือ {{complaint_id}} เราจะติดตามโดยเร็ว"

Return this structure:
{{
  "reply_lang": "en | sg | ms | zh | th | ta | id | vi",
  "reply": "closing message in resident's language with {{complaint_id}} placeholder",
  "openEndedValue": null,
  "save_complaint": true,
  "is_gibberish": false,
  "end_conversation": false,
  "complaint_data": {{
    "name": "resident full name in ENGLISH",
    "complaint_type": "category from the list in ENGLISH",
    "description": "full description of the problem in ENGLISH",
    "location": "location/area in ENGLISH, floor if volunteered"
  }}
}}

**Now respond to:** `{statement}`
""".strip()


# --- Core LLM call ---
async def get_ai_command_response(
    command_text: str,
    session_id: str = None,
    lang: str = "en",   # kept as fallback for closing template if reply_lang is missing
    source_label: str = "web-chat",
) -> dict:
    """
    Main LLM call: Groq Llama 3.3 70B primary (fast, ~500 tok/s), Claude Haiku fallback.

    The LLM responds in a structured JSON format so we can extract:
    - reply: what Maya says aloud
    - save_complaint: true when the complaint is confirmed and should be saved to DynamoDB
    - complaint_data: name, complaint_type, description, location
    - is_gibberish / end_conversation: edge-case handling flags

    After parsing the JSON, this function:
    1. Updates the session's collected field cache (so context is never re-asked)
    2. If save_complaint=true, saves to DynamoDB and replaces {complaint_id} in the reply
    3. Returns the full parsed dict (the WebSocket handler strips 'reply' and streams TTS)
    """
    if not GROQ_LLM_CLIENT and not claude_client:
        return {
            "command": None,
            "reply": "AI service is not available.",
            "openEndedValue": None,
            "error": "AI_NOT_AVAILABLE",
        }

    session = get_or_create_session(session_id)
    session.add_message("user", command_text)
    conversation_context = session.get_context_for_gemini(command_text)
    prompt = create_maya_prompt(command_text, conversation_context)

    try:
        raw_text = None

        # Primary: Groq Llama 3.3 70B — ~500 tok/s, sub-100ms for short complaint replies
        if GROQ_LLM_CLIENT:
            try:
                resp = await GROQ_LLM_CLIENT.chat.completions.create(
                    model=GROQ_LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                    temperature=0.2,
                )
                raw_text = resp.choices[0].message.content.strip()
            except Exception as groq_err:
                logger.warning(f"Groq LLM failed, falling back to Claude: {groq_err}")

        # Fallback: Claude Haiku (used if Groq fails or is at capacity)
        if raw_text is None and claude_client:
            resp = await claude_client.messages.create(
                model=CLAUDE_MODEL, max_tokens=1024, temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
            )
            if resp.content and resp.content[0].type == "text":
                raw_text = resp.content[0].text.strip()

        if not raw_text:
            return {
                "command": None,
                "reply": "I received an unexpected response. Please try again.",
                "openEndedValue": None,
                "error": "EMPTY_AI_RESPONSE",
            }

        try:
            parsed = _process_raw_response(raw_text, session, session_id, lang, source_label)
            return parsed
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse AI JSON: {e}. Raw: '{raw_text}'")
            return {
                "command": None,
                "reply": "I'm sorry, I had trouble processing that. Could you please try again?",
                "openEndedValue": None,
                "error": "INVALID_AI_JSON_RESPONSE",
            }

    except Exception as e:
        logger.error(f"AI processing error: {e}", exc_info=True)
        return {
            "command": None,
            "reply": f"An internal error occurred: {str(e)}",
            "openEndedValue": None,
            "error": "INTERNAL_AI_PROCESSING_ERROR",
        }


def _process_raw_response(raw_text: str, session, session_id: str, lang: str, source_label: str = "web-chat") -> dict:
    """
    Parse the LLM JSON response and apply all session/complaint side-effects.
    Shared by both the non-streaming (get_ai_command_response) and streaming
    (stream_ai_response) paths so the logic lives in exactly one place.
    """
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        start = raw_text.find('{')
        end   = raw_text.rfind('}')
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(raw_text[start:end+1])
        else:
            raise

    if session:
        if parsed.get("is_gibberish"):
            session.gibberish_count += 1
        else:
            session.gibberish_count = 0

    if parsed.get("complaint_data") and session:
        session.update_collected(parsed["complaint_data"])

    if parsed.get("save_complaint") and session and not session.confirmed:
        session.confirmed = True

    cd = parsed.get("complaint_data") or {}
    if session:
        for f in ["name", "complaint_type", "description", "location"]:
            if not cd.get(f) or str(cd[f]).strip().lower() in ("null", ""):
                if session.collected.get(f):
                    cd[f] = session.collected[f]

    has_description = bool(cd.get("description") and str(cd["description"]).strip().lower() not in ("null", ""))
    if parsed.get("save_complaint") and has_description:
        complaint_id = "CMP-" + str(uuid.uuid4())[:8].upper()
        try:
            data = dict(cd)
            data["created_at"] = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
            data["source"]     = f"{source_label} session:{session_id}"
            complaint_id = save_complaint(data)
            logger.info(f"Complaint saved: id={complaint_id}")
        except Exception as e:
            logger.error(f"Error saving complaint: {e}")
        reply_lang    = parsed.get("reply_lang", lang)
        closing_lang  = "en" if reply_lang == "sg" else reply_lang
        if closing_lang not in CLOSING_TEMPLATES:
            closing_lang = "en"
        parsed["reply"]        = CLOSING_TEMPLATES[closing_lang].replace("{complaint_id}", complaint_id)
        parsed["complaint_id"] = complaint_id

    session.add_message("assistant", parsed.get("reply", ""))
    return parsed


async def stream_ai_response(command_text: str, session_id: str = None, lang: str = "en"):
    """
    Async generator — streams Groq LLM and yields sentences for immediate TTS.

    Yields:
      {"type": "sentence", "text": str, "lang": str}  — one per sentence, as detected
      {"type": "done", ...full parsed response...}     — last item always

    The reply_lang field is now first in the prompt JSON so we know the TTS
    voice within the first few tokens, before the reply text begins.

    Falls back to non-streaming (get_ai_command_response) if Groq streaming fails.
    """
    session  = get_or_create_session(session_id)
    session.add_message("user", command_text)
    prompt   = create_maya_prompt(command_text, session.get_context_for_gemini(command_text), lang=lang)

    full_text       = ""
    tts_lang        = "en"
    lang_found      = False
    reply_start_idx = -1   # index in full_text where reply value chars begin
    parsed_to       = 0    # chars of reply value already processed
    pending         = ""   # sentence being built
    escape_next     = False
    reply_closed    = False
    any_yielded     = False

    try:
        if not GROQ_LLM_CLIENT:
            raise RuntimeError("no groq client")

        stream = await GROQ_LLM_CLIENT.chat.completions.create(
            model=GROQ_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.2,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            full_text += delta

            # ── 1. Extract reply_lang (first field in our reordered JSON) ──────────
            if not lang_found:
                m = re.search(r'"reply_lang"\s*:\s*"([a-z]{2,5})"', full_text)
                if m:
                    rl       = m.group(1)
                    tts_lang = "en" if rl == "sg" else rl
                    lang_found = True

            # ── 2. Locate start of the reply string value ─────────────────────────
            if reply_start_idx == -1:
                m = re.search(r'"reply"\s*:\s*"', full_text)
                if m:
                    reply_start_idx = m.end()
                    parsed_to       = 0

            # ── 3. Parse reply chars and yield complete sentences ─────────────────
            if reply_start_idx != -1 and not reply_closed and lang_found:
                new_chars = full_text[reply_start_idx + parsed_to:]
                parsed_to += len(new_chars)

                for ch in new_chars:
                    if escape_next:
                        escape_next = False
                        if   ch == 'n': pending += ' '
                        elif ch == '"': pending += '"'
                        elif ch == '\\': pending += '\\'
                        continue

                    if ch == '\\':
                        escape_next = True
                        continue

                    if ch == '"':
                        # Closing quote — flush whatever is left
                        reply_closed = True
                        s = pending.strip()
                        if s:
                            yield {"type": "sentence", "text": s, "lang": tts_lang}
                            any_yielded = True
                        pending = ""
                        break

                    pending += ch

                    # Sentence boundary: . ! ? and we have a real sentence
                    if ch in '.!?' and len(pending.strip()) > 5:
                        s = pending.strip()
                        yield {"type": "sentence", "text": s, "lang": tts_lang}
                        any_yielded = True
                        pending = ""

        # Flush any partial sentence at end of stream (no closing quote yet)
        if pending.strip() and not reply_closed:
            yield {"type": "sentence", "text": pending.strip(), "lang": tts_lang}
            any_yielded = True

    except Exception as e:
        logger.warning(f"LLM streaming failed ({e}), falling back to non-streaming")
        full_text = ""

    # ── Parse full JSON and run session/complaint side-effects ────────────────────
    try:
        if full_text:
            parsed = _process_raw_response(full_text, session, session_id, lang)
        else:
            # Streaming failed — fall back to a fresh non-streaming call
            # (session already has the user message added, so undo it first)
            session.chat_history = session.chat_history[:-1]
            parsed = await get_ai_command_response(command_text, session_id, lang)
    except Exception as e:
        logger.error(f"Response processing failed: {e}", exc_info=True)
        parsed = {"reply": "Sorry, something went wrong.", "reply_lang": lang}

    # If streaming yielded nothing (e.g. reply was empty), emit sentences from parsed
    if not any_yielded:
        fb_tts = "en" if parsed.get("reply_lang", lang) == "sg" else parsed.get("reply_lang", lang)
        for s in split_into_sentences(parsed.get("reply", "")):
            yield {"type": "sentence", "text": s, "lang": fb_tts}

    yield {"type": "done", **parsed}


# --- FastAPI App Setup ---
app = FastAPI(title="Building Complaint AI API", version="2.0.0")


@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    if request.method == "OPTIONS" or \
       request.url.path in ["/", "/health", "/docs", "/redoc", "/openapi.json"] or \
       request.url.path.startswith("/ws"):
        return await call_next(request)

    api_key = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    if api_key.startswith("Bearer "):
        api_key = api_key[7:]

    if not api_key or api_key != API_KEY:
        logger.warning(f"Unauthorized from {request.client.host if request.client else 'unknown'}")
        return JSONResponse(
            status_code=401,
            content={"error": "UNAUTHORIZED", "message": "Valid X-API-Key header required."},
        )
    return await call_next(request)


async def _session_cleanup_loop():
    while True:
        await asyncio.sleep(60)
        cleanup_expired_sessions()


@app.on_event("startup")
async def startup_event():
    init_table()
    _init_polly()
    asyncio.create_task(_session_cleanup_loop())


@app.get("/health")
async def health_check():
    from dynamodb import _get_table
    try:
        _get_table().load()
        return {"status": "ok", "dynamodb": "connected"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", "dynamodb": "unavailable"})


origins = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:5176",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "https://18-143-155-12.sslip.io",
    "https://admin.18-143-155-12.sslip.io",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


# --- In-memory session store ---
# Sessions are keyed by the frontend-generated session_id and expire after 5 minutes of inactivity.
# Each session tracks the full chat history, what fields have been collected so far,
# and how many consecutive gibberish inputs have been received.
class ConversationSession:
    TRACKED_FIELDS  = ["name", "complaint_type", "description", "location"]
    REQUIRED_FIELDS = ["complaint_type", "description", "location"]   # name is optional
    OPTIONAL_FIELDS = {"name"}

    def __init__(self, session_id: str):
        self.session_id     = session_id
        self.chat_history   = []
        self.created_at     = datetime.now()
        self.last_activity  = datetime.now()
        self.collected      = {}
        self.confirmed      = False
        self.gibberish_count = 0

    def add_message(self, role: str, content: str):
        self.chat_history.append({
            "role":      role,
            "content":   content,
            "timestamp": datetime.now().isoformat(),
        })
        self.last_activity = datetime.now()

    @staticmethod
    def _is_valid_name(val: str) -> bool:
        # CJK name: valid if 2+ Chinese characters
        if re.search(r'[\u4e00-\u9fff]', val):
            return len(re.findall(r'[\u4e00-\u9fff]', val)) >= 2
        # All other names: accept any non-empty string
        return len(val.strip()) >= 1

    def _maya_has_asked_for(self, field: str) -> bool:
        """Returns True if Maya has already asked for this optional field in the chat history."""
        if field == "name":
            patterns = ["your name", "may i have your name", "what is your name", "please provide your name",
                        "名字", "姓名", "贵姓", "名字是", "您叫", "请问您的名", "请问您贵",
                        "nama anda", "nama awak", "boleh tahu nama",
                        "your full name", "can i have your name", "mind sharing your name",
                        "could i get your name",
                        ]
        else:
            patterns = []
        for msg in self.chat_history:
            if msg["role"] == "assistant":
                content_lower = msg["content"].lower()
                if any(p in content_lower for p in patterns):
                    return True
        return False

    def update_collected(self, complaint_data: dict):
        for field in self.TRACKED_FIELDS:
            val = complaint_data.get(field)
            if val and str(val).strip() and str(val).strip().lower() not in ("null", ""):
                self.collected[field] = str(val).strip()

    def get_context_for_gemini(self, current_statement: str) -> str:
        context = ""
        if self.chat_history:
            context += "\n**FULL CALL TRANSCRIPT (append-only — every message since call started):**\n"
            context += "⚠ BEFORE asking ANY question, scan this entire transcript. If the answer is already here, DO NOT ask again.\n"
            for msg in self.chat_history[-10:]:
                role_display = "Resident" if msg["role"] == "user" else "Maya"
                context += f"{role_display}: {msg['content']}\n"



        if self.collected:
            context += "\n**ALREADY COLLECTED — DO NOT ASK AGAIN:**\n"
            for k, v in self.collected.items():
                context += f"  ✓ {k}: {v}\n"
            if "name" in self.collected:
                context += "  ⚠ Name is collected — do NOT use it or any formal address. Just speak naturally.\n"
            missing = [f for f in self.REQUIRED_FIELDS if f not in self.collected]
            # Determine which optional fields still need to be asked
            optional_unasked = [
                f for f in ("name",)
                if f not in self.collected and not self._maya_has_asked_for(f)
            ]
            if missing:
                context += f"\n**STILL NEEDED:** {', '.join(missing)}\n"
            elif optional_unasked:
                context += f"\n**ASK ONCE (optional — move on if declined):** {', '.join(optional_unasked)}\n"
            elif self.confirmed:
                context += (
                    "\n**RESIDENT HAS CONFIRMED. YOU MUST NOW:**\n"
                    "Set save_complaint=true and return the closing message with the reference ID.\n"
                    "Do NOT ask for confirmation again.\n"
                )
            else:
                name = self.collected.get("name", "")
                ctype = self.collected.get("complaint_type", "")
                desc = self.collected.get("description", "")

                # Check if Maya already asked for confirmation in the last turn
                last_harry = next(
                    (m["content"] for m in reversed(self.chat_history) if m["role"] == "assistant"), ""
                )
                already_asked = (
                    any(p in last_harry.lower() for p in [
                        "all this information correct", "is this correct", "is that correct",
                        "betul", "maklumat ini betul", "correct, ah", "correct ah",
                        "information correct", "details correct", "that correct",
                        # Malay variants
                        "adakah maklumat", "maklumat ini tepat", "semua betul",
                        # Singlish variants
                        "all correct", "everything correct", "confirm ah", "confirm lah",
                    ]) or
                    # Chinese — check original (not lowercased) since Chinese chars don't change
                    any(p in last_harry for p in [
                        "是否正确", "信息正确吗", "这些信息对吗", "以上信息对吗", "以上对吗",
                        "资料正确吗", "确认一下", "这样对吗", "信息无误吗", "以上正确吗",
                        "这些信息都正确吗", "都正确吗", "信息都正确吗", "内容正确吗",
                        "这样正确吗", "以上内容正确吗", "请确认", "是否无误", "资料都正确吗",
                    ])
                )

                location = self.collected.get("location", "")
                if already_asked:
                    context += (
                        f"\n**CONFIRMATION PENDING — Maya already asked 'Is all this information correct?'**\n"
                        f"Confirmed details: Name: {name} | Complaint Type: {ctype} | Description: {desc} | Location: {location}\n"
                        f"The resident just replied. Their answer is the CURRENT INPUT below.\n"
                        f"If their reply is any form of agreement (yes, correct, right, okay, all correct, lah, lor, etc.):\n"
                        f"  - Set save_complaint=true\n"
                        f"  - Populate complaint_data with: name={name}, complaint_type={ctype}, description={desc}, location={location}\n"
                        f"  - Give the closing message with the reference ID.\n"
                        f"If they want to change something — update the relevant field and re-confirm.\n"
                        f"Do NOT ask for confirmation again.\n"
                    )
                else:
                    context += (
                        f"\n**ALL REQUIRED FIELDS COLLECTED:**\n"
                        f"Name: {name or 'Anonymous'} | Complaint Type: {ctype} | Description: {desc} | Location: {location}\n"
                        f"Read back these details and ask 'Is all this information correct?'\n"
                    )

        if self.gibberish_count > 0:
            context += f"\n**GIBBERISH COUNT:** {self.gibberish_count}/3 — if this input is also gibberish, increment to {self.gibberish_count + 1}. At 3, set end_conversation=true.\n"

        context += f"\n**CURRENT INPUT:** {current_statement}\n\n"
        return context

    def is_expired(self) -> bool:
        return datetime.now() - self.last_activity > SESSION_TIMEOUT


def cleanup_expired_sessions():
    expired = [sid for sid, s in CONVERSATION_SESSIONS.items() if s.is_expired()]
    for sid in expired:
        del CONVERSATION_SESSIONS[sid]


def get_or_create_session(session_id: str = None) -> ConversationSession:
    if session_id and session_id in CONVERSATION_SESSIONS:
        session = CONVERSATION_SESSIONS[session_id]
        if not session.is_expired():
            return session
        del CONVERSATION_SESSIONS[session_id]
    new_id  = session_id or str(uuid.uuid4())
    session = ConversationSession(new_id)
    CONVERSATION_SESSIONS[new_id] = session
    return session


# --- Unified endpoint (text + audio) ---
@app.post("/process-command-unified/", response_class=JSONResponse)
async def process_command_unified_endpoint(
    command_text: Optional[str]        = Form(None),
    audio_file:   Optional[UploadFile] = File(None),
    session_id:   Optional[str]        = Form(None),
    langChoice:   str                  = Form("en"),
):
    if not command_text and not audio_file:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing input", "reply": "Please provide either command_text or audio_file."},
        )
    if command_text and audio_file:
        return JSONResponse(
            status_code=400,
            content={"error": "Multiple inputs", "reply": "Please provide either command_text OR audio_file, not both."},
        )

    if audio_file:
        if GROQ_CLIENT is None:
            return JSONResponse(
                status_code=503,
                content={"error": "Groq unavailable", "reply": "Audio transcription is not available right now."},
            )
        valid_types = ['audio/', 'video/webm', 'video/mp4']
        valid_exts  = ['.mp3', '.wav', '.m4a', '.webm', '.ogg', '.flac', '.aac']
        is_valid = (
            (audio_file.content_type and any(audio_file.content_type.startswith(t) for t in valid_types))
            or (audio_file.filename and any(audio_file.filename.lower().endswith(e) for e in valid_exts))
        )
        if not is_valid:
            return JSONResponse(status_code=400, content={"error": "Invalid file type", "reply": "Please upload an audio file."})

        try:
            audio_data = await audio_file.read()
            result     = await transcribe_audio(audio_data, filename=audio_file.filename or "audio.mp3")
            if "error" in result:
                return JSONResponse(status_code=500, content={"error": result["error"], "reply": "Transcription failed."})
            command_text = result["text"]
            if not command_text.strip():
                return JSONResponse(status_code=400, content={"error": "No speech detected", "reply": "No speech detected. Please try again."})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e), "reply": "Audio processing failed."})

    if not command_text.strip():
        raise HTTPException(status_code=400, detail="command_text cannot be empty")

    ai_response = await get_ai_command_response(command_text, session_id=session_id)

    if "error" in ai_response:
        status_code = 503 if ai_response.get("error") == "AI_NOT_AVAILABLE" else 500
        return JSONResponse(status_code=status_code, content=ai_response)

    # Generate TTS in the same response to eliminate a second round trip.
    # Use reply_lang from the LLM (what language Maya actually replied in).
    reply_text = ai_response.get("reply", "")
    if reply_text:
        reply_lang = ai_response.get("reply_lang", langChoice)
        tts_lang   = "en" if reply_lang == "sg" else reply_lang
        audio_bytes = await _generate_tts(reply_text, tts_lang)
        if audio_bytes:
            import base64
            ai_response["audio_b64"] = base64.b64encode(audio_bytes).decode()

    return JSONResponse(status_code=200, content=ai_response)


@app.post("/reset-conversation/{session_id}")
async def reset_conversation(session_id: str):
    CONVERSATION_SESSIONS.pop(session_id, None)
    return {"message": "Conversation reset", "session_id": session_id}


@app.post("/start-session/")
async def start_session(session_id: str = Form(...), greeting: str = Form(...)):
    """Seed a new session with Maya's opening greeting so she doesn't re-greet."""
    session = get_or_create_session(session_id)
    # Only seed if history is empty (fresh call)
    if not session.chat_history:
        session.add_message("assistant", greeting)
    return {"ok": True}


# --- Complaints endpoints ---
@app.get("/complaints")
async def list_complaints():
    try:
        complaints = get_all_complaints()
        return {"complaints": complaints}
    except Exception as e:
        logger.error(f"Failed to fetch complaints: {e}")
        return {"complaints": [], "warning": "DynamoDB not configured."}


@app.get("/complaints/patterns")
async def complaint_patterns():
    """Cross-complaint pattern detection for the admin dashboard — see patterns.py."""
    try:
        alerts = detect_patterns(get_all_complaints())
        return {"alerts": alerts}
    except Exception as e:
        logger.error(f"Failed to compute complaint patterns: {e}")
        return {"alerts": [], "warning": "Pattern detection unavailable."}


@app.patch("/complaints/{complaint_id}/status")
async def update_complaint_status(complaint_id: str, status: str = Body(..., embed=True)):
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {VALID_STATUSES}")
    try:
        update_status(complaint_id, status)
        return {"complaint_id": complaint_id, "status": status}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status_code=404, detail="Complaint not found")
        logger.error(f"Failed to update status for {complaint_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update status")




@app.delete("/complaints/clear")
async def clear_complaints():
    try:
        clear_all_complaints()
    except Exception as e:
        logger.error(f"Failed to clear complaints: {e}")
    return {"message": "All complaints cleared."}


# edge-tts voices per language (fallback for Polly-unsupported languages)
EDGE_VOICES = {
    "en": "en-US-AriaNeural",
    "sg": "en-US-AriaNeural",
    "ms": "ms-MY-OsmanNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ta": "ta-SG-VenbaNeural",
    "th": "th-TH-PremwadeeNeural",
    "id": "id-ID-GadisNeural",
    "vi": "vi-VN-HoaiMyNeural",
}

# AWS Polly — sub-200ms synthesis, same AWS region as EC2
POLLY_CLIENT = None
POLLY_VOICES = {
    "en": ("Joanna", "neural"),
    "sg": ("Joanna", "neural"),
    # zh omitted — Polly Zhiyu requires polly:SynthesizeSpeech IAM permission not granted; falls to edge-tts directly
}

def _init_polly():
    global POLLY_CLIENT
    try:
        POLLY_CLIENT = boto3.client("polly", region_name="ap-southeast-1")
        logger.info("AWS Polly initialized")
    except Exception as e:
        logger.warning(f"Polly init failed (will use edge-tts): {e}")


async def _generate_polly_tts(text: str, lang: str) -> bytes | None:
    if POLLY_CLIENT is None or lang not in POLLY_VOICES:
        return None
    voice_id, engine = POLLY_VOICES[lang]
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: POLLY_CLIENT.synthesize_speech(
                Text=text, OutputFormat="mp3", VoiceId=voice_id, Engine=engine
            ),
        )
        return response["AudioStream"].read()
    except Exception as e:
        logger.warning(f"Polly TTS failed for lang={lang}: {e}")
        return None


def split_into_sentences(text: str) -> list[str]:
    """
    Split the LLM reply into sentence-sized chunks for streaming TTS.

    The backend generates TTS for each chunk concurrently (asyncio.gather), so shorter
    chunks mean the first audio arrives faster. We buffer until we hit a sentence boundary
    or 6 words — whichever comes first — to avoid very short chunks that would cause
    unnatural pauses between words.
    """
    parts = re.split(r'(?<=[.!?。！？])\s+', text.strip())
    result = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        buf += (" " if buf else "") + part
        # Emit chunk when buffer reaches ~6 words or ends with strong punctuation
        if len(buf.split()) >= 6 or re.search(r'[.!?。！？]$', buf):
            result.append(buf)
            buf = ""
    if buf:
        result.append(buf)
    return result if result else [text]


GROQ_TTS_VOICES = {
    "en": "Aaliyah-PlayAI",
    "sg": "Aaliyah-PlayAI",
}

async def _generate_groq_tts(text: str, lang: str) -> bytes | None:
    """
    Groq PlayAI TTS — fastest option (~100ms), English and Singlish only.
    Returns None if the language isn't supported or if the API call fails,
    so the caller can try the next TTS provider in the chain.
    """
    if lang not in GROQ_TTS_VOICES or not GROQ_API_KEY:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/speech",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "playai-tts", "voice": GROQ_TTS_VOICES[lang], "input": text, "response_format": "mp3"},
            )
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.warning(f"Groq TTS HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        logger.warning(f"Groq TTS failed: {e}")
    return None


async def _generate_tts(text: str, lang: str) -> bytes | None:
    """
    TTS waterfall: Groq PlayAI → AWS Polly → edge-tts.

    - Groq PlayAI: ~100ms, en/sg only
    - Polly:       ~200ms, en/sg only (zh omitted — Polly Zhiyu needs IAM permission not granted)
    - edge-tts:    ~2s, all languages — always works, just slow
    """
    # 1. Groq PlayAI — fastest (~100ms), en/sg only
    if lang in GROQ_TTS_VOICES:
        audio = await _generate_groq_tts(text, lang)
        if audio:
            return audio

    # 2. Polly — fast neural TTS for en/sg; zh intentionally skipped (AccessDenied on IAM)
    if lang in POLLY_VOICES:
        audio = await _generate_polly_tts(text, lang)
        if audio:
            return audio

    # 2. edge-tts — free Microsoft neural TTS, covers all our languages
    voice = EDGE_VOICES.get(lang, EDGE_VOICES["en"])
    last_err = None
    for attempt in range(2):
        try:
            communicate = edge_tts.Communicate(text, voice)
            audio_chunks = []
            async def collect():
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio":
                        audio_chunks.append(chunk["data"])
            await asyncio.wait_for(collect(), timeout=6)
            audio_data = b"".join(audio_chunks)
            if not audio_data:
                raise ValueError("No audio returned")
            return audio_data
        except Exception as e:
            last_err = e
            logger.warning(f"edge-tts attempt {attempt + 1} failed: {e}")
            if attempt < 1:
                await asyncio.sleep(0.1)
    logger.error(f"TTS failed after all attempts: {last_err}")
    return None


# --- TTS: edge-tts buffered with timeout + retries ---
@app.post("/speak")
async def speak_endpoint(text: str = Form(...), lang: str = Form("en")):
    audio_data = await _generate_tts(text, lang)
    if audio_data is None:
        raise HTTPException(status_code=500, detail="TTS generation failed")
    return Response(content=audio_data, media_type="audio/mpeg")


# --- Transcribe endpoint (Groq Whisper with language hint) ---
LANG_TO_WHISPER  = {"ms": "ms", "zh": "zh", "en": "en", "th": "th", "ta": "ta", "id": "id", "vi": "vi"}
WHISPER_TO_LANG  = {
    "en": "en", "english": "en",
    "ms": "ms", "malay": "ms",
    "id": "id", "indonesian": "id",
    "zh": "zh", "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
    "yue": "zh",
    "th": "th", "thai": "th",
    "ta": "ta", "tamil": "ta",
    "vi": "vi", "vietnamese": "vi",
}

def detect_language_from_text(text: str) -> str:
    """
    Detect language from the transcribed text rather than from audio.

    Text-based detection is more reliable than Whisper's audio detection because:
    - Whisper often misidentifies Malay as Indonesian
    - Singlish scores as English (correct — we treat them the same)
    - CJK characters are an unambiguous signal for Mandarin
    - A single common Malay word in the transcript is enough to classify it as Malay
    """
    if not text:
        return "en"

    # Thai: Thai Unicode block U+0E00-U+0E7F
    if any('\u0e00' <= c <= '\u0e7f' for c in text):
        return "th"

    # Tamil: Tamil Unicode block U+0B80-U+0BFF
    if any('\u0b80' <= c <= '\u0bff' for c in text):
        return "ta"

    # Chinese: CJK Unified Ideographs
    if any('\u4e00' <= c <= '\u9fff' for c in text):
        return "zh"

    # Vietnamese: distinctive diacritic characters
    vi_chars = "\u0111\u01a1\u01b0\u1ecd\u1ebf\u1ed3\u1ebd\u1edb\u1eb3"
    if sum(1 for c in text.lower() if c in vi_chars) >= 2:
        return "vi"

    words    = text.lower().split()
    word_set = set(words)

    # Indonesian markers (distinct from Malay)
    indonesian_markers = {
        "tidak", "bisa", "sudah", "nggak", "adalah", "juga", "akan",
        "bapak", "ibu", "pak", "bu", "bagaimana", "laporan", "gedung",
        "lantai", "kerusakan", "perbaikan", "mohon",
    }
    # Malay-specific markers
    malay_markers = {
        "saya", "awak", "anda", "nama", "aduan", "tolong", "terima", "kasih",
        "boleh", "selamat", "perlukan", "bantuan", "bangunan", "nombor",
        "dengan", "untuk", "yang", "ini", "itu", "ada", "dari", "pada",
        "tentang", "masalah", "berlaku", "membuat", "mengenai", "ingin",
    }
    malay_hits = len(word_set & malay_markers)
    indo_hits  = len(word_set & indonesian_markers)

    if malay_hits > indo_hits and malay_hits >= 1:
        return "ms"
    if indo_hits >= 2:
        return "id"
    if malay_hits >= 1 or indo_hits >= 1:
        return "ms"

    # Singlish — treat as English for reply purposes
    return "en"


@app.post("/transcribe")
async def transcribe_endpoint(audio: UploadFile = File(...), lang: str = Form("en")):
    audio_data = await audio.read()
    # Pass language hint to Whisper when known — reduces hallucinations on non-English audio
    # For 'en'/'sg' (may actually be Chinese/Malay on first utterance), pass no hint so Whisper auto-detects
    LANG_TO_WHISPER = {"ms": "ms", "zh": "zh", "th": "th", "ta": "ta", "id": "id", "vi": "vi", "en": None, "sg": None}
    whisper_lang_hint = LANG_TO_WHISPER.get(lang, None)
    # Relax no_speech rejection for non-English hints — Whisper scores Chinese higher without a hint
    relaxed_threshold = whisper_lang_hint is None
    result = await transcribe_audio(audio_data, filename=audio.filename or "audio.webm", language=whisper_lang_hint, relaxed=relaxed_threshold)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])

    text = result.get("text", "")

    # Primary: detect language from the transcribed text
    detected_lang = detect_language_from_text(text)

    # If text detection is ambiguous (returned 'en'), cross-check with Whisper's audio detection
    if detected_lang == "en":
        raw_whisper = result.get("language", "en") or "en"
        whisper_lang = WHISPER_TO_LANG.get(raw_whisper.lower(), "en")
        if whisper_lang not in ("en", "sg"):
            detected_lang = whisper_lang

    # Singlish is treated as English throughout
    if detected_lang == "sg":
        detected_lang = "en"

    return {"text": text, "detected_lang": detected_lang}



# --- Gemini Live WebSocket ---
@app.websocket("/gemini-ws")
async def gemini_websocket(websocket: WebSocket, api_key: str = ""):
    if not api_key or api_key != API_KEY:
        await websocket.close(code=4001)
        return
    await handle_gemini_ws(websocket)


# --- WebSocket: legacy conversation endpoint (kept as fallback) ---
@app.websocket("/ws")
async def websocket_conversation(websocket: WebSocket, api_key: str = ""):
    """
    Persistent WebSocket — hot path for every conversation turn.

    Supports barge-in: client sends {"type":"interrupt"} while Maya is speaking,
    which cancels the in-flight LLM/TTS task immediately.

    Auth: ?api_key=... query param.
    """
    if not api_key or api_key != API_KEY:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    session_id   = None
    current_task: asyncio.Task | None = None

    async def process_message(text: str, sid: str, lang: str = "en"):
        """
        Stream LLM sentences → kick off TTS per sentence immediately → send chunks in order.

        Each sentence's TTS starts as soon as the LLM yields it, so sentence-1 audio
        can be in-flight while the LLM is still generating sentence-2. This cuts
        time-to-first-audio compared to waiting for the full reply before batching.
        """
        try:
            chunk_index = 0
            pending: list[tuple[str, asyncio.Task]] = []
            send_cursor = 0
            done_data   = None

            async def drain_ready():
                nonlocal send_cursor, chunk_index
                while send_cursor < len(pending):
                    sentence, task = pending[send_cursor]
                    if not task.done():
                        break
                    audio_bytes = task.result()
                    send_cursor += 1
                    if isinstance(audio_bytes, Exception) or not audio_bytes:
                        continue
                    await websocket.send_text(json.dumps({
                        "type":      "chunk",
                        "sentence":  sentence,
                        "audio_b64": base64.b64encode(audio_bytes).decode(),
                        "index":     chunk_index,
                        "is_last":   False,
                    }))
                    chunk_index += 1

            async for item in stream_ai_response(text, session_id=sid, lang=lang):
                if item["type"] == "sentence":
                    task = asyncio.create_task(_generate_tts(item["text"], item["lang"]))
                    pending.append((item["text"], task))
                    await drain_ready()
                elif item["type"] == "done":
                    done_data = item

            # Flush remaining chunks in order; mark the actual last one
            total = len(pending)
            for i in range(send_cursor, total):
                sentence, task = pending[i]
                audio_bytes = await task
                if isinstance(audio_bytes, Exception) or not audio_bytes:
                    continue
                await websocket.send_text(json.dumps({
                    "type":      "chunk",
                    "sentence":  sentence,
                    "audio_b64": base64.b64encode(audio_bytes).decode(),
                    "index":     chunk_index,
                    "is_last":   i == total - 1,
                }))
                chunk_index += 1

            if done_data:
                meta = {k: v for k, v in done_data.items() if k != "type"}
                meta["type"]       = "done"
                meta["full_reply"] = done_data.get("reply", "")
                await websocket.send_text(json.dumps(meta))

        except asyncio.CancelledError:
            logger.info(f"Barge-in: cancelled processing for session={sid}")

    # Use asyncio.wait so we can receive an interrupt while processing is ongoing.
    # pending_receive always holds the next receive coroutine as a Task.
    pending_receive = asyncio.create_task(websocket.receive_text())

    try:
        while True:
            wait_set = {pending_receive}
            if current_task and not current_task.done():
                wait_set.add(current_task)

            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            if pending_receive not in done:
                continue  # only the process task finished — loop back to wait for next message

            try:
                raw = pending_receive.result()
            except Exception:
                break  # WebSocket closed or error
            pending_receive = asyncio.create_task(websocket.receive_text())

            data = json.loads(raw)

            if data.get("type") == "init":
                session_id = data.get("session_id") or str(uuid.uuid4())
                greeting   = data.get("greeting", "")
                if greeting:
                    sess = get_or_create_session(session_id)
                    if not sess.chat_history:
                        sess.add_message("assistant", greeting)
                await websocket.send_text(json.dumps({"type": "ready", "session_id": session_id}))

            elif data.get("type") == "interrupt":
                if current_task and not current_task.done():
                    current_task.cancel()
                    logger.info(f"Barge-in interrupt: session={session_id}")

            elif data.get("type") == "message":
                text = (data.get("text") or "").strip()
                lang = data.get("lang", "en")
                if not text:
                    continue
                if current_task and not current_task.done():
                    current_task.cancel()
                current_task = asyncio.create_task(process_message(text, session_id, lang))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: session={session_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        if current_task:
            current_task.cancel()
        pending_receive.cancel()


# --- Root ---
@app.get("/")
async def read_root():
    return {
        "message": "Building Complaint AI API",
        "version": "2.0.0",
        "assistant": "Maya — Building Supervisor Assistant",
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# Lambda entrypoint — wraps the FastAPI app for AWS Lambda + API Gateway HTTP API
from mangum import Mangum
_stage = os.getenv("STAGE", "prod")
lambda_handler = Mangum(app, lifespan="off", api_gateway_base_path=f"/{_stage}")
