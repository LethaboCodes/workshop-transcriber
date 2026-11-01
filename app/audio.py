"""Expose completed listening copies independently of transcript import."""
import json
import logging
import os
import shutil
import subprocess
import threading
import wave

from sqlalchemy import select

from .db import DATA_DIR, Recording, SessionLocal, recording_folder

_publish_lock = threading.Lock()


def available_audio_path(recording, version):
    folder = recording_folder(recording.id).resolve()
    stored = recording.original_path if version == "original" else recording.enhanced_path
    candidates = [DATA_DIR / stored] if stored else []
    if version == "enhanced":
        candidates.append(folder / "enhanced.m4a")
    for candidate in candidates:
        path = candidate.resolve()
        if path.is_relative_to(folder) and path.is_file() and path.stat().st_size > 0:
            return path
    return None


def publish_checkpoint_enhancement(session, recording):
    """Publish only the completed enhancement matching this recording/model."""
    with _publish_lock:
        path = available_audio_path(recording, "enhanced")
        if path is None:
            folder = recording_folder(recording.id)
            if not (folder / "checkpoints").is_dir():
                return False
            original = available_audio_path(recording, "original")
            if original is None:
                return False
            from .checkpoints import fingerprint, source_key
            from .inference import config
            checkpoint = folder / "checkpoints" / source_key(original)
            enhancement = checkpoint / ("enhance-" + fingerprint(["enhancement-v2", config()["enhancement"]]))
            source = enhancement / "enhanced.m4a"
            prepared = checkpoint / "prepared.wav"
            enhanced_wav = enhancement / "enhanced.wav"
            if not all(file.is_file() and file.stat().st_size for file in (source, prepared, enhanced_wav)):
                return False
            with wave.open(str(prepared), "rb") as before, wave.open(str(enhanced_wav), "rb") as after:
                if (before.getframerate(), before.getnchannels(), before.getnframes()) != (after.getframerate(), after.getnchannels(), after.getnframes()):
                    return False
                duration = before.getnframes() / before.getframerate()
                if duration <= 0:
                    return False
            probe = subprocess.run([os.getenv("FFPROBE_BINARY") or os.getenv("FFPROBE", "ffprobe"),
                                    "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
                                   check=True, capture_output=True, text=True, timeout=30)
            if abs(float(json.loads(probe.stdout)["format"]["duration"]) - duration) > 0.5:
                return False
            temporary = folder / "enhanced.publishing"
            shutil.copyfile(source, temporary)
            path = folder / "enhanced.m4a"
            temporary.replace(path)
        relative = path.relative_to(DATA_DIR).as_posix()
        changed = recording.enhanced_path != relative
        recording.enhanced_path = relative
        session.flush()
        return changed


def recover_available_enhancements():
    with SessionLocal() as session:
        for recording in session.scalars(select(Recording).where(Recording.original_path != "")):
            try:
                if publish_checkpoint_enhancement(session, recording):
                    logging.getLogger(__name__).info("Enhanced listening copy is available for recording %s.", recording.id)
            except (OSError, EOFError, ValueError, KeyError, wave.Error, subprocess.SubprocessError) as exc:
                logging.getLogger(__name__).warning("Could not publish enhanced audio for %s: %s", recording.id, exc)
        session.commit()
