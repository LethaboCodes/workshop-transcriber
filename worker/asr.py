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
    from app.inference import transcribe_audio, release_models
    device = task["device"]
    label = f"{task['model_key']} shard {shard + 1}/{count}"
    if device == "cuda":
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
        for item in task["passes"]:
            language, version = item["language"], item["version"]
            folder = Path(item["checkpoint"])
            indices = ([next((index for index, segment in enumerate(task["segments"])
                              if segment["end"] - segment["start"] >= 0.5), None)] if validate
                       else range(shard, len(task["segments"]), count))
            emit(f"[transcribe] {'Validating' if validate else 'Running'} {label}, {language}, {version}.")
            with sf.SoundFile(item["audio"]) as audio:
                for index in indices:
                    if index is None:
                        continue
                    segment = task["segments"][index]
                    def recognize():
                        audio.seek(round(segment["start"] * 16000))
                        samples = audio.read(max(1, round((segment["end"] - segment["start"]) * 16000)), dtype="float32")
                        return transcribe_audio(samples, language, task["model_key"], version, segment["start"],
                                                task.get("word_list", ""), device, segment["end"] - segment["start"])
                    if validate:
                        recognize()  # Always execute inference, even if this segment was checkpointed.
                        emit(f"[setup] Real {label}/{language}/{version} model loading and inference passed.")
                        gpu_sample(label + " validation passed")
                    else:
                        _, reused = cached_json(folder / f"{index:05d}.json", recognize)
                        emit("[asr-progress] " + json.dumps({"index": index, "language": language,
                                                            "version": version, "reused": reused}))
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
