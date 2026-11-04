"""Single-device ASR subprocess: one model lifetime and CUDA context per process."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def emit(message):
    print(message, flush=True)


def gpu_sample(label):
    physical = os.environ.get("ASR_PHYSICAL_DEVICE", "0")
    try:
        result = subprocess.run(["nvidia-smi", "--id=" + physical,
                                 "--query-gpu=index,uuid,utilization.gpu,memory.used",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            emit(f"[gpu] {label}; worker PID {os.getpid()}; index, UUID, utilization %, memory MiB: {result.stdout.strip()}")
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_task(task, shard, count, validate=False, translation=False):
    if translation:
        from app.translate import translate
        samples = {"xh": "Molo.", "af": "Goeie dag."}
        for language in task["languages"]:
            if language != "en":
                translated = translate(samples.get(language, "Hello."), language)
                if not translated.strip():
                    raise RuntimeError(f"Translation validation returned empty text for {language}.")
                emit(f"[setup] Real {language}-to-English translation inference passed.")
        return
    import soundfile as sf
    from app.checkpoints import cached_json
    from app.inference import transcribe_audio, release_models, acoustic_languages, config, reuse_window_encoder
    device = task["device"]
    label = f"{task['model_key']} shard {shard + 1}/{count}"
    if device == "cuda":
        from worker.cuda_runtime import verify_libraries
        library_handles = verify_libraries(emit)
        import ctranslate2
        if ctranslate2.get_cuda_device_count() != 1:
            raise RuntimeError("Each isolated ASR process must see exactly one GPU.")
        emit(f"[gpu] {label}: CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES')}; exactly one CUDA device visible.")
    stop = threading.Event()
    def monitor():
        while not stop.wait(15):
            gpu_sample(label)
    sampler = threading.Thread(target=monitor, daemon=True) if device == "cuda" else None
    if sampler:
        sampler.start()
    try:
        # Process all languages for the same model/audio window together. The
        # forced-language decoders, fallback temperatures and word alignment are
        # unchanged; identical encoder inputs can share their computed tensor.
        versions = list(dict.fromkeys(item["version"] for item in task["passes"]))
        hits = misses = 0
        for version in versions:
            items = [item for item in task["passes"] if item["version"] == version]
            indices = ([task.get("validation_index", 0)] if validate and task["segments"]
                       else [] if validate else range(shard, len(task["segments"]), count))
            emit(f"[transcribe] {'Validating' if validate else 'Running'} {label}, {version}, languages " +
                 ", ".join(item["language"] for item in items) + ".")
            with sf.SoundFile(items[0]["audio"]) as audio:
                for index in indices:
                    if index is None:
                        continue
                    segment = task["segments"][index]
                    saved = {}
                    if not validate:
                        for item in items:
                            path = Path(item["checkpoint"]) / f"{index:05d}.json"
                            try:
                                saved[item["language"]] = json.loads(path.read_text())
                            except (OSError, ValueError):
                                pass
                    missing = [item for item in items if validate or item["language"] not in saved]
                    if missing:
                        audio.seek(round(segment["start"] * 16000))
                        samples = audio.read(max(1, round((segment["end"] - segment["start"]) * 16000)), dtype="float32")
                        if not len(samples):
                            raise ValueError(f"Recognition window {index + 1} has no decodable samples.")
                        with reuse_window_encoder(task["model_key"], device) as cache:
                            for item in missing:
                                language = item["language"]
                                def recognize():
                                    candidate = transcribe_audio(samples, language, task["model_key"], version, segment["start"],
                                                        task.get("word_list", ""), device, segment["end"] - segment["start"],
                                                        coverage_mode=task.get("coverage_mode", False), recovery=task.get("recovery", False))
                                    if candidate is not None and task.get("coverage_mode") and config()["asr"][task["model_key"]].get("language_evidence_codes"):
                                        def probe():
                                            return acoustic_languages(samples, task["model_key"], segment["start"], device,
                                                                      task.get("language_probe_seconds", 6))
                                        # Validation runs the same window on both GPUs.
                                        # It must not race over a shared checkpoint file.
                                        evidence = probe() if validate else cached_json(
                                            Path(item["evidence_checkpoint"]) / f"{index:05d}.json", probe)[0]
                                        candidate["language_evidence"] = evidence
                                    return candidate
                                if validate:
                                    recognize()
                                    emit(f"[setup] Real {label}/{language}/{version} model loading and inference passed.")
                                    gpu_sample(label + " validation passed")
                                else:
                                    cached_json(Path(item["checkpoint"]) / f"{index:05d}.json", recognize)
                                    emit("[asr-progress] " + json.dumps({"index": index, "language": language,
                                                                        "version": version, "reused": False}))
                            if cache:
                                hits += cache.hits
                                misses += cache.misses
                    if not validate:
                        for language in saved:
                            emit("[asr-progress] " + json.dumps({"index": index, "language": language,
                                                                "version": version, "reused": True}))
        emit(f"[performance] {label}: reused {hits} identical encoder calls; computed {misses}. Every language pass was retained.")
    finally:
        stop.set()
        if sampler:
            sampler.join(timeout=6)
        release_models()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--translation", action="store_true")
    arguments = parser.parse_args()
    try:
        run_task(json.loads(arguments.task.read_text()), arguments.shard, arguments.count,
                 arguments.validate, arguments.translation)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
