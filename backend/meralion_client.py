"""MERaLiON API client — wraps http://meralion.org:8010/api"""

import base64
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests


class _RateLimiter:
    """Token-bucket: max `rate` calls per 60 seconds."""
    def __init__(self, rate: int = 4):
        self._rate = rate
        self._interval = 60.0 / rate
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            wait = self._last + self._interval - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


_limiter = _RateLimiter(rate=4)

BASE_URL = "http://meralion.org:8010/api"

MODELS = {
    "v3":      "MERaLiON/MERaLiON-3-10B",
    "v2":      "MERaLiON/MERaLiON-2-10B",
    "v2-asr":  "MERaLiON/MERaLiON-2-10B-ASR",
    "exp":     "MERaLiON/MERaLiON-ASR-EXP",
    "encoder": "MERaLiON/MERaLiON-SpeechEncoder2-ASR-CTC",
}


@dataclass
class APIResult:
    endpoint: str
    model: str
    text: str
    latency_ms: float
    status_code: int
    tokens_used: int = 0
    error: str = ""
    words: list = field(default_factory=list)
    segments: list = field(default_factory=list)


def _encode_audio(path: str | Path, data_uri: bool = True) -> str:
    path = Path(path)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    if not data_uri:
        return b64
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg", "m4a": "audio/mp4"}.get(path.suffix.lstrip(".").lower(), "audio/wav")
    return f"data:{mime};base64,{b64}"


def _extract_text(resp: requests.Response) -> tuple[str, int, list, list]:
    data = resp.json()
    choices = data.get("choices", [])
    text = choices[0]["message"]["content"] if choices else ""
    tokens = data.get("usage", {}).get("total_tokens", 0)
    msg = choices[0].get("message", {}) if choices else {}
    words = msg.get("words") or []
    segments = msg.get("segments") or []
    return text, tokens, words, segments


class MERaLiONClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _post(self, endpoint: str, payload: dict) -> tuple[requests.Response, float]:
        _limiter.wait()
        t0 = time.perf_counter()
        resp = self.session.post(f"{BASE_URL}{endpoint}", json=payload, timeout=120)
        latency = (time.perf_counter() - t0) * 1000
        return resp, latency

    def _result(self, endpoint: str, model_key: str, resp: requests.Response, latency: float) -> APIResult:
        model_name = MODELS.get(model_key, model_key)
        if not resp.ok:
            return APIResult(endpoint, model_name, "", latency, resp.status_code, error=resp.text[:200])
        text, tokens, words, segments = _extract_text(resp)
        return APIResult(endpoint, model_name, text, latency, resp.status_code, tokens, words=words, segments=segments)

    def transcribe(
        self,
        audio_path: str | Path,
        model: str = "v2-asr",
        return_timestamps: bool = False,
        return_diarization: bool = False,
        boundary_mode: str = "parallel",
    ) -> APIResult:
        payload = {
            "audio_url": _encode_audio(audio_path),
            "model": MODELS.get(model, model),
            "return_timestamps": return_timestamps,
            "return_diarization": return_diarization,
            "boundary_mode": boundary_mode,
        }
        resp, lat = self._post("/audio/transcription", payload)
        return self._result("/audio/transcription", model, resp, lat)

    def translate(
        self,
        audio_path: str | Path,
        target_language: str,
        source_language: str = "",
        model: str = "v2-asr",
    ) -> APIResult:
        payload = {
            "audio_url": _encode_audio(audio_path),
            "model": MODELS.get(model, model),
            "translation_params": {"source_language": source_language, "target_language": target_language},
        }
        resp, lat = self._post("/audio/translation", payload)
        return self._result("/audio/translation", model, resp, lat)

    def summarize(self, audio_path: str | Path, model: str = "v2") -> APIResult:
        payload = {"audio_url": _encode_audio(audio_path), "model": MODELS.get(model, model)}
        resp, lat = self._post("/audio/summarization", payload)
        return self._result("/audio/summarization", model, resp, lat)

    def detect_emotion(self, audio_path: str | Path, model: str = "v3") -> APIResult:
        payload = {"audio_url": _encode_audio(audio_path), "model": MODELS.get(model, model)}
        resp, lat = self._post("/audio/emotion_detection", payload)
        return self._result("/audio/emotion_detection", model, resp, lat)

    def detect_gender(self, audio_path: str | Path, model: str = "v3") -> APIResult:
        payload = {"audio_url": _encode_audio(audio_path), "model": MODELS.get(model, model)}
        resp, lat = self._post("/audio/gender_detection", payload)
        return self._result("/audio/gender_detection", model, resp, lat)

    def caption(self, audio_path: str | Path, model: str = "v3") -> APIResult:
        payload = {"audio_url": _encode_audio(audio_path), "model": MODELS.get(model, model)}
        resp, lat = self._post("/audio/audio_captioning", payload)
        return self._result("/audio/audio_captioning", model, resp, lat)

    def speech_instruction(self, audio_path: str | Path, model: str = "v3") -> APIResult:
        payload = {"audio_url": _encode_audio(audio_path), "model": MODELS.get(model, model)}
        resp, lat = self._post("/audio/speech_instruction", payload)
        return self._result("/audio/speech_instruction", model, resp, lat)

    def encoder_transcribe(self, audio_paths: list[str | Path]) -> list[APIResult]:
        """Transcribes one file at a time (encoder doesn't support large batches)."""
        results = []
        for path in audio_paths:
            _limiter.wait()
            payload = {"audio": [_encode_audio(path, data_uri=False)]}
            t0 = time.perf_counter()
            resp = self.session.post(f"{BASE_URL}/speechencoder/transcribe", json=payload, timeout=120)
            lat = (time.perf_counter() - t0) * 1000
            if not resp.ok:
                results.append(APIResult("/speechencoder/transcribe", MODELS["encoder"], "", lat, resp.status_code, error=resp.text[:200]))
            else:
                items = resp.json().get("data", [])
                text = items[0].get("text", "") if items else ""
                results.append(APIResult("/speechencoder/transcribe", MODELS["encoder"], text, lat, resp.status_code))
        return results

    def ping(self) -> dict:
        resp = self.session.get(f"{BASE_URL}/keys/ping", timeout=10)
        return resp.json() if resp.ok else {"error": resp.text}

    def usage(self) -> dict:
        resp = self.session.get(f"{BASE_URL}/keys/usage", timeout=10)
        return resp.json() if resp.ok else {"error": resp.text}
