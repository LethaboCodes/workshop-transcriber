"""Standalone local/Kaggle recording pipeline. All model identities live in models.yaml."""
import argparse
import gc
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def valid_audio(path, seconds=None):
    try:
        with wave.open(str(path), "rb") as audio:
            valid = audio.getframerate() == 16000 and audio.getnchannels() == 1 and audio.getsampwidth() == 2
            return valid and audio.getnframes() > 0 and (seconds is None or abs(audio.getnframes() - round(seconds * 16000)) <= 1)
    except (OSError, EOFError, wave.Error):
        return False


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
    chunk_seconds = 120
    count = max(1, int((duration + chunk_seconds - 0.001) // chunk_seconds))
    chunks = enhanced_wav.parent / "chunks"
    chunks.mkdir(exist_ok=True)
    pending = [index for index in range(count) if not valid_audio(
        chunks / f"{index:05d}.wav", min(duration - index * chunk_seconds, chunk_seconds))]
    if len(pending) < count:
        log("resume", f"Reusing {count - len(pending)}/{count} completed enhancement chunks.")
    workers = min(2, os.cpu_count() or 1, max(1, len(pending)))
    log("enhance", f"Processing {len(pending)} remaining chunks with {workers} CPU workers.")

    def process_chunk(index):
        # Each task owns its scratch files; only a fully aligned chunk is committed.
        with tempfile.TemporaryDirectory(prefix="enhance-", dir=str(temporary)) as scratch:
            scratch = Path(scratch)
            output_dir = scratch / "denoised"
            output_dir.mkdir()
            start = index * chunk_seconds
            end = min(duration, start + chunk_seconds)
            context_start = max(0, start - 0.5)
            context_end = min(duration, end + 0.5)
            noisy = scratch / "enhance-input.wav"
            audio_command("-ss", str(context_start), "-t", str(context_end - context_start), "-i", prepared,
                          "-af", "apad=pad_dur=0.1", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", noisy)
            command([executable, "--model", model, "--compensate-delay", "--output-dir", output_dir, noisy])
            denoised = output_dir / noisy.name
            aligned = scratch / "enhance-aligned.wav"
            audio_command("-ss", str(start - context_start), "-i", denoised, "-af", "apad", "-t", str(end - start),
                          "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", aligned)
            if not valid_audio(aligned, end - start):
                raise RuntimeError(f"Enhancement chunk {index + 1} has an invalid length or format.")
            aligned.replace(chunks / f"{index:05d}.wav")

    # Native DeepFilterNet is CPU-bound. Two independent processes use available
    # cores while bounding RAM; the model and audio quality settings are unchanged.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_chunk, index): index for index in pending}
        completed = count - len(pending)
        try:
            for future in as_completed(futures):
                future.result()
                completed += 1
                log("enhance", f"Listening audio chunk {futures[future] + 1}/{count} complete ({completed}/{count} saved).")
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    assembled_path = enhanced_wav.with_name("enhanced.partial.wav")
    with sf.SoundFile(str(assembled_path), "w", samplerate=16000, channels=1, subtype="PCM_16") as assembled:
        for index in range(count):
            samples, _ = sf.read(str(chunks / f"{index:05d}.wav"), dtype="int16")
            assembled.write(samples)
    assembled_path.replace(enhanced_wav)
    encode_enhanced(enhanced_wav)
    shutil.rmtree(chunks)


def encode_enhanced(enhanced_wav):
    target = enhanced_wav.parent / "enhanced.m4a"
    temporary = target.with_name("enhanced.partial.m4a")
    audio_command("-i", enhanced_wav, "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", temporary)
    temporary.replace(target)


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


def run_asr_processes(task_path, device, count, validate=False, translation=False, progress=None):
    """Only pipe readers use threads; each GPU model lives in its own process."""
    messages = queue.Queue()
    processes, readers = [], []
    failures = {}
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")
    try:
        for shard in range(count):
            environment = dict(os.environ)
            physical = visible[shard].strip() if len(visible) >= count and all(visible) else str(shard)
            environment["CUDA_VISIBLE_DEVICES"] = physical if device == "cuda" and not translation else ""
            environment["ASR_PHYSICAL_DEVICE"] = physical
            environment["OMP_NUM_THREADS"] = "1"
            arguments = [sys.executable, "-u", str(ROOT / "worker/asr.py"), str(task_path),
                         "--shard", str(shard), "--count", str(count)]
            if validate:
                arguments.append("--validate")
            if translation:
                arguments.append("--translation")
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=environment)
            processes.append(process)
            def read_output(index, child):
                try:
                    for line in child.stdout:
                        messages.put((index, line))
                finally:
                    messages.put((index, None))
            reader = threading.Thread(target=read_output, args=(shard, process), daemon=True)
            reader.start()
            readers.append(reader)
        ended = set()
        while len(ended) < len(processes):
            try:
                index, line = messages.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                ended.add(index)
                code = processes[index].wait()
                if code:
                    detail = failures.get(index, "see its traceback above")
                    raise RuntimeError(f"Isolated {'translation' if translation else 'ASR'} worker {index + 1} exited with code {code}: {detail}. Completed hypotheses remain checkpointed.")
            elif line.startswith("[asr-progress] "):
                if progress:
                    progress(json.loads(line[len("[asr-progress] "):]))
            else:
                if line.startswith(("RuntimeError:", "ValueError:", "ImportError:", "ModuleNotFoundError:", "OSError:", "KeyError:")):
                    failures[index] = line.strip()
                print(line.rstrip(), flush=True)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for reader in readers:
            reader.join(timeout=1)


def recognize_segments(segments, paths, checkpoint, enhancement_key, languages, settings, device, temporary, publish=None):
    import ctranslate2
    from app.checkpoints import fingerprint, recognition_key
    from app.inference import choices_for, config
    catalog = config()
    passes = sorted([(key, language) for language in languages for key in choices_for(language)])
    groups = {}
    total = len(passes) * len(paths) * len(segments)
    cached_count = 0
    for model_key, language in passes:
        group = groups.setdefault(model_key, {"model_key": model_key, "segments": segments, "passes": [],
                                              "device": device, "word_list": settings.get("word_list", ""),
                                              "coverage_mode": True, "recovery": bool(settings.get("_recovery")),
                                              "validation_index": settings.get("_validation_index",0),
                                              "language_probe_seconds": catalog.get("pipeline", {}).get("language_probe_seconds",6)})
        for version, path in paths.items():
            key = recognition_key(catalog,model_key,language,version,enhancement_key,segments,settings,device)
            folder = checkpoint / ("asr-" + key)
            missing = 0
            for index in range(len(segments)):
                try:
                    json.loads((folder / f"{index:05d}.json").read_text())
                    cached_count += 1
                except (OSError, ValueError):
                    missing += 1
            group["passes"].append({"language": language, "version": version, "audio": str(path),
                                    "checkpoint": str(folder), "missing": missing,
                                    "evidence_checkpoint": str(checkpoint / ("language-" + fingerprint([
                                        catalog["asr"][model_key],version,enhancement_key if version=="enhanced" else None,
                                        segments,device,group["language_probe_seconds"]])))})
    count = max(1, min(2, ctranslate2.get_cuda_device_count())) if device == "cuda" else 1
    log("resume", f"{cached_count}/{total} recognition hypotheses already checkpointed.")
    log("transcribe", f"Using {count} isolated ASR processes, one fixed GPU per process; keeping all {len(passes)} model/language passes.")
    pending = []
    for index, group in enumerate(groups.values()):
        path = temporary / f"asr-group-{index}.json"
        write_json(path, group)
        if any(item["missing"] for item in group["passes"]):
            pending.append((path, group))
        else:
            log("resume", f"Reusing every completed {group['model_key']} hypothesis; its model will not be loaded.")
    completed_count = 0
    def collect():
        nonlocal completed_count
        candidates = [[] for _ in segments]
        completed = 0
        for group in groups.values():
            for item in group["passes"]:
                for index in range(len(segments)):
                    try:
                        candidate = json.loads((Path(item["checkpoint"]) / f"{index:05d}.json").read_text())
                        completed += 1
                        if candidate:
                            candidates[index].append(candidate)
                    except (OSError, ValueError):
                        pass
        if publish:
            publish(candidates, completed, total)
        completed_count = completed
        return candidates
    candidates = collect()
    if segments:
        translation_task = temporary / "translation-validation.json"
        write_json(translation_task, {"languages": languages})
        log("setup", "Checking real translation inference before remaining recognition work.")
        run_asr_processes(translation_task, "cpu", 1, translation=True)
    # Validate every remaining model on each actual GPU before any long pass.
    for path, group in pending:
        log("setup", f"Validating {group['model_key']} on each isolated worker with real recording audio.")
        run_asr_processes(path, device, count, validate=True)
    done, computed = cached_count, 0
    started = last_report = time.monotonic()
    def progress(event):
        nonlocal done, computed, last_report
        if event["reused"]:
            return
        done += 1
        computed += 1
        log("transcribe", f"{done}/{total} hypotheses: window {event['index'] + 1}, {event['language']}, {event['version']}.")
        now = time.monotonic()
        if now - last_report >= 30:
            rate = computed / max(0.001, now - started)
            log("transcribe", f"Throughput: {rate * 60:.1f} hypotheses/minute; approximately {(total - done) / rate / 60:.0f} minutes of recognition remaining (excludes translation).")
            last_report = now
    for path, group in pending:
        log("transcribe", f"Starting fresh processes for {group['model_key']}.")
        run_asr_processes(path, device, count, progress=progress)
        candidates = collect()
    if completed_count != total:
        raise RuntimeError(f"Only {completed_count}/{total} window hypotheses were saved. Retry will resume the missing work.")
    return candidates


def run(folder, settings, max_seconds=None, prepare_only=False):
    folder = Path(folder).resolve()
    os.environ["RESOURCE_RUN_ID"] = settings.get("_resource_run_id", "worker")
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
    from app.checkpoints import cached_json, fingerprint, source_key
    from app.inference import config
    catalog = config()
    log("resume", "Checking recording fingerprint and saved processing stages.")
    from app.timeline import pipeline_options
    pipeline = pipeline_options(catalog)
    input_key = source_key(original, max_seconds)
    checkpoint = folder / "checkpoints" / input_key
    checkpoint.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="processing-", dir=str(folder)) as work:
        temporary = Path(work)
        prepared = checkpoint / "prepared.wav"
        if valid_audio(prepared):
            duration = duration_of(prepared)
            log("resume", f"Reusing prepared audio ({duration:.2f} seconds).")
        else:
            pending = checkpoint / "prepared.partial.wav"
            duration = prepare(original, pending, max_seconds)
            pending.replace(prepared)
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
        from app.inference import choices_for
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
        enhancement_key = fingerprint(["enhancement-v2", catalog["enhancement"]])
        enhancement = checkpoint / ("enhance-" + enhancement_key)
        enhancement.mkdir(exist_ok=True)
        enhanced_wav = enhancement / "enhanced.wav"
        if valid_audio(enhanced_wav, duration):
            log("resume", "Reusing completed enhanced audio; no enhancement chunks need to run.")
            if not (enhancement / "enhanced.m4a").is_file():
                encode_enhanced(enhanced_wav)
        else:
            enhance(prepared, enhanced_wav, temporary, duration, settings)
        shutil.copyfile(enhancement / "enhanced.m4a", folder / "enhanced.m4a")
        audio_paths = {"original": prepared, "enhanced": enhanced_wav}
        selected_paths = audio_paths if asr_input == "both" else {asr_input: audio_paths[asr_input]}
        vad_key = fingerprint(["vad-v1", asr_input, enhancement_key if asr_input != "original" else None, catalog["vad"]])
        regions, reused = cached_json(checkpoint / f"vad-{vad_key}.json", lambda: speech_regions(selected_paths, duration))
        if reused:
            log("resume", f"Reusing {len(regions)} detected speech regions.")
        gpu_seconds = 0.0
        stage_start = time.monotonic()
        diarization_key = fingerprint(["speakers-v1", catalog["diarization"], settings.get("min_speakers"), settings.get("max_speakers"), device])
        turns, reused = (cached_json(checkpoint / f"speakers-{diarization_key}.json",
                                    lambda: diarize(prepared, settings, device)) if duration > 0 else ([], False))
        if reused:
            log("resume", "Reusing completed speaker diarization.")
        if device == "cuda":
            gpu_seconds += time.monotonic() - stage_start
        from app.timeline import VERSION, windows, tracks, assemble, audit, recovery_windows, speech_overlap
        from app.progress import timeline_draft, publish_snapshot, publish_translation
        plan = windows(duration, regions, pipeline.get("core_seconds",24), pipeline.get("context_seconds",3))
        if audit(duration,plan,[],[],[],[])["input_coverage_gaps"]:
            raise RuntimeError("Recognition planner left uncovered input audio.")
        plan_key = fingerprint([VERSION,pipeline,regions,duration])
        write_json(checkpoint / f"timeline-plan-{plan_key}.json", plan)
        log("segment", f"Full-timeline plan: {len(plan)} overlapping windows; all {duration:.2f} seconds eligible for every model pass. Speaker turns do not cut recognition audio.")
        preferred = {language: choices_for(language)[0] for language in languages}
        preview = None
        def preview_ready(candidates, completed, total):
            nonlocal preview
            rows,_ = assemble(tracks(plan,candidates),turns,duration,preferred,pipeline.get("language_probe_seconds",6))
            preview = timeline_draft(rows,duration,completed,total,turns)
            publish_snapshot(folder,preview)
        stage_start = time.monotonic()
        validation_index = max(range(len(plan)),key=lambda index:speech_overlap(plan[index]["start"],plan[index]["end"],regions))
        candidates = recognize_segments(plan, selected_paths, checkpoint, enhancement_key,
                                        languages, {**settings,"_validation_index":validation_index}, device, temporary, publish=preview_ready)
        primary_lanes = tracks(plan,candidates)
        segments,decisions = assemble(primary_lanes,turns,duration,preferred,pipeline.get("language_probe_seconds",6))
        initial_report = audit(duration,plan,candidates,segments,regions,turns)
        retry_plan,deferred = recovery_windows(initial_report,regions,pipeline.get("recovery_fraction",.25))
        recovery_candidates = []
        if retry_plan:
            log("coverage", f"Detected {len(initial_report['issues'])} possible coverage/quality issues. Automatically checking {len(retry_plan)} shorter, shifted windows against original and enhanced audio; expensive preparation/enhancement is reused.")
            recovery_candidates = recognize_segments(retry_plan,audio_paths,checkpoint,enhancement_key,languages,
                {**settings,"_recovery":True},device,temporary)
            segments,decisions = assemble(primary_lanes + tracks(retry_plan,recovery_candidates,"recovery"),
                turns,duration,preferred,pipeline.get("language_probe_seconds",6))
        report = audit(duration,plan,candidates,segments,regions,turns)
        report.update(pipeline=pipeline,recognition_windows=len(plan),passes_per_audio=sum(len(choices_for(language)) for language in languages),
                      initial_issues=initial_report["issues"],recovery_windows=len(retry_plan),deferred_recovery=deferred,
                      selection_decisions=decisions,selection_changes=sum(x["changed"] for x in decisions),
                      ambiguous_selections=sum(x.get("ambiguous",False) for x in decisions),
                      recovery_limit_seconds=round(min(duration,max(60,duration*pipeline.get("recovery_fraction",.25))),3))
        write_json(folder / "coverage.json",report)
        write_json(checkpoint / f"coverage-{plan_key}.json",report)
        log("coverage", f"All input audio was submitted to every primary pass. Speech evidence without words: {initial_report['speech_without_words_seconds']:.1f}s before recovery, {report['speech_without_words_seconds']:.1f}s after. {len(deferred)} intervals exceeded the recovery budget; {report['ambiguous_selections']} language cells remain ambiguous. See coverage.json.")
        if device == "cuda":
            gpu_seconds += time.monotonic() - stage_start
            torch.cuda.empty_cache()
        for segment in segments:
            if any(issue["start"] < segment["end"] and issue["end"] > segment["start"] for issue in report["issues"]):
                segment["review_status"] = "flagged"
            segment["recognition_pipeline"] = VERSION
        preview = timeline_draft(segments,duration,len(plan),len(plan),turns)
        publish_snapshot(folder,preview)
        for index, segment in enumerate(segments):
            winner = segment
            language = winner["language"]
            translation_key = fingerprint(["translation-v1", winner["text"], language, catalog["translation"]])
            english, reused = cached_json(checkpoint / "translations" / f"{translation_key}.json",
                                         lambda: translate(winner["text"], language))
            publish_translation(folder, preview, index, english)
            segment.update({"start": round(segment["start"], 3), "end": round(segment["end"], 3),
                            "language": language, "suggested_language": language,
                            "machine_text": winner["text"], "text": winner["text"],
                            "machine_english": english, "english": english, "english_machine_generated": True,
                            "confidence": winner["confidence"], "words": winner["words"],
                            "alternates": winner["alternates"], "model": winner["model"],
                            "audio_version": winner["audio_version"]})
            log("translate", f"Translated segment {index + 1}/{len(segments)} to English.")
        speaker_ids = list(dict.fromkeys(segment["speaker"] for segment in segments))
        from importlib.metadata import version as package_version
        manifest = {"pipeline":pipeline,"source_key":input_key,"models":catalog["asr"],
                    "allowed_languages":languages,"asr_input":asr_input,"max_seconds":max_seconds,
                    "runtime":{name:package_version(name) for name in ("faster-whisper","ctranslate2","torch","pyannote.audio","silero-vad")}}
        manifest["code_fingerprint"] = fingerprint({name:(ROOT/name).read_text(encoding="utf-8-sig")
            for name in ("worker/run.py","worker/asr.py","app/timeline.py","app/inference.py","app/checkpoints.py","app/translate.py","models.yaml")})
        result = {"duration": duration, "enhanced_path": "enhanced.m4a", "gpu_seconds": round(gpu_seconds, 3),
                  "speakers": [{"id": speaker, "name": "Unknown speaker" if speaker == "SPEAKER_UNKNOWN" else f"Speaker {index + 1}"}
                               for index, speaker in enumerate(speaker_ids)], "segments": segments,
                  "device": device, "partial_sample": bool(max_seconds), "pipeline_version": VERSION, "coverage": report,
                  "recognition_manifest": manifest}
        write_json(folder / "result.json", result)
        preview["stage"] = "finalizing"
        publish_snapshot(folder, preview)
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
