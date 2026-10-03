# Amar Voice — A Voice That Stays Theirs

Assistive speech PWA for elderly family members experiencing vocal fatigue or loss:
trilingual quick-cards (EN/HI/BN) plus custom text, spoken in each person's own
cloned voice (ElevenLabs v4), with MP3 downloads and per-profile voice management.

## System Architecture

```text
[Mobile Phone (Grandparent)]
      │
      │  Tap Big Button / Type Custom Text
      ▼
[PWA Frontend (HTML5 / Resilient Offline CSS / ServiceWorker)]
      │
      │  POST /api/speak { text: "...", language: "en" | "hi" | "bn", speaker: "sp" }
      ▼
[Backend (FastAPI / Python 3.10)]
      ├── Static Presets Cache: Precomputed WAVs (0ms response)
      └── ElevenLabs v4 (cloned voices, EN + HI + BN) + MP3 downloads capped at 350 KB
```

> Setup needs `ELEVENLABS_API_KEY`: `cp .env.example .env` and paste the key
> (free credits: `hacktoberfest.com/my/promos`). Never commit `.env`.

## Quick Start

### 1. Prerequisites
- Python 3.10
- `ffmpeg` installed on host:
  ```bash
  sudo apt install -y ffmpeg
  ```
- An ElevenLabs API key (`ELEVENLABS_API_KEY`); `default` voice via `ELEVENLABS_DEFAULT_VOICE_ID`.

### 2. Environment Setup
```bash
uv venv --python 3.10 venv
source venv/bin/activate
uv pip install -r requirements.txt
```

### 3. Launch the Server
```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` on any device on your local network.

## Testing

Run the test suite:
```bash
python test_suite.py
```

## License
MIT License.
