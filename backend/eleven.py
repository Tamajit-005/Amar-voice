"""Minimal ElevenLabs REST client (stdlib only, no new dependencies).

Uses ELEVENLABS_API_KEY from the environment. Endpoints used:
  POST /v1/text-to-speech/{voice_id}?output_format=...  -> audio bytes
  GET  /v1/voices                                        -> list voices
  POST /v1/voices/add (multipart: name + files)          -> {"voice_id": ...}
  DELETE /v1/voices/{voice_id}                           -> delete cloned voice
"""
import io
import json
import mimetypes
import os
import urllib.request
import urllib.error
import uuid

BASE_URL = "https://api.elevenlabs.io/v1"


def _load_dotenv() -> None:
    """Load REPO_ROOT/.env (KEY=VALUE lines) so uvicorn picks up the key without exports."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except OSError:
        pass


_load_dotenv()


def api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set. Export it before starting the server "
            "(claim free credits at https://hacktoberfest.com/my/promos)."
        )
    return key


def _http(method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: int = 180) -> bytes:
    req = urllib.request.Request(BASE_URL + path, data=body, method=method)
    req.add_header("xi-api-key", api_key())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if e.code == 401:
            raise RuntimeError("ElevenLabs rejected the API key (401). Check ELEVENLABS_API_KEY.") from e
        if e.code == 429:
            raise RuntimeError("ElevenLabs rate limit hit (429). Wait a minute and retry.") from e
        raise RuntimeError(f"ElevenLabs error {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        # DNS failure, reset, timeout: transient network, safe to retry once
        raise RuntimeError(f"ElevenLabs unreachable (network). Check internet and retry: {e}") from e


def text_to_speech(
    text: str,
    voice_id: str,
    model_id: str = "eleven_multilingual_v2",
    stability: float = 0.55,
    similarity_boost: float = 0.75,
    style: float = 0.35,
    output_format: str = "mp3_44100_128",
) -> bytes:
    """English + Hindi auto-detected by eleven_multilingual_v2; returns MP3 bytes."""
    payload = json.dumps({
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": True,
        },
    }).encode("utf-8")
    return _http(
        "POST",
        f"/text-to-speech/{voice_id}?output_format={output_format}",
        body=payload,
        headers={"Content-Type": "application/json"},
        timeout=180,
    )


def list_voices() -> list:
    raw = _http("GET", "/voices", timeout=60)
    return json.loads(raw.decode("utf-8")).get("voices", [])


def delete_voice(voice_id: str) -> bool:
    """Returns True if the cloud clone is gone (or was already gone)."""
    try:
        _http("DELETE", f"/voices/{voice_id}", timeout=60)
        return True
    except Exception as e:
        print(f"ElevenLabs delete note ({voice_id}): {e}")
        return False


def rename_voice(voice_id: str, new_name: str) -> None:
    """Renames a cloned voice in place — the ID stays the same voice."""
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="name"\r\n\r\n{new_name}\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    raw = _http(
        "POST",
        f"/voices/{voice_id}/edit",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=60,
    )
    try:
        status = json.loads(raw.decode("utf-8")).get("status")
    except ValueError:
        raise RuntimeError(f"ElevenLabs rename returned non-JSON: {raw[:200]!r}")
    if status != "ok":
        raise RuntimeError(f"ElevenLabs rename failed: {raw[:200]!r}")


def clone_voice(name: str, file_paths: list[str]) -> str:
    """Instant Voice Cloning from local sample files. Returns the new voice_id."""
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()

    def field(field_name: str, value: str):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode())
        buf.write(value.encode() + b"\r\n")

    field("name", name)
    attached = 0
    for path in file_paths:
        try:
            if os.path.getsize(path) > 15 * 1024 * 1024:
                print(f"ElevenLabs clone note: skipping oversize sample {path}")
                continue
        except OSError as e:
            print(f"ElevenLabs clone note: unreadable sample {path}: {e}")
            continue
        fname = os.path.basename(path)
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="files"; filename="{fname}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(data + b"\r\n")
        attached += 1
    if not attached:
        raise RuntimeError("ElevenLabs clone got no usable sample files.")
    buf.write(f"--{boundary}--\r\n".encode())

    raw = _http(
        "POST",
        "/voices/add",
        body=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=300,
    )
    try:
        voice_id = json.loads(raw.decode("utf-8")).get("voice_id")
    except ValueError:
        raise RuntimeError(f"ElevenLabs clone returned non-JSON: {raw[:200]!r}")
    if not voice_id:
        raise RuntimeError(f"ElevenLabs clone returned no voice_id: {raw[:200]!r}")
    return voice_id
