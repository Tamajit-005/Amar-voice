# Amar Voice — A Voice That Stays Theirs

*Built for a friend — or a grandparent — whose voice is fading, so the people around them still hear **them** — in English, Hindi, and Bengali.*

When speaking gets exhausting, even asking for water becomes hard. Amar Voice is a
big-button phone app built for anyone with vocal fatigue or voice loss: they tap a
☕ tea, 😊 I'm fine, 🚨 help — and the phone speaks **in their own cloned voice**.
Anything else can be typed into the custom-message box. It installs like an app,
works offline for the everyday phrases, and lets the family add, rename, or remove
voice profiles from the phone itself.

## Who it's for

A friend, grandparent, or anyone with vocal fatigue or voice loss — and the people
around them.
Everything about the interface assumes shaky hands and tired eyes: giant
high-contrast cards, three language pills (English / हिंदी / বাংলা), one speaker
menu, zero settings pages.

## What happens when you tap

1. **Quick cards** play pre-generated audio instantly (0ms) — 6 phrases × 3 languages,
   cached on the server per speaker and in the phone's offline cache.
2. **Custom text** (up to 1000 chars) is synthesized in the selected person's voice
   in ~2–4 seconds, with a spinner, playback, and a capped MP3 download (350 KB).
3. **First tap for a new voice is slow, then instant** — missing audio generates
   once, then caches. A background job rebuilds all 18 presets whenever clips are
   added, with live progress in the upload drawer.

## How the voices work

Each person gets a profile (e.g. `demo`): a few 10–25s voice notes in any language.
The backend clones the voice once via ElevenLabs v4 Instant Voice Cloning and reuses
that voice ID everywhere — so the same person speaks English, Hindi, and Bengali
with their own timbre and native accents. Each phone gets its own private space
(a random ID kept in that browser only): voices you add are visible on your device
alone, while `default` is shared by everyone. Uploading more clips under the same name
re-clones from the full set; renaming keeps the same voice ID; deleting removes the
clone, mapping, cached audio, and clips. `default` is pinned to one
`ELEVENLABS_DEFAULT_VOICE_ID` voice and can't be renamed, deleted, or overwritten.

Under the hood: FastAPI backend (`backend/main.py` routes, `backend/engine.py`
voice management, `backend/eleven.py` API client), single-file PWA frontend with a
service worker, per-IP rate limits on the paid endpoints, atomic cache writes so
concurrent taps never serve half-written audio.

## Honest trade-offs

- **Cloud, not offline synthesis.** First versions ran fully local models; they
  sounded distant from the real voice, so synthesis moved to ElevenLabs v4. Preset
  taps still work offline once cached; custom speech needs internet (~2–4s).
- **It costs characters.** Every new sentence is a paid API call (free tier +
  Hacktoberfest promo credits cover demo use). Rate limits and 18-preset batching
  keep accidents small.
- **Voice data leaves the device.** Only the `demo` voice ships in git; every other
  profile is gitignored, and the API key lives in server-side `.env` only.

## Run it yourself

```bash
uv venv --python 3.10 venv && source venv/bin/activate
uv pip install -r requirements.txt
sudo apt install -y ffmpeg
cp .env.example .env  # paste ELEVENLABS_API_KEY (+ optional ELEVENLABS_DEFAULT_VOICE_ID)
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` (or the Render URL — `render.yaml` + `Dockerfile`
included, just add the two env vars). `python test_suite.py` runs the checks.
