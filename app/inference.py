"""Lazy, CPU/int8 segment recognition; shared by the GPU worker."""
import gc
import json
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
import zlib
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_models = OrderedDict()
_lock = threading.RLock()


def config():
    import yaml
    with Path(os.environ.get("MODELS_CONFIG", ROOT / "models.yaml")).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def cache_dir():
    path = Path(os.environ.get("MODEL_CACHE", ROOT / "data" / "models"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_catalog():
    return [{"key": key, "label": value.get("label", key), "languages": value["languages"]}
            for key, value in config()["asr"].items()]


def choices_for(language, model_key=None):
    catalog = config()["asr"]
    if model_key:
        if model_key not in catalog:
            raise ValueError("Unknown speech model.")
        if language not in catalog[model_key]["languages"]:
            raise ValueError("That model does not support the selected language.")
        return [model_key]
    matches = [key for key, model in catalog.items()
               if language in model.get("default_for", [])]
    if not matches:
        matches = [key for key, model in catalog.items() if language in model["languages"]][:1]
    if not matches:
        raise ValueError(f"No speech model is configured for language {language}.")
    return matches + [key for key, model in catalog.items()
                      if language in model.get("compare_for", []) and key not in matches]


def ffmpeg_binary():
    command = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not command:
        raise RuntimeError("ffmpeg is missing. Install ffmpeg or set FFMPEG_BINARY to its executable path.")
    return command


def extract_audio(path, start=0, end=None):
    import numpy as np
    arguments = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(max(0, start)), "-i", str(path)]
    if end is not None:
        if end <= start:
            raise ValueError("The segment end must be after its start.")
        arguments.extend(["-t", str(end - start)])
    arguments.extend(["-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"])
    output = subprocess.run(arguments, capture_output=True, check=False)
    if output.returncode:
        raise RuntimeError("Audio decoding failed: " + output.stderr.decode("utf-8", errors="replace")[-1500:])
    samples = np.frombuffer(output.stdout, dtype="<f4").copy()
    if not samples.size:
        raise ValueError("There is no audio inside this segment.")
    return samples


@lru_cache(maxsize=16)
def _language_code(model_id, map_filename, map_key):
    from huggingface_hub import hf_hub_download
    path = hf_hub_download(model_id, map_filename, cache_dir=str(cache_dir()), token=os.environ.get("HF_TOKEN"))
    with open(path, encoding="utf-8") as handle:
        mapping = json.load(handle)
    if map_key not in mapping:
        raise ValueError(f"Language {map_key} is missing from the model's language map.")
    return mapping[map_key]


def _load_model(model_config, device):
    from faster_whisper import WhisperModel
    model_id = model_config["id"]
    key = (model_id, device)
    if key in _models:
        _models.move_to_end(key)
        return _models[key]
    # Evict BEFORE loading another large model, keeping laptop/GPU memory bounded.
    capacity = max(1, int(os.environ.get("ASR_CACHE_MODELS", "1")))
    while len(_models) >= capacity:
        _, old = _models.popitem(last=False)
        del old
        gc.collect()
    model = WhisperModel(model_id, device=device, compute_type="int8_float16" if device == "cuda" else "int8",
                         download_root=str(cache_dir()), cpu_threads=max(1, min(8, os.cpu_count() or 4)))
    _models[key] = model
    return model


def release_models():
    with _lock:
        _models.clear()
        gc.collect()


def _is_hallucination(text, compression_limit=2.4):
    raw = text.strip().encode("utf-8")
    if not raw or len(raw) / max(1, len(zlib.compress(raw))) > compression_limit:
        return True
    words = re.findall(r"\w+", text.casefold())
    for width in range(1, min(13, len(words) // 4 + 1)):
        repeats = 6 if width == 1 else 4
        for index in range(len(words) - width * repeats + 1):
            phrase = words[index:index + width]
            if all(words[index + width * n:index + width * (n + 1)] == phrase for n in range(1, repeats)):
                return True
    return False


def transcribe_audio(audio, language, model_key, audio_version="original", start_offset=0,
                     word_list="", device="cpu", duration=None):
    model_config = config()["asr"][model_key]
    if language not in model_config["languages"]:
        raise ValueError("The speech model does not support this language.")
    code = model_config.get("language_codes", {}).get(language)
    if model_config.get("language_map_file"):
        code = _language_code(model_config["id"], model_config["language_map_file"],
                              model_config.get("language_map_keys", {}).get(language, language))
    if isinstance(word_list, (tuple, list)):
        word_list = ", ".join(str(word) for word in word_list)
    with _lock:
        model = _load_model(model_config, device)
        generated, _ = model.transcribe(audio, language=code, task="transcribe", beam_size=5,
                                        word_timestamps=True, initial_prompt=str(word_list).strip() or None,
                                        condition_on_previous_text=False, vad_filter=False,
                                        compression_ratio_threshold=2.4, log_prob_threshold=-1.0,
                                        no_speech_threshold=0.6, temperature=(0.0, 0.2, 0.4))
        parts = list(generated)
    valid = [part for part in parts if not _is_hallucination(part.text)
             and float(getattr(part, "compression_ratio", 0)) <= 2.4]
    text = " ".join(part.text.strip() for part in valid).strip()
    if _is_hallucination(text):
        return None
    words = []
    for part in valid:
        for word in part.words or []:
            start = max(0.0, float(word.start))
            end = max(start, float(word.end))
            if duration is not None:
                start, end = min(start, duration), min(end, duration)
            words.append({"word": word.word, "start": round(start_offset + start, 3),
                          "end": round(start_offset + end, 3),
                          "probability": round(max(0.0, min(1.0, float(word.probability))), 4)})
    confidence = sum(word["probability"] for word in words) / len(words) if words else (
        sum(math.exp(min(0, float(part.avg_logprob))) for part in valid) / len(valid) if valid else 0)
    return {"id": uuid.uuid4().hex, "text": text, "language": language, "model": model_key,
            "audio_version": audio_version, "confidence": round(confidence, 4), "words": words}


def recording_audio(folder, settings, version):
    folder = Path(folder).resolve()
    value = settings.get("enhanced_path", "enhanced.m4a") if version == "enhanced" else settings.get("original_path")
    if value:
        path = Path(value)
        if not path.is_absolute():
            path = folder / path
        if path.is_file():
            return path
    if version == "original":
        matches = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in
                   {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".mp4", ".webm"}
                   and not path.name.startswith(("enhanced", "prepared"))]
        if len(matches) == 1:
            return matches[0]
    raise FileNotFoundError(f"The {version} audio could not be found for this recording.")


def retranscribe(recording_folder, settings, segment, language, model_key=None):
    from app.translate import translate
    start, end = float(segment["start"]), float(segment["end"])
    versions = ["original", "enhanced"] if settings.get("asr_input", "original") == "both" else [settings.get("asr_input", "original")]
    clips = {version: extract_audio(recording_audio(recording_folder, settings, version), start, end)
             for version in versions}
    candidates = []
    for key in choices_for(language, model_key):
        for version, clip in clips.items():
            candidate = transcribe_audio(clip, language, key, version, start,
                                         settings.get("word_list", ""), "cpu", end - start)
            if candidate:
                candidates.append(candidate)
    if not candidates:
        raise ValueError("No usable speech was recognized. Try another model, audio version, or segment boundary.")
    candidates.sort(key=lambda item: item["confidence"], reverse=True)
    selected = candidates[0].copy()
    selected["english"] = translate(selected["text"], language)
    selected["alternates"] = candidates[1:]
    return selected
