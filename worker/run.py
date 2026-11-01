"""Standalone local/Kaggle recording pipeline. All model identities live in models.yaml."""
import argparse
import gc
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def log(step, message):
    print(f"[{step}] {message}", flush=True)


def command(arguments):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([str(arg) for arg in arguments], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=flags)
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace")[-2500:]
        raise RuntimeError(f"{Path(arguments[0]).name} failed: {error}")
    return result


def ffmpeg():
    executable = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg is missing. Install ffmpeg and add it to PATH, or set FFMPEG_BINARY.")
    return executable


def audio_command(*arguments):
    return command([ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *arguments])


def duration_of(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def original_file(folder, settings):
    from app.inference import recording_audio
    return recording_audio(folder, settings, "original")


def prepare(original, prepared, max_seconds=None):
    log("prepare", "Converting to mono 16 kHz WAV and normalizing listening levels.")
    arguments = ["-i", original]
    if max_seconds:
        arguments += ["-t", str(max_seconds)]
    audio_command(*arguments, "-vn", "-af", "loudnorm=I=-18:TP=-2:LRA=11", "-ar", "16000",
                  "-ac", "1", "-c:a", "pcm_s16le", prepared)
    duration = duration_of(prepared)
    if duration <= 0:
        raise ValueError("The recording contains no decodable audio.")
    log("prepare", f"Prepared {duration:.2f} seconds; original file was preserved.")
    return duration


def cached_download(url, directory, filename):
    destination = directory / filename
    if destination.is_file() and destination.stat().st_size:
        return destination
    directory.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "WorkshopTranscriber/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    temporary.replace(destination)
    return destination


def enhance(prepared, enhanced_wav, temporary, duration, settings):
    import soundfile as sf
    from app.inference import cache_dir, config
    enhancement = config()["enhancement"]
    log("enhance", f"Loading {enhancement['id']} native CPU runtime.")
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64"}.get(machine, machine)
    platform_key = platform.system() + "-" + machine
    executable = os.environ.get("DEEPFILTER_BINARY")
    model_cache = cache_dir() / "enhancement"
    if not executable:
        url = enhancement["binaries"].get(platform_key)
        if not url:
            raise RuntimeError(f"No enhancement runtime configured for {platform_key}; set DEEPFILTER_BINARY.")
        executable = cached_download(url, model_cache, url.rsplit("/", 1)[-1])
        if os.name != "nt":
            executable.chmod(0o755)
    model_url = enhancement["model_url"]
    model = cached_download(model_url, model_cache, model_url.rsplit("/", 1)[-1])
    output_dir = temporary / "denoised"
    output_dir.mkdir(exist_ok=True)
    chunk_seconds = 120
    count = max(1, int((duration + chunk_seconds - 0.001) // chunk_seconds))
    # Bounded files avoid loading a three-hour 48 kHz recording into the native
    # runtime. Context at each edge and delay compensation preserve alignment.
    with sf.SoundFile(str(enhanced_wav), "w", samplerate=16000, channels=1, subtype="PCM_16") as assembled:
        for index in range(count):
            start = index * chunk_seconds
            end = min(duration, start + chunk_seconds)
            context_start = max(0, start - 0.5)
            context_end = min(duration, end + 0.5)
            noisy = temporary / "enhance-input.wav"
            audio_command("-ss", str(context_start), "-t", str(context_end - context_start), "-i", prepared,
                          "-af", "apad=pad_dur=0.1", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", noisy)
            command([executable, "--model", model, "--compensate-delay", "--output-dir", output_dir, noisy])
            denoised = output_dir / noisy.name
            aligned = temporary / "enhance-aligned.wav"
            audio_command("-ss", str(start - context_start), "-i", denoised, "-af", "apad", "-t", str(end - start),
                          "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", aligned)
            samples, _ = sf.read(str(aligned), dtype="float32")
            required = round((end - start) * 16000)
            assembled.write(samples[:required])
            log("enhance", f"Listening audio chunk {index + 1}/{count} complete.")
    audio_command("-i", enhanced_wav, "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
                  enhanced_wav.parent / "enhanced.m4a")


def speech_regions(audio_paths, duration):
    import soundfile as sf
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    from app.inference import config
    log("segment", "Detecting speech with Silero VAD.")
    model = load_silero_vad(onnx=True)
    raw = []
    config_vad = config()["vad"]
    # Ten-minute windows bound VAD memory. A half-second overlap protects speech
    # at window edges; intervals are reunited before the 30-second limit.
    for version, path in audio_paths.items():
        with sf.SoundFile(str(path)) as audio:
            offset = 0.0
            while offset < duration:
                begin = max(0, offset - 0.5)
                stop = min(duration, offset + 600.5)
                audio.seek(round(begin * 16000))
                samples = audio.read(round((stop - begin) * 16000), dtype="float32")
                regions = get_speech_timestamps(torch.from_numpy(samples), model, sampling_rate=16000,
                                               threshold=float(config_vad.get("threshold", 0.5)),
                                               min_speech_duration_ms=250, min_silence_duration_ms=300,
                                               speech_pad_ms=100, max_speech_duration_s=30,
                                               return_seconds=False)
                raw.extend((begin + item["start"] / 16000, min(duration, begin + item["end"] / 16000))
                           for item in regions)
                offset += 600
                log("segment", f"{version}: scanned {min(duration, offset):.0f}/{duration:.0f} seconds.")
    union = []
    for start, end in sorted(raw):
        if union and start < union[-1][1] - 0.001:
            union[-1] = (union[-1][0], max(end, union[-1][1]))
        else:
            union.append((start, end))
    pause_boundaries = sorted({end for _, end in raw})
    bounded = []
    for start, end in union:
        while end - start > 30:
            # Retain the pause chosen by VAD when overlapping windows or the
            # second audio version caused two speech intervals to reunite.
            pauses = [point for point in pause_boundaries if start + 1 < point <= start + 30]
            cutoff = pauses[-1] if pauses else start + 30
            bounded.append((start, cutoff))
            start = cutoff
        if end > start:
            if bounded and start - bounded[-1][1] <= 0.75 and end - bounded[-1][0] <= 30:
                bounded[-1] = (bounded[-1][0], end)
            else:
                bounded.append((start, end))
    del model
    log("segment", f"Found {len(bounded)} speech regions.")
    return bounded


def diarize(prepared, settings, device):
    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline
    from app.inference import config
    model_id = config()["diarization"]["id"]
    log("speakers", "Running exclusive speaker diarization on the original audio.")
    pipeline = Pipeline.from_pretrained(model_id, token=os.environ["HF_TOKEN"])
    if pipeline is None:
        raise RuntimeError("Diarization model access failed. Accept its Hugging Face terms and set HF_TOKEN.")
    pipeline.to(torch.device(device))
    waveform, sample_rate = sf.read(str(prepared), dtype="float32")
    parameters = {key: int(settings[key]) for key in ("min_speakers", "max_speakers") if settings.get(key)}
    last_report = [0.0]
    def hook(step_name, step_artifact, file=None, total=None, completed=None):
        now = time.monotonic()
        if now - last_report[0] >= 25 or (total is not None and completed == total):
            log("speakers", f"{step_name}" + (f": {completed}/{total}" if total is not None else ""))
            last_report[0] = now
    output = pipeline({"waveform": torch.from_numpy(waveform).unsqueeze(0), "sample_rate": sample_rate},
                      hook=hook, **parameters)
    exclusive = output.exclusive_speaker_diarization
    turns = [(float(turn.start), float(turn.end), str(speaker))
             for turn, _, speaker in exclusive.itertracks(yield_label=True)]
    del waveform, output, pipeline
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
    log("speakers", f"Found {len(set(turn[2] for turn in turns))} speakers.")
    return sorted(turns)


def split_speakers(regions, turns):
    segments = []
    turn_cursor = 0
    for start, end in regions:
        while turn_cursor < len(turns) and turns[turn_cursor][1] <= start:
            turn_cursor += 1
        relevant = []
        for turn in turns[turn_cursor:]:
            if turn[0] >= end:
                break
            if turn[1] > start:
                relevant.append(turn)
        overlaps = {}
        for a, b, speaker in relevant:
            overlaps[speaker] = overlaps.get(speaker, 0) + max(0, min(end, b) - max(start, a))
        fallback = max(overlaps, key=overlaps.get) if overlaps else "SPEAKER_UNKNOWN"
        boundaries = sorted({start, end, *[max(start, min(end, t)) for a, b, _ in relevant for t in (a, b)]})
        local = []
        for left, right in zip(boundaries, boundaries[1:]):
            if right - left < 0.001:
                continue
            candidates = [(max(0, min(right, b) - max(left, a)), speaker) for a, b, speaker in relevant]
            score, speaker = max(candidates, default=(0, fallback))
            if score <= 0:
                speaker = local[-1]["speaker"] if local else fallback
            if local and local[-1]["speaker"] == speaker:
                local[-1]["end"] = right
            else:
                local.append({"start": left, "end": right, "speaker": speaker})
        segments.extend(local)
    return segments


def write_json(path, content):
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def run(folder, settings, max_seconds=None, prepare_only=False):
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError("The recording folder does not exist.")
    env_file = folder / "env.json"
    if env_file.is_file():
        secrets = json.loads(env_file.read_text(encoding="utf-8-sig"))
        if secrets.get("HF_TOKEN"):
            os.environ["HF_TOKEN"] = secrets["HF_TOKEN"]
        del secrets
    if not prepare_only and not os.environ.get("HF_TOKEN"):
        raise RuntimeError("Set HF_TOKEN and accept the diarization model's Hugging Face terms before running a full job. --prepare-only validates audio without model access.")
    if not prepare_only:
        log("setup", "Checking speaker, VAD, ASR and codec imports before audio preparation.")
        import torch
        import torchaudio
        import torchcodec
        from pyannote.audio import Pipeline
        from silero_vad import load_silero_vad
        from faster_whisper import WhisperModel
        log("setup", "Model-library imports passed.")
        from app.inference import check_model_access
        count = check_model_access(settings)
        log("setup", f"Verified access to {count} configured model repositories.")
    original = original_file(folder, settings)
    with tempfile.TemporaryDirectory(prefix="processing-", dir=str(folder)) as work:
        temporary = Path(work)
        prepared = temporary / "prepared.wav"
        duration = prepare(original, prepared, max_seconds)
        if prepare_only:
            destination = folder / "prepared-sample.wav"
            shutil.copyfile(prepared, destination)
            result = {"validation_only": True, "duration": duration, "prepared_audio": destination.name,
                      "original_preserved": True, "models_executed": False}
            write_json(folder / "prepare-validation.json", result)
            log("complete", "Audio preparation validation passed; no transcription or enhancement was attempted.")
            return result
        import ctranslate2
        import soundfile as sf
        import torch
        from app.inference import choices_for, release_models, transcribe_audio
        from app.translate import translate
        requested = settings.get("device", "auto")
        device = ("cuda" if torch.cuda.is_available() and ctranslate2.get_cuda_device_count() else "cpu") if requested == "auto" else requested
        if device not in {"cpu", "cuda"}:
            raise ValueError("Worker device must be auto, cpu, or cuda.")
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but PyTorch cannot access a GPU.")
        log("prepare", f"Inference device: {device}. Models download on first use.")
        languages = list(dict.fromkeys(settings.get("allowed_languages") or ["en", "xh", "af"]))
        for language in languages:
            choices_for(language)
        asr_input = settings.get("asr_input", "original")
        if asr_input not in {"original", "enhanced", "both"}:
            raise ValueError("ASR input must be original, enhanced, or both.")
        if settings.get("min_speakers") and settings.get("max_speakers") and int(settings["min_speakers"]) > int(settings["max_speakers"]):
            raise ValueError("Minimum speakers cannot exceed maximum speakers.")
        enhanced_wav = temporary / "enhanced.wav"
        enhance(prepared, enhanced_wav, temporary, duration, settings)
        shutil.copyfile(temporary / "enhanced.m4a", folder / "enhanced.m4a")
        audio_paths = {"original": prepared, "enhanced": enhanced_wav}
        selected_paths = audio_paths if asr_input == "both" else {asr_input: audio_paths[asr_input]}
        regions = speech_regions(selected_paths, duration)
        gpu_seconds = 0.0
        stage_start = time.monotonic()
        turns = diarize(prepared, settings, device) if regions else []
        if device == "cuda":
            gpu_seconds += time.monotonic() - stage_start
        segments = split_speakers(regions, turns)
        candidates = [[] for _ in segments]
        # Visit models first, keeping one large ASR model resident for its entire
        # pass instead of reloading it for every segment.
        passes = [(key, language) for language in languages for key in choices_for(language)]
        passes.sort(key=lambda item: item[0])
        total = len(passes) * len(selected_paths) * len(segments)
        done = 0
        stage_start = time.monotonic()
        for model_key, language in passes:
            log("transcribe", f"Running {model_key} for {language}.")
            for version, path in selected_paths.items():
                with sf.SoundFile(str(path)) as audio:
                    for index, segment in enumerate(segments):
                        audio.seek(round(segment["start"] * 16000))
                        samples = audio.read(max(1, round((segment["end"] - segment["start"]) * 16000)), dtype="float32")
                        candidate = transcribe_audio(samples, language, model_key, version, segment["start"],
                                                     settings.get("word_list", ""), device,
                                                     segment["end"] - segment["start"])
                        if candidate:
                            candidates[index].append(candidate)
                        done += 1
                        log("transcribe", f"{done}/{total} hypotheses: segment {index + 1}, {language}, {version}.")
        release_models()
        if device == "cuda":
            gpu_seconds += time.monotonic() - stage_start
            torch.cuda.empty_cache()
        for index, segment in enumerate(segments):
            hypotheses = sorted(candidates[index], key=lambda item: item["confidence"], reverse=True)
            winner = hypotheses[0] if hypotheses else {"text": "", "language": languages[0], "confidence": 0,
                                                       "words": [], "model": choices_for(languages[0])[0],
                                                       "audio_version": next(iter(selected_paths))}
            language = winner["language"]
            english = translate(winner["text"], language)
            segment.update({"start": round(segment["start"], 3), "end": round(segment["end"], 3),
                            "language": language, "suggested_language": language,
                            "machine_text": winner["text"], "text": winner["text"],
                            "machine_english": english, "english": english, "english_machine_generated": True,
                            "confidence": winner["confidence"], "words": winner["words"],
                            "alternates": hypotheses[1:], "model": winner["model"],
                            "audio_version": winner["audio_version"]})
            log("translate", f"Translated segment {index + 1}/{len(segments)} to English.")
        speaker_ids = list(dict.fromkeys(segment["speaker"] for segment in segments))
        result = {"duration": duration, "enhanced_path": "enhanced.m4a", "gpu_seconds": round(gpu_seconds, 3),
                  "speakers": [{"id": speaker, "name": "Unknown speaker" if speaker == "SPEAKER_UNKNOWN" else f"Speaker {index + 1}"}
                               for index, speaker in enumerate(speaker_ids)], "segments": segments,
                  "device": device, "partial_sample": bool(max_seconds)}
        write_json(folder / "result.json", result)
        log("complete", f"Saved {len(segments)} segments, enhanced audio, and {gpu_seconds:.1f} estimated GPU seconds.")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording_folder", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--max-seconds", type=float, help="Limit the input for a short real-audio validation run.")
    parser.add_argument("--prepare-only", action="store_true", help="Validate ffmpeg preparation only; produces no transcript.")
    arguments = parser.parse_args()
    if arguments.max_seconds is not None and arguments.max_seconds <= 0:
        parser.error("--max-seconds must be positive")
    settings_path = arguments.settings or arguments.recording_folder / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path.exists() else {}
        run(arguments.recording_folder, settings, arguments.max_seconds, arguments.prepare_only)
    except Exception as exc:
        message = str(exc)
        details = traceback.format_exc()
        for name in ("HF_TOKEN", "KAGGLE_KEY"):
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, "[REDACTED]")
                details = details.replace(secret, "[REDACTED]")
        log("failed", message)
        print(details, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
