import glob
import json
import os
import re

# Ensure repo root anchor (kept for main.py's REPO_ROOT import compatibility)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from backend import eleven

# Single source of truth for quick-card presets (shared by precompute + on-demand fallback)
PRESETS = [
    {"id": "water", "text": "I need some water, please...", "lang": "en"},
    {"id": "water_hi", "text": "मुझे थोड़ा पानी चाहिए...", "lang": "hi"},
    {"id": "medicine", "text": "Time for my medicine...", "lang": "en"},
    {"id": "medicine_hi", "text": "मेरी दवाई का समय हो गया है...", "lang": "hi"},
    {"id": "call", "text": "Please call my family member right now!", "lang": "en"},
    {"id": "call_hi", "text": "कृपया मेरे परिवार के किसी सदस्य को बुलाओ!", "lang": "hi"},
    {"id": "tea", "text": "I would like some warm tea.", "lang": "en"},
    {"id": "tea_hi", "text": "मुझे थोड़ी गर्म चाय चाहिए।", "lang": "hi"},
    {"id": "fine", "text": "I am doing fine, do not worry.", "lang": "en"},
    {"id": "fine_hi", "text": "मैं बिल्कुल ठीक हूँ, चिंता मत करो।", "lang": "hi"},
    {"id": "help", "text": "I need help, please come here!", "lang": "en"},
    {"id": "help_hi", "text": "मुझे मदद चाहिए, कृपया यहाँ आओ!", "lang": "hi"},
    {"id": "water_bn", "text": "আমার একটু জল দরকার, দয়া করে...", "lang": "bn"},
    {"id": "medicine_bn", "text": "আমার ওষুধ খাওয়ার সময় হয়েছে...", "lang": "bn"},
    {"id": "call_bn", "text": "দয়া করে এখনই আমার পরিবারের কাউকে ডাকো!", "lang": "bn"},
    {"id": "tea_bn", "text": "আমাকে একটু গরম চা দাও।", "lang": "bn"},
    {"id": "fine_bn", "text": "আমি ভালো আছি, চিন্তা কোরো না।", "lang": "bn"},
    {"id": "help_bn", "text": "আমার সাহায্য দরকার, দয়া করে এখানে এসো!", "lang": "bn"},
]

PRESET_MAP = {p["id"]: p for p in PRESETS}

# Single fixed delivery: ElevenLabs prosody carries the expression, so there is
# no speed/emotion plumbing (the old local-engine knobs are gone).

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".opus", ".ogg")
GLOB_EXTS = ("*.wav", "*.mp3", "*.m4a", "*.opus", "*.ogg")

SHARED_NS = "shared"


def _map_file(ns: str) -> str:
    if ns == SHARED_NS:
        return os.path.join(REPO_ROOT, "assets", "voices", "eleven_voices.json")
    return os.path.join(REPO_ROOT, "assets", "voices", ns, "eleven_voices.json")


class VoiceEngine:
    """ElevenLabs-backed voice engine with per-device namespaces.

    ns == "shared" uses the legacy flat paths (assets/voices/<speaker>,
    backend/static_presets/<speaker>), so existing data keeps working with
    zero migration. Any other ns is isolated under voices/<ns>/ and
    static_presets/<ns>/. The "default" speaker is global (env-pinned) and
    never namespaced, so its presets are generated once, not per device.
    """

    provider = "elevenlabs"

    def __init__(self, base_assets_dir=None, device="cpu"):
        self.device = device  # kept for API compat; inference is in the cloud
        self.repo_root = REPO_ROOT
        self.base_assets_dir = base_assets_dir or os.path.join(REPO_ROOT, "assets")
        self.voices_dir = os.path.join(self.base_assets_dir, "voices")
        os.makedirs(self.voices_dir, exist_ok=True)
        # Compat attrs (old health endpoint shape); kept so nothing else breaks
        self.pipelines = {"en": "elevenlabs", "hi": "elevenlabs"}
        self.converter = True

        self.model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_v4")
        # Local model sampling rate kept for the upload resample path in main.py
        self.target_sampling_rate = 22050

        self.cache_dir = os.path.join(REPO_ROOT, "backend", "static_presets")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._drop_stale_local_cache()

        self.key_present = bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())
        if not self.key_present:
            print("⚠️ [Engine] ELEVENLABS_API_KEY not set — set it before serving speech.")

        self.speaker_profiles = {}  # { (ns, speaker_name): elevenlabs_voice_id }
        self._maps = {}  # { ns: { speaker_name: elevenlabs_voice_id } }
        self.load_all_speakers(SHARED_NS)

    # ----- namespace helpers -----

    def _vbase(self, ns: str) -> str:
        """Base dir holding a namespace's speaker folders (and shared flat files)."""
        return self.voices_dir if ns == SHARED_NS else os.path.join(self.voices_dir, ns)

    def _cbase(self, ns: str) -> str:
        """Base dir holding a namespace's preset cache."""
        return self.cache_dir if ns == SHARED_NS else os.path.join(self.cache_dir, ns)

    def _get_map(self, ns: str) -> dict:
        if ns not in self._maps:
            self._maps[ns] = self._load_map(ns)
        return self._maps[ns]

    # ----- internal helpers -----

    def _load_map(self, ns: str) -> dict:
        path = _map_file(ns)
        for candidate in (path, path + ".bak"):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if candidate.endswith(".bak"):
                            print(f"[Engine] Restored voice map from backup ({ns}).")
                        return data
            except (OSError, ValueError):
                continue
        return {}

    def _save_map(self, ns: str) -> None:
        path = _map_file(ns)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._get_map(ns), f, indent=2)
        try:
            if os.path.exists(path):
                os.replace(path, path + ".bak")
        except OSError:
            pass
        os.replace(tmp, path)

    def _drop_stale_local_cache(self) -> None:
        """One-time wipe of WAVs generated by the retired Kokoro/OpenVoice engine."""
        marker = os.path.join(self.cache_dir, ".provider")
        try:
            current = open(marker).read().strip() if os.path.exists(marker) else ""
        except OSError:
            current = ""
        if current == self.provider:
            return
        removed = 0
        for root, _, files in os.walk(self.cache_dir):
            for fn in files:
                if fn.endswith(".wav"):
                    try:
                        os.remove(os.path.join(root, fn))
                        removed += 1
                    except OSError:
                        pass
        try:
            with open(marker, "w") as f:
                f.write(self.provider)
        except OSError:
            pass
        if removed:
            print(f"🧹 [Engine] Cleared {removed} stale local-engine WAV(s); presets will regenerate via ElevenLabs.")

    # ----- speaker management -----

    def _iter_ns_items(self, ns: str):
        """Yields (is_dir, name) entries of a namespace's voices base."""
        base = self._vbase(ns)
        if not os.path.isdir(base):
            return
        for item in sorted(os.listdir(base)):
            item_path = os.path.join(base, item)
            if os.path.isdir(item_path):
                if ns == SHARED_NS and (
                    re.match(r"^u[0-9a-f]{12}$", item)
                    or os.path.exists(os.path.join(item_path, "eleven_voices.json"))
                ):
                    continue  # device namespace dir, not a speaker
                yield True, item
            elif item.endswith(AUDIO_EXTS):
                yield False, os.path.splitext(item)[0]

    def get_speaker_audio_files(self, speaker_name: str, ns: str = SHARED_NS):
        """Finds local sample clips used as cloning sources for a speaker."""
        speaker_dir = os.path.join(self._vbase(ns), speaker_name)
        if os.path.isdir(speaker_dir):
            files = []
            for ext in GLOB_EXTS:
                files.extend(glob.glob(os.path.join(speaker_dir, ext)))
            files = [f for f in files if not f.endswith(".json")]
            if files:
                return sorted(files)

        # Legacy single-file layout only exists in the shared namespace
        if ns == SHARED_NS:
            for ext in AUDIO_EXTS:
                file_path = os.path.join(self.voices_dir, f"{speaker_name}{ext}")
                if os.path.exists(file_path):
                    return [file_path]
        return []

    def load_speaker_profile(self, speaker_name: str, ns: str = SHARED_NS):
        """Resolves (and clones if needed) the ElevenLabs voice_id for a speaker.

        'default' is pinned to ELEVENLABS_DEFAULT_VOICE_ID only — uploads and
        stored mappings never change it, so it sounds identical in every language.
        """
        key = (ns, speaker_name)
        if key in self.speaker_profiles:
            return self.speaker_profiles[key]

        if speaker_name == "default":
            voice_id = os.environ.get("ELEVENLABS_DEFAULT_VOICE_ID", "").strip() or None
            if voice_id:
                self.speaker_profiles[key] = voice_id
                return voice_id
            return None

        # 1. Known mapping (persisted from a previous clone)
        voice_map = self._get_map(ns)
        voice_id = voice_map.get(speaker_name)
        if voice_id:
            self.speaker_profiles[key] = voice_id
            return voice_id

        # 2. Clone from local sample clips (upload more clips = better clone)
        audio_files = self.get_speaker_audio_files(speaker_name, ns)
        if not audio_files:
            return None
        print(f"🔄 [Engine] Cloning ElevenLabs voice for '{speaker_name}' from {len(audio_files)} sample(s)...")
        try:
            old_id = voice_map.get(speaker_name)
            new_id = eleven.clone_voice(f"amar-voice-{speaker_name}", audio_files)
            if old_id and old_id != new_id and not eleven.delete_voice(old_id):
                print(f"⚠️ [Engine] Old clone {old_id} may be orphaned (delete failed).")
            voice_map[speaker_name] = new_id
            self._save_map(ns)
            self.speaker_profiles[key] = new_id
            print(f"✅ [Engine] Cloned speaker '{speaker_name}' -> {new_id}.")
            return new_id
        except Exception as e:
            print(f"❌ [Engine] Failed to clone '{speaker_name}': {e}")
            return None

    def reclone_speaker(self, speaker_name: str, ns: str = SHARED_NS):
        """Re-clones from ALL current clips (new uploads included), replacing the old voice.

        Uploads land in voices/<ns>/<speaker>/ first, so re-cloning picks them up.
        Returns the voice_id, or the existing one when there is nothing to clone from.
        'default' is env-pinned and never re-cloned.
        """
        if speaker_name == "default":
            return self.load_speaker_profile("default", ns)
        key = (ns, speaker_name)
        voice_map = self._get_map(ns)
        audio_files = self.get_speaker_audio_files(speaker_name, ns)
        if not audio_files:
            return self.speaker_profiles.get(key) or voice_map.get(speaker_name)
        print(f"🔄 [Engine] Re-cloning ElevenLabs voice for '{speaker_name}' from {len(audio_files)} sample(s)...")
        try:
            old_id = voice_map.get(speaker_name)
            new_id = eleven.clone_voice(f"amar-voice-{speaker_name}", audio_files)
            if old_id and old_id != new_id and not eleven.delete_voice(old_id):
                print(f"⚠️ [Engine] Old clone {old_id} may be orphaned (delete failed).")
            voice_map[speaker_name] = new_id
            self._save_map(ns)
            self.speaker_profiles[key] = new_id
            print(f"✅ [Engine] Re-cloned speaker '{speaker_name}' -> {new_id}.")
            return new_id
        except Exception as e:
            print(f"❌ [Engine] Failed to re-clone '{speaker_name}': {e}")
            return self.speaker_profiles.get(key) or voice_map.get(speaker_name)

    def delete_speaker(self, speaker_name: str, ns: str = SHARED_NS) -> dict:
        """Deletes the cloud clone, mapping, cached audio, and local clips for a speaker."""
        import shutil as _shutil
        voice_map = self._get_map(ns)
        known = (
            speaker_name in voice_map
            or (ns, speaker_name) in self.speaker_profiles
            or os.path.exists(os.path.join(self._cbase(ns), speaker_name))
            or os.path.exists(os.path.join(self._vbase(ns), speaker_name))
        )
        if not known:
            raise ValueError(f"No voice profile '{speaker_name}'.")
        voice_id = voice_map.pop(speaker_name, None)
        clone_gone = True
        if voice_id:
            clone_gone = eleven.delete_voice(voice_id)
        self.speaker_profiles.pop((ns, speaker_name), None)
        self._save_map(ns)
        removed = {"clone": voice_id is not None and clone_gone,
                   "clone_orphaned": bool(voice_id) and not clone_gone}
        for path in (
            os.path.join(self._cbase(ns), speaker_name),
            os.path.join(self._vbase(ns), speaker_name),
        ):
            if os.path.exists(path):
                try:
                    if os.path.isdir(path):
                        _shutil.rmtree(path)
                    else:
                        os.remove(path)
                    removed[path] = True
                except OSError as e:
                    removed[path] = f"failed: {e}"
        return removed

    def rename_speaker(self, old_name: str, new_name: str, ns: str = SHARED_NS):
        """Renames a profile locally and on ElevenLabs; the voice ID is kept,
        so it stays pointed at the same person."""
        if old_name == "default" or new_name == "default":
            raise ValueError("The default voice cannot be renamed.")
        if old_name == new_name:
            raise ValueError("Old and new names are identical.")
        voice_map = self._get_map(ns)
        known = set(voice_map) | {spk for (n, spk) in self.speaker_profiles if n == ns}
        for _, name in self._iter_ns_items(ns):
            known.add(name)
        if old_name not in known:
            raise ValueError(f"No voice profile '{old_name}'.")
        if new_name in known or os.path.exists(os.path.join(self._vbase(ns), new_name)):
            raise ValueError(f"A voice profile '{new_name}' already exists.")
        voice_id = voice_map.get(old_name)
        if voice_id:
            eleven.rename_voice(voice_id, f"amar-voice-{new_name}")
        for base in (self._vbase(ns), self._cbase(ns)):
            src, dst = os.path.join(base, old_name), os.path.join(base, new_name)
            if os.path.exists(src) and not os.path.exists(dst):
                os.rename(src, dst)
        if old_name in voice_map:
            voice_map[new_name] = voice_map.pop(old_name)
            self._save_map(ns)
        key_old, key_new = (ns, old_name), (ns, new_name)
        if key_old in self.speaker_profiles:
            self.speaker_profiles[key_new] = self.speaker_profiles.pop(key_old)
        return voice_id

    def load_all_speakers(self, ns: str = SHARED_NS):
        """Discovers speakers from voices dirs + persisted voice map (no heavy models)."""
        voice_map = self._get_map(ns)
        names: set[str] = set(voice_map.keys())
        for _, name in self._iter_ns_items(ns):
            names.add(name)
        names.add("default")
        for name in sorted(names):
            key = (ns, name)
            if key not in self.speaker_profiles:
                # Prefer already-mapped voices; cloning of unmapped speakers with
                # local clips happens lazily on first use (needs API key anyway).
                voice_id = voice_map.get(name)
                if voice_id:
                    self.speaker_profiles[key] = voice_id
        if (ns, "default") not in self.speaker_profiles:
            default_id = os.environ.get("ELEVENLABS_DEFAULT_VOICE_ID", "").strip()
            if default_id:
                self.speaker_profiles[(ns, "default")] = default_id

    def list_speakers(self, ns: str = SHARED_NS):
        """Returns known speaker names (folders + mapped voices)."""
        discovered: set[str] = {spk for (n, spk) in self.speaker_profiles if n == ns}
        discovered |= set(self._get_map(ns).keys())
        for _, name in self._iter_ns_items(ns):
            discovered.add(name)
        discovered.add("default")
        return sorted(discovered)

    def _resolve_voice(self, speaker: str, ns: str = SHARED_NS) -> str:
        # No silent fallback: serving default's voice as the requested speaker
        # misleads the listener. Callers surface the honest error instead.
        voice_id = self.speaker_profiles.get((ns, speaker))
        if voice_id is None:
            voice_id = self.load_speaker_profile(speaker, ns)
        if not voice_id:
            raise RuntimeError(
                f"No ElevenLabs voice for '{speaker}'. Upload voice samples for it first."
            )
        return voice_id

    # ----- synthesis -----

    def synthesize(self, text: str, lang: str = "en", speaker: str = "default",
                   out_path: str = "output.wav", ns: str = SHARED_NS):
        """Synthesizes speech via ElevenLabs in the speaker's cloned voice.

        lang selects intent only ('hi' hints Devanagari text); the multilingual
        model auto-detects language and carries expression in its prosody.

        Cloud HTTP calls are thread-safe, so concurrent requests synthesize in
        parallel (no global lock). Output is written to a unique temp file then
        atomically moved, so concurrent same-target writes never interleave.
        """
        voice_id = self._resolve_voice(speaker, ns)
        mp3_bytes = eleven.text_to_speech(
            text=text,
            voice_id=voice_id,
            model_id=self.model_id,
            stability=0.55,
            style=0.35,
        )
        if not mp3_bytes or len(mp3_bytes) < 1000:
            raise RuntimeError("ElevenLabs returned empty audio. Retry in a moment.")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        # Cache layer serves .wav; convert cloud MP3 via ffmpeg-backed pydub
        from pydub import AudioSegment
        import io as _io
        import tempfile as _tempfile
        audio = AudioSegment.from_file(_io.BytesIO(mp3_bytes), format="mp3")
        audio = audio.set_channels(1).set_frame_rate(self.target_sampling_rate)
        fd, tmp_wav = _tempfile.mkstemp(suffix=".wav", dir=os.path.dirname(os.path.abspath(out_path)))
        os.close(fd)
        try:
            audio.export(tmp_wav, format="wav")
            os.replace(tmp_wav, out_path)  # atomic: readers never see a partial file
        finally:
            try:
                if os.path.exists(tmp_wav):
                    os.remove(tmp_wav)
            except OSError:
                pass
        return out_path

    def precompute_presets(self, ns: str = SHARED_NS):
        """Precomputes quick-card preset audio files to guarantee 0ms latency."""
        if not self.key_present:
            print("[Engine] Skipping preset precompute: ELEVENLABS_API_KEY not set.")
            return
        for spk in self.list_speakers(ns):
            # Only warm speakers that actually resolve (skip ones with no voice yet)
            try:
                self._resolve_voice(spk, ns)
            except Exception as e:
                print(f"Preset warm-up skipped for '{spk}': {e}")
                continue
            spk_cache_dir = os.path.join(self._cbase(ns), spk)
            os.makedirs(spk_cache_dir, exist_ok=True)
            for p in PRESETS:
                out_file = os.path.join(spk_cache_dir, f"{p['id']}.wav")
                if not os.path.exists(out_file):
                    try:
                        self.synthesize(text=p["text"], lang=p["lang"], speaker=spk, out_path=out_file, ns=ns)
                    except Exception as e:
                        print(f"Could not precompute preset {p['id']} for {spk}: {e}")

    def refresh_speaker_presets(self, speaker_name: str, progress=None, ns: str = SHARED_NS):
        """Regenerates a speaker's presets in the (new) voice.

        progress(done, total) is called after each preset for lazy-loader UIs.

        Each preset is atomically replaced in place, so there is never a window
        with zero presets (refresh is restart-safe: rerun to fill any gaps).
        """
        spk_cache_dir = os.path.join(self._cbase(ns), speaker_name)
        os.makedirs(spk_cache_dir, exist_ok=True)
        total = len(PRESETS)
        for i, p in enumerate(PRESETS, 1):
            out_file = os.path.join(spk_cache_dir, f"{p['id']}.wav")
            try:
                self.synthesize(text=p["text"], lang=p["lang"], speaker=speaker_name, out_path=out_file, ns=ns)
            except Exception as e:
                print(f"Could not refresh preset {p['id']} for {speaker_name}: {e}")
            if progress:
                try:
                    progress(i, total)
                except Exception:
                    pass
