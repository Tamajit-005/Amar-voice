import os
import re
import time
import uuid
import shutil
import threading
from typing import Literal
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Query, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from backend.engine import VoiceEngine, REPO_ROOT

app = FastAPI(title="Amar Voice API", version="1.0.0")

# Security: Allow safe origins (can be tuned for LAN / Cloudflare tunnel)
# W3C CORS spec: allow_origins=["*"] cannot be used with allow_credentials=True
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Engine lazy-initialization singleton
_engine_instance = None

def get_engine() -> VoiceEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = VoiceEngine()
    return _engine_instance

# Input Validation & Sanitization
def sanitize_identifier(value: str) -> str:
    """Strictly sanitize speaker and preset identifiers against path traversal."""
    cleaned = re.sub(r'[^a-zA-Z0-9_\-]', '', value)
    return cleaned if cleaned else "default"


def _ns(user: str | None) -> str:
    """Device namespace: per-phone voice spaces. Missing/blank -> shared."""
    if not user or not user.strip():
        return "shared"
    cleaned = re.sub(r'[^a-zA-Z0-9_\-]', '', user.strip())
    return cleaned if cleaned and cleaned != "default" else "shared"


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000, description="Text to synthesize")
    language: Literal["en", "hi", "bn"] = "en"
    speaker: str = Field("default", max_length=50)
    user: str = Field("shared", max_length=50)

@app.get("/health")
def health():
    eng = get_engine()
    try:
        speakers = eng.list_speakers()
    except Exception as e:
        print(f"[error] list_speakers failed :: {type(e).__name__}: {e}")
        speakers = ["default"]
    return {
        "status": "online",
        "provider": getattr(eng, "provider", "elevenlabs"),
        "model": getattr(eng, "model_id", "eleven_v4"),
        "key_present": getattr(eng, "key_present", False),
        "models_ready": {
            "elevenlabs_model": getattr(eng, "model_id", "eleven_v4"),
            "key_present": getattr(eng, "key_present", False)
        },
        "speakers": speakers,
        "device": eng.device
    }

@app.get("/api/speakers")
def get_speakers(user: str = Query("shared", max_length=50)):
    eng = get_engine()
    try:
        return {"speakers": eng.list_speakers(_ns(user))}
    except Exception as e:
        print(f"[error] list_speakers failed :: {type(e).__name__}: {e}")
        return {"speakers": ["default"]}

@app.post("/api/speak")
def speak(req: SpeakRequest, background_tasks: BackgroundTasks, request: Request):
    _check_rate(request, "speak")
    safe_speaker = sanitize_identifier(req.speaker)
    ns = _ns(req.user)
    clean_text = req.text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
        
    # Generate unique temp file per request to eliminate race conditions and CWD litter (M1/M2/Bug 7)
    temp_dir = os.path.join(REPO_ROOT, "tmp_outputs")
    os.makedirs(temp_dir, exist_ok=True)
    unique_id = uuid.uuid4().hex
    out_file = os.path.join(temp_dir, f"speech_{ns}_{safe_speaker}_{unique_id}.wav")
    
    eng = get_engine()
    try:
        eng.synthesize(
            text=clean_text,
            lang=req.language,
            speaker=safe_speaker,
            out_path=out_file,
            ns=ns
        )
        
        # Schedule cleanup after streaming response completes
        background_tasks.add_task(os.remove, out_file)
        return FileResponse(out_file, media_type="audio/wav")
    except Exception as e:
        if os.path.exists(out_file):
            try:
                os.remove(out_file)
            except OSError:
                pass
        if isinstance(e, HTTPException):
            raise e
        if "No ElevenLabs voice" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise _public_fail(e, "Voice generation failed. Retry in a moment.")

@app.get("/api/presets/{preset_id}")
def get_preset(preset_id: str, speaker: str = "default", user: str = Query("shared", max_length=50)):
    safe_preset = sanitize_identifier(preset_id)
    safe_speaker = sanitize_identifier(speaker)
    ns = _ns(user)
    eng = get_engine()

    # Speaker-scoped file only — never silently serve another voice.
    # (Old fallback to default/ is removed: it made every speaker sound like "default".)
    spk_path = os.path.join(eng._cbase(ns), safe_speaker, f"{safe_preset}.wav")
    if os.path.exists(spk_path):
        return FileResponse(spk_path, media_type="audio/wav")

    # On-demand fallback: fresh deploy / new speaker with empty cache synthesizes once, then caches
    from backend.engine import PRESET_MAP
    preset_def = PRESET_MAP.get(safe_preset)
    if preset_def is None:
        raise HTTPException(status_code=404, detail="Preset not found.")
    try:
        out_path = os.path.join(eng._cbase(ns), safe_speaker, f"{safe_preset}.wav")
        eng.synthesize(text=preset_def["text"], lang=preset_def["lang"], speaker=safe_speaker, out_path=out_path, ns=ns)
        return FileResponse(out_path, media_type="audio/wav")
    except HTTPException:
        raise
    except Exception as e:
        # Honest failure: never serve another speaker's voice as this one.
        if "No ElevenLabs voice" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise _public_fail(e, "Preset synthesis failed. Retry in a moment.")

# In-memory per-IP rate limits (paid ElevenLabs calls; no auth on LAN).
# {bucket: (max_hits, window_seconds)}
_RATE_LIMITS = {
    "speak": (30, 60),
    "download": (30, 60),
    "upload": (10, 3600),
}
_rate_hits: dict = {}


def _check_rate(request: Request, bucket: str):
    limit, window = _RATE_LIMITS[bucket]
    ip = (request.client.host if request.client else "unknown")
    now = time.monotonic()
    key = (bucket, ip)
    hits = [t for t in _rate_hits.get(key, []) if now - t < window]
    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests. Slow down and retry.")
    hits.append(now)
    _rate_hits[key] = hits


def _public_fail(exc: Exception, public: str, code: int = 500) -> HTTPException:
    """Log full internals server-side; send only a safe message to the client."""
    print(f"[error] {public} :: {type(exc).__name__}: {exc}")
    return HTTPException(status_code=code, detail=public)


# Lazy-loader state for voice imports: speaker -> {state, done, total, detail}
_voice_status: dict = {}

# Regen dedup: one clone+regen job per speaker at a time (prevents quota pileup)
_regen_running: set = set()


def _background_update_speaker(speaker_name: str, ns: str = "shared"):
    """Heavy synthesis task run out of the event loop to prevent server DoS."""
    job = (ns, speaker_name)
    if job in _regen_running:
        print(f"Regen already running for '{speaker_name}' [{ns}]; skipping duplicate.")
        return
    _regen_running.add(job)
    from backend.engine import PRESETS
    _voice_status[job] = {"state": "working", "done": 0, "total": len(PRESETS), "detail": "cloning voice"}
    try:
        eng = get_engine()
        # Re-clone (not just resolve): newly uploaded clips must join the voice
        eng.reclone_speaker(speaker_name, ns)
        # Delete-then-regenerate so quick-cards pick up the new voice (stale-cache fix)
        def _progress(done: int, total: int):
            _voice_status[job] = {"state": "working", "done": done, "total": total, "detail": "generating presets"}
        eng.refresh_speaker_presets(speaker_name, progress=_progress, ns=ns)
        _voice_status[job] = {"state": "ready", "done": len(PRESETS), "total": len(PRESETS), "detail": "ok"}
    except Exception as e:
        print(f"Error in background speaker update for '{speaker_name}' [{ns}]: {e}")
        _voice_status[job] = {"state": "error", "done": 0, "total": len(PRESETS), "detail": "voice update failed"}
    finally:
        _regen_running.discard(job)


@app.get("/api/voice-status")
def voice_status(speaker: str = Query("default", max_length=50), user: str = Query("shared", max_length=50)):
    """Poll target for the frontend lazy loader during voice cloning."""
    safe_speaker = sanitize_identifier(speaker)
    ns = _ns(user)
    return {"speaker": safe_speaker, **_voice_status.get((ns, safe_speaker), {"state": "ready", "done": 0, "total": 0, "detail": "idle"})}

class DownloadRequest(BaseModel):
    text: str | None = Field(default=None, max_length=1000, description="Custom text (or preset_id for quick-cards)")
    preset_id: str | None = Field(default=None, description="Preset id, e.g. water, water_hi, water_bn")
    language: Literal["en", "hi", "bn"] = "en"
    speaker: str = Field("default", max_length=50)
    user: str = Field("shared", max_length=50)


MAX_DOWNLOAD_BYTES = 350 * 1024  # downloadable voice ships as MP3 capped at 350 KB


@app.post("/api/download")
def download(req: DownloadRequest, background_tasks: BackgroundTasks, request: Request):
    """MP3 download of a preset or custom text, bitrate-stepped to fit 350 KB."""
    _check_rate(request, "download")
    from backend.engine import PRESET_MAP
    from pydub import AudioSegment

    safe_speaker = sanitize_identifier(req.speaker)
    ns = _ns(req.user)
    eng = get_engine()
    tmp_dir = os.path.join(REPO_ROOT, "tmp_downloads")
    os.makedirs(tmp_dir, exist_ok=True)
    unique_id = uuid.uuid4().hex
    src_wav = os.path.join(tmp_dir, f"dl_src_{ns}_{safe_speaker}_{unique_id}.wav")
    out_mp3 = os.path.join(tmp_dir, f"dl_{ns}_{safe_speaker}_{unique_id}.mp3")
    background_tasks.add_task(_cleanup_paths, src_wav, out_mp3)

    try:
        if req.preset_id:
            safe_preset = sanitize_identifier(req.preset_id)
            preset_def = PRESET_MAP.get(safe_preset)
            if preset_def is None:
                raise HTTPException(status_code=404, detail="Preset not found.")
            cached = os.path.join(eng._cbase(ns), safe_speaker, f"{safe_preset}.wav")
            if os.path.exists(cached):
                src_wav = cached  # guarded: _cleanup_paths never deletes under static_presets
            else:
                eng.synthesize(text=preset_def["text"], lang=preset_def["lang"], speaker=safe_speaker, out_path=src_wav, ns=ns)
            stem = f"amar-voice-{safe_preset}-{safe_speaker}"
        elif req.text and req.text.strip():
            eng.synthesize(text=req.text.strip(), lang=req.language, speaker=safe_speaker, out_path=src_wav, ns=ns)
            stem = f"amar-voice-custom-{safe_speaker}-{req.language}"
        else:
            raise HTTPException(status_code=400, detail="Provide text or preset_id.")

        audio = AudioSegment.from_file(src_wav).set_channels(1).set_frame_rate(22050)
        for bitrate in ("128k", "96k", "64k", "32k"):
            audio.export(out_mp3, format="mp3", bitrate=bitrate, parameters=["-ac", "1", "-ar", "22050"])
            if os.path.getsize(out_mp3) <= MAX_DOWNLOAD_BYTES:
                return FileResponse(out_mp3, media_type="audio/mpeg", filename=f"{stem}.mp3")
        raise HTTPException(status_code=413, detail="Audio exceeds 350 KB even at minimum quality. Use shorter text.")
    except HTTPException:
        raise
    except Exception as e:
        if "No ElevenLabs voice" in str(e):
            raise HTTPException(status_code=404, detail=str(e))
        raise _public_fail(e, "Download failed. Retry in a moment.")


def _cleanup_paths(*paths: str):
    for p in paths:
        try:
            if os.path.isfile(p) and "static_presets" not in os.path.abspath(p):
                os.remove(p)
        except OSError:
            pass


class RenameRequest(BaseModel):
    old_name: str = Field(..., max_length=50)
    new_name: str = Field(..., max_length=50)
    user: str = Field("shared", max_length=50)


@app.post("/api/voices/rename")
def rename_voice_endpoint(req: RenameRequest, request: Request):
    """Renames a voice profile; the ElevenLabs voice ID is preserved."""
    _check_rate(request, "speak")
    if not req.new_name.strip():
        raise HTTPException(status_code=400, detail="New name is required.")
    old = sanitize_identifier(req.old_name)
    new = sanitize_identifier(req.new_name)
    ns = _ns(req.user)
    if old == "default" or new == "default":
        raise HTTPException(status_code=400, detail="The default voice cannot be renamed.")
    if old == new:
        raise HTTPException(status_code=400, detail="Old and new names are identical.")
    eng = get_engine()
    try:
        voice_id = eng.rename_speaker(old, new, ns)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise _public_fail(e, "Rename failed. Retry in a moment.")
    _voice_status.pop((ns, old), None)
    return {"status": "success", "old": old, "new": new, "voice_id": voice_id}


@app.delete("/api/voices/{speaker_name}")
def delete_voice(speaker_name: str, user: str = Query("shared", max_length=50)):
    """Deletes a speaker: cloud clone, id mapping, cached presets, and local clips."""
    safe_speaker = sanitize_identifier(speaker_name)
    if safe_speaker == "default":
        raise HTTPException(status_code=400, detail="The default voice cannot be deleted.")
    ns = _ns(user)
    eng = get_engine()
    try:
        removed = eng.delete_speaker(safe_speaker, ns)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise _public_fail(e, "Delete failed. Retry in a moment.")
    _voice_status.pop((ns, safe_speaker), None)
    return {"status": "success", "speaker": safe_speaker, "removed": removed}


@app.on_event("startup")
def warm_preset_cache():
    """Regenerates any missing presets in background so fresh deploys never 404."""
    if not shutil.which("ffmpeg"):
        print("[warn] ffmpeg not found — uploads and downloads need it (sudo apt install -y ffmpeg).")
    def _warm():
        try:
            eng = get_engine()
            eng.precompute_presets()
        except Exception as e:
            print(f"Preset warm-up note: {e}")
    threading.Thread(target=_warm, daemon=True).start()

@app.post("/api/upload-voice")
async def upload_voice(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    speaker_name: str = Query("default", max_length=50),
    user: str = Query("shared", max_length=50)
):
    """Uploads one or many voice notes; a single background clone+regen covers the batch."""
    _check_rate(request, "upload")
    uploads = [f for f in [file, *(files or [])] if f is not None]
    if not uploads:
        raise HTTPException(status_code=400, detail="Please select at least one audio file.")
    if len(uploads) > 6:
        raise HTTPException(status_code=400, detail="Maximum 6 clips per upload.")

    safe_speaker = sanitize_identifier(speaker_name)
    if safe_speaker == "default":
        raise HTTPException(status_code=400, detail="The default voice is fixed to ELEVENLABS_DEFAULT_VOICE_ID. Upload under a profile name instead.")
    ns = _ns(user)
    eng = get_engine()
    speaker_dir = os.path.join(eng._vbase(ns), safe_speaker)
    os.makedirs(speaker_dir, exist_ok=True)

    from pydub import AudioSegment
    saved: list[str] = []
    try:
        for upload in uploads:
            # Validate extension
            orig_name = os.path.basename(upload.filename or "audio.wav")
            safe_filename = sanitize_identifier(os.path.splitext(orig_name)[0])
            ext = os.path.splitext(orig_name)[1].lower()
            if ext not in [".wav", ".mp3", ".m4a", ".opus", ".ogg"]:
                raise HTTPException(status_code=400, detail=f"Unsupported audio format in '{orig_name}'. Please upload .wav, .mp3, .m4a, .opus, or .ogg.")

            # Cap each clip at 800 KB (free-tier disk/RAM; 6 clips = 4.8 MB max).
            # Compressed phone recordings (m4a/mp3/opus) fit minutes in this;
            # uncompressed WAV over ~18s will not — send compressed audio.
            MAX_BYTES = 800 * 1024
            content = await upload.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise HTTPException(status_code=413, detail=f"'{orig_name}' is too large. Maximum size is 800 KB per clip.")

            # Unique-ify so distinct originals never silently overwrite each other
            stem, counter = safe_filename, 0
            while os.path.exists(os.path.join(speaker_dir, f"{stem}.wav")) or f"{stem}.wav" in saved:
                counter += 1
                stem = f"{safe_filename}_{counter}"
            raw_path = os.path.join(speaker_dir, f"raw_{stem}{ext}")
            clean_path = os.path.join(speaker_dir, f"{stem}.wav")

            with open(raw_path, "wb") as f:
                f.write(content)

            try:
                audio = AudioSegment.from_file(raw_path)
                if len(audio) > 120_000:
                    raise HTTPException(status_code=413, detail=f"'{orig_name}' is longer than 2 minutes. Trim it first.")
                # Resample to native target sampling rate from engine config (22050 Hz)
                audio = audio.set_channels(1).set_frame_rate(eng.target_sampling_rate)
                audio.export(clean_path, format="wav")
                saved.append(os.path.basename(clean_path))
            except HTTPException:
                try:
                    if os.path.exists(clean_path):
                        os.remove(clean_path)
                except OSError:
                    pass
                raise
            except Exception as e:
                try:
                    if os.path.exists(clean_path):
                        os.remove(clean_path)
                except OSError:
                    pass
                raise _public_fail(e, f"Could not process '{orig_name}'. Try a different file.")
            finally:
                if os.path.exists(raw_path):
                    try:
                        os.remove(raw_path)
                    except OSError:
                        pass
    except Exception:
        # Roll back this batch: remove saved clips
        for name in saved:
            try:
                os.remove(os.path.join(speaker_dir, name))
            except OSError:
                pass
        raise
            
    # Offload voice cloning + preset regeneration to background worker (DoS fix)
    background_tasks.add_task(_background_update_speaker, safe_speaker, ns)
    
    return {
        "status": "success",
        "speaker": safe_speaker,
        "files": saved,
        "message": f"{len(saved)} clip(s) uploaded. Profile for '{safe_speaker}' is updating in background."
    }

# Mount static frontend assets
frontend_dir = os.path.join(REPO_ROOT, "frontend")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

@app.get("/manifest.json")
def get_manifest():
    manifest_path = os.path.join(REPO_ROOT, "frontend", "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="manifest.json not found")

@app.get("/sw.js")
def get_service_worker():
    sw_path = os.path.join(REPO_ROOT, "frontend", "sw.js")
    if os.path.exists(sw_path):
        return FileResponse(sw_path, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="sw.js not found")

@app.get("/icon.png")
def get_icon():
    icon_path = os.path.join(REPO_ROOT, "frontend", "icon.png")
    if os.path.exists(icon_path):
        return FileResponse(icon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="icon.png not found")

@app.get("/icon-192.png")
def get_icon_192():
    icon_path = os.path.join(REPO_ROOT, "frontend", "icon-192.png")
    if os.path.exists(icon_path):
        return FileResponse(icon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="icon-192.png not found")

@app.get("/")
def index():
    html_path = os.path.join(REPO_ROOT, "frontend", "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return {"message": "Amar Voice Backend Running"}
