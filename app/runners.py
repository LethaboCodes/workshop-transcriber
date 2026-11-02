"""Durable local / private Kaggle jobs and a separate CPU editing queue."""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from sqlalchemy import select

from .db import (DATA_DIR, ROOT, Job, Recording, Segment, SessionLocal, app_config,
                 append_log, apply_retranscription, apply_translation, import_result,
                 recording_folder, serialize_segment, utcnow)

_stop = threading.Event()
_threads = []
_active = {}


def wait_for_recording(recording_id, timeout=15):
    deadline = time.monotonic() + timeout
    while recording_id in list(_active.values()):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def poll_wait(job_id, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_cancelled(job_id)
        _stop.wait(min(1, max(0, deadline - time.monotonic())))


class Cancelled(Exception):
    pass


def safe_message(value):
    text = str(value)
    for key in ("HF_TOKEN", "KAGGLE_KEY", "APP_PASSWORD"):
        secret = os.getenv(key)
        if secret:
            text = text.replace(secret, "[secret]")
    return text


def log(job_id, message):
    message = safe_message(message).strip()
    message = "\n".join(line for line in message.splitlines() if not line.startswith("[resource] "))
    if not message:
        return
    for line in message.splitlines():
        print(f"{utcnow()} [job {job_id}] {line}", flush=True)
    append_log(job_id, message)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job:
            job.step = message[:240]
            session.commit()


def check_cancelled(job_id):
    if _stop.is_set():
        raise Cancelled("Application stopped; retry the job after restart.")
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job or job.status == "cancelled":
            raise Cancelled("Cancelled locally.")


def job_settings(recording):
    settings = dict(recording.settings or {})
    settings["original_path"] = str((DATA_DIR / recording.original_path).resolve())
    return settings


class JobRunner(ABC):
    @abstractmethod
    def run(self, job, recording):
        """Return the worker result after installing its audio in the recording folder."""


class LocalRunner(JobRunner):
    def run(self, job, recording):
        folder = recording_folder(recording.id)
        folder.mkdir(parents=True, exist_ok=True)
        settings_path = folder / "settings.json"
        settings_path.write_text(json.dumps({**job_settings(recording), "_resource_run_id": job.id}), encoding="utf-8")
        interpreter = os.getenv("WORKER_PYTHON", sys.executable)
        log(job.id, "Starting local worker. First use downloads model weights.")
        command = [interpreter, "-u", str(ROOT / "worker" / "run.py"), str(folder), "--settings", str(settings_path)]
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                   start_new_session=os.name != "nt")
        lines = queue.Queue()
        def read_output():
            for line in process.stdout:
                lines.put(line)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            while process.poll() is None or not lines.empty():
                check_cancelled(job.id)
                try:
                    log(job.id, lines.get(timeout=0.5))
                except queue.Empty:
                    continue
            reader.join(timeout=1)
            while not lines.empty():
                log(job.id, lines.get_nowait())
            if process.returncode:
                raise RuntimeError(f"Worker exited with code {process.returncode}. See the last log lines. Local mode needs worker/requirements.txt installed in WORKER_PYTHON; the Docker web image uses Kaggle mode.")
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        result_path = folder / "result.json"
        if not result_path.is_file():
            raise RuntimeError("Worker completed without result.json.")
        return json.loads(result_path.read_text(encoding="utf-8"))


def status_name(response):
    if isinstance(response, str):
        return response.lower().split(".")[-1]
    status = response.get("status", "") if isinstance(response, dict) else getattr(response, "status", "")
    return str(getattr(status, "name", status)).lower().split(".")[-1]


def http_status(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        response = getattr(error, "response", None)
        if response is not None:
            return getattr(response, "status_code", None)
        error = error.__cause__ or error.__context__
    return None


def kernel_reference(response, requested):
    reference = str(getattr(response, "ref", "") or "").strip("/")
    if not reference:
        path = urlsplit(str(getattr(response, "url", "") or "")).path
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "code":
            reference = "/".join(parts[1:3])
    if reference and re.fullmatch(r"[\w-]+/[\w-]+", reference):
        return reference
    return requested


def kernel_log_text(value):
    try:
        events = json.loads(value)
        if isinstance(events, list):
            return "".join(str(event.get("data", "")) for event in events if isinstance(event, dict))
    except (ValueError, TypeError):
        pass
    return str(value or "")


def wait_for_dataset(api, job_id, dataset_ref):
    # A successful create can precede access through Kaggle's status endpoint.
    # Retry only this known-created resource, and bound permission retries.
    permission_failures = 0
    for attempt in range(60):
        check_cancelled(job_id)
        try:
            state = status_name(api.dataset_status(dataset_ref))
            permission_failures = 0
        except Exception as exc:
            code = http_status(exc)
            if code not in {403, 404}:
                raise
            permission_failures += 1
            if permission_failures >= 12:
                raise RuntimeError(f"Kaggle created the private dataset but still returns HTTP {code} after several minutes. Open https://www.kaggle.com/datasets/{dataset_ref} with the account used by KAGGLE_USERNAME and check access.") from exc
            log(job_id, f"Private dataset created; Kaggle access is still propagating (HTTP {code}, retry {permission_failures}/12 in 30 seconds).")
            poll_wait(job_id)
            continue
        if state in {"ready", "complete", "completed"}:
            log(job_id, "Private Kaggle dataset is ready.")
            return
        if state in {"error", "failed"}:
            raise RuntimeError(f"Kaggle dataset preparation {state}.")
        log(job_id, f"Kaggle dataset: {state or 'preparing'}")
        poll_wait(job_id)
    raise RuntimeError("Kaggle dataset did not become ready within 30 minutes.")


def delete_dataset(api, job_id, dataset_ref):
    owner, slug = dataset_ref.split("/", 1)
    for attempt in range(6):
        try:
            api.dataset_delete(owner, slug, no_confirm=True)
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job:
                    job.payload = {**(job.payload or {}), "dataset_created": False, "cleanup_pending": False}
                    session.commit()
            log(job_id, "Deleted private input dataset and its token file.")
            return True
        except Exception as exc:
            code = http_status(exc)
            if code in {403, 404, 429, 500, 502, 503, 504} and attempt < 5:
                log(job_id, f"Kaggle cleanup returned HTTP {code}; retrying in 10 seconds ({attempt + 1}/6).")
                time.sleep(10)
                continue
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job:
                    job.payload = {**(job.payload or {}), "cleanup_pending": True}
                    session.commit()
            log(job_id, f"Could not delete input dataset {dataset_ref}: {exc}. Delete it at https://www.kaggle.com/datasets/{dataset_ref}.")
            return False


def download_kaggle_artifacts(api, kernel_ref, output, folder, job_id):
    from .checkpoints import unpack_checkpoints
    from .progress import preview_from_result
    def fetch(pattern):
        token = None
        while True:
            _, token = api.kernels_output(kernel_ref, str(output), file_pattern=pattern,
                                          force=True, quiet=True, page_token=token)
            if not token:
                break
    # Make small, viewable results available before large audio/checkpoint files.
    fetch(r"(result\.json|coverage\.json|preview\.json|worker\.log|runtime-cache\.json)$")
    coverage_file = next(iter(output.rglob("coverage.json")), None)
    if coverage_file:
        shutil.copyfile(coverage_file, folder / "coverage.json")
    result_file = next(iter(output.rglob("result.json")), None)
    preview_file = next(iter(output.rglob("preview.json")), None)
    if result_file:
        preview_from_result(folder, json.loads(result_file.read_text(encoding="utf-8")))
        log(job_id, "Complete transcript and translations are available to preview/export while remaining files download.")
    elif preview_file:
        from .checkpoints import save_json
        save_json(folder / "preview.json", json.loads(preview_file.read_text(encoding="utf-8")))
    fetch(r"enhanced\.m4a$")
    enhanced = next(iter(output.rglob("enhanced.m4a")), None)
    if enhanced:
        temporary = folder / "enhanced.publishing"
        shutil.copyfile(enhanced, temporary)
        temporary.replace(folder / "enhanced.m4a")
    fetch(r"checkpoints\.bundle$")
    checkpoint = next(iter(output.rglob("checkpoints.bundle")), None)
    if checkpoint:
        unpack_checkpoints(checkpoint, folder)
        log(job_id, f"Saved processing checkpoints locally ({checkpoint.stat().st_size / 1024**2:.1f} MB download). Retry can reuse completed work.")
        # A failed transcription must not hide an already completed enhancement.
        from .audio import publish_checkpoint_enhancement
        from .progress import recover_checkpoint_preview
        try:
            with SessionLocal() as session:
                live = session.get(Job, job_id)
                recording = session.get(Recording, live.recording_id) if live else None
                if recording and publish_checkpoint_enhancement(session, recording):
                    session.commit()
                    log(job_id, "Enhanced listening copy is available; transcript processing can continue separately.")
                if recording:
                    recover_checkpoint_preview(recording)
        except Exception as exc:
            log(job_id, f"Could not publish the enhanced preview yet: {exc}")
    manifest = next(iter(output.rglob("runtime-cache.json")), None)
    if manifest and json.loads(manifest.read_text()).get("ready"):
        with SessionLocal() as session:
            live = session.get(Job, job_id)
            if live:
                live.payload = {**(live.payload or {}), "runtime_cache_available": True}
                session.commit()
        log(job_id, "Reusable dependencies/models are available in this private notebook's outputs; future jobs can attach them within Kaggle.")
    for remote_log in output.rglob("worker.log"):
        log(job_id, remote_log.read_text(encoding="utf-8", errors="replace"))


class KaggleRunner(JobRunner):
    def run(self, job, recording):
        for name in ("KAGGLE_USERNAME", "KAGGLE_KEY", "HF_TOKEN"):
            if not os.getenv(name):
                raise RuntimeError(f"Set {name} in .env and restart the app, then retry this job. HF_TOKEN also needs the diarization model terms accepted.")
        from kaggle import KaggleApi
        api = KaggleApi()
        api.authenticate()
        if not (job.kaggle_ref and (job.payload or {}).get("submitted")):
            from .inference import check_model_access
            log(job.id, "Checking model access before uploading or starting GPU compute.")
            check_model_access(recording.settings or {})
        folder = recording_folder(recording.id)
        staging = folder / ("kaggle-" + job.id)
        dataset = staging / "dataset"
        kernel = staging / "kernel"
        output = staging / "output"
        for path in (dataset, kernel, output):
            path.mkdir(parents=True, exist_ok=True)
        owner = os.environ["KAGGLE_USERNAME"]
        dataset_ref = str((job.payload or {}).get("dataset_ref", f"{owner}/workshop-audio-{job.id[:24]}"))
        kernel_ref = job.kaggle_ref or f"{owner}/workshop-run-{job.id[:24]}"
        submitted = bool(job.kaggle_ref and (job.payload or {}).get("submitted"))
        created = bool((job.payload or {}).get("dataset_created"))
        remote_started = time.monotonic()
        remote_gpu = float(job.gpu_seconds or 0)
        remote_finished = False
        live_stream = None
        try:
            if not submitted:
                checkpoint_ref = (job.payload or {}).get("checkpoint_ref")
                if checkpoint_ref:
                    log(job.id, "Recovering checkpoints from the previous failed notebook before starting a retry.")
                    download_kaggle_artifacts(api, checkpoint_ref, output, folder, job.id)
                    shutil.rmtree(output)
                    output.mkdir()
                    with SessionLocal() as session:
                        live = session.get(Job, job.id)
                        if live:
                            live.payload = {key: value for key, value in live.payload.items() if key != "checkpoint_ref"}
                            session.commit()
                log(job.id, "Creating private Kaggle input dataset.")
                source = DATA_DIR / recording.original_path
                target = dataset / ("original" + source.suffix.lower())
                shutil.copyfile(source, target)
                settings = job_settings(recording)
                settings["original_path"] = target.name
                settings["device"] = "cuda"
                settings["_resource_run_id"] = job.id
                (dataset / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
                (dataset / "env.json").write_text(json.dumps({"HF_TOKEN": os.environ["HF_TOKEN"]}), encoding="utf-8")
                (dataset / "dataset-metadata.json").write_text(json.dumps({"title": f"Workshop audio {job.id[:24]}", "id": dataset_ref, "licenses": [{"name": "other"}]}), encoding="utf-8")
                from .checkpoints import pack_checkpoints
                if pack_checkpoints(folder, dataset / "checkpoints.bundle"):
                    log(job.id, "Including saved processing checkpoints in the private input dataset.")
                # Kaggle can expand files with a .zip extension on ingestion.
                with zipfile.ZipFile(dataset / "source.bundle", "w", zipfile.ZIP_DEFLATED) as archive:
                    for name in ("models.yaml", "worker/run.py", "worker/asr.py", "worker/runtime_cache.py", "worker/requirements.txt", "app/__init__.py", "app/inference.py", "app/translate.py", "app/checkpoints.py", "app/progress.py", "app/timeline.py"):
                        archive.write(ROOT / name, name)
                response = api.dataset_create_new(str(dataset), public=False, quiet=True, convert_to_csv=False)
                error = getattr(response, "error", None)
                if error:
                    raise RuntimeError(f"Private dataset creation failed: {error}")
                created = True
                with SessionLocal() as session:
                    live = session.get(Job, job.id)
                    if live:
                        live.payload = {**(live.payload or {}), "dataset_ref": dataset_ref, "dataset_created": True}
                        live.kaggle_ref = kernel_ref
                        session.commit()
                wait_for_dataset(api, job.id, dataset_ref)
                cache_ref = ""
                with SessionLocal() as session:
                    for previous in session.scalars(select(Job).where(Job.mode == "kaggle", Job.status.in_(["complete", "failed"])).order_by(Job.created_at.desc()).limit(50)):
                        if (previous.payload or {}).get("runtime_cache_available") and previous.kaggle_ref.startswith(owner + "/"):
                            cache_ref = previous.kaggle_ref
                            break
                if cache_ref:
                    log(job.id, "Attaching reusable private runtime/model cache: https://www.kaggle.com/code/" + cache_ref)
                metadata = {"id": kernel_ref, "title": f"Workshop run {job.id[:24]}", "code_file": "run_job.py", "language": "python", "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_internet": True, "dataset_sources": [dataset_ref], "competition_sources": [], "kernel_sources": [cache_ref] if cache_ref else []}
                (kernel / "kernel-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                (kernel / "run_job.py").write_text(KERNEL_SCRIPT, encoding="utf-8")
                log(job.id, "Starting private GPU notebook: https://www.kaggle.com/code/" + kernel_ref)
                response = api.kernels_push(str(kernel))
                error = getattr(response, "error", None)
                if error:
                    raise RuntimeError(f"Kaggle rejected the notebook: {error}")
                submitted = True
                kernel_ref = kernel_reference(response, kernel_ref)
                with SessionLocal() as session:
                    live = session.get(Job, job.id)
                    if live:
                        live.kaggle_ref = kernel_ref
                        live.payload = {**(live.payload or {}), "submitted": True, "resume_remote": True,
                                        "runtime_cache_available": False, "runtime_cache_ref": cache_ref}
                        session.commit()
                log(job.id, "Submitted private notebook: https://www.kaggle.com/code/" + kernel_ref)
            else:
                log(job.id, "Resuming monitoring of existing private Kaggle notebook.")
            from .live import LiveKaggleLog
            live_stream = LiveKaggleLog(api, kernel_ref, job.id, recording.id,
                                      lambda message: log(job.id, message), _stop.is_set)
            last_log, previous_state = "", ""
            status_failures = 0
            while True:
                check_cancelled(job.id)
                try:
                    response = api.kernels_status(kernel_ref)
                    status_failures = 0
                except Exception as exc:
                    code = http_status(exc)
                    status_failures += 1
                    if code not in {403, 404, 429, 500, 502, 503, 504} or status_failures >= 12:
                        raise
                    log(job.id, f"Notebook status temporarily unavailable (HTTP {code}); retry {status_failures}/12 in 30 seconds.")
                    poll_wait(job.id)
                    continue
                state = status_name(response)
                if state != previous_state:
                    log(job.id, f"Kaggle notebook: {state}")
                    previous_state = state
                now = time.monotonic()
                if "running" in state:
                    remote_gpu += max(0, now - remote_started)
                    with SessionLocal() as session:
                        live = session.get(Job, job.id)
                        if live:
                            live.gpu_seconds = remote_gpu
                            session.commit()
                remote_started = now
                if hasattr(api, "kernels_logs"):
                    try:
                        current_log = kernel_log_text(api.kernels_logs(kernel_ref))
                        if current_log and current_log != last_log:
                            log(job.id, current_log[len(last_log):] if current_log.startswith(last_log) else current_log)
                            last_log = current_log
                    except Exception:
                        pass  # Some Kaggle versions expose logs only after completion.
                if state in ("complete", "completed"):
                    remote_finished = True
                    break
                if state in ("error", "failed", "cancelled", "canceled", "cancel_acknowledged"):
                    remote_finished = True
                    message = getattr(response, "failure_message", "")
                    if not message:
                        failures = re.findall(r"^\[failed\]\s*(.+)$", last_log, re.MULTILINE)
                        if not failures:
                            failures = re.findall(r"^(?:ImportError|ModuleNotFoundError|RuntimeError|OSError|ValueError):\s*(.+)$", last_log, re.MULTILINE)
                        message = failures[-1] if failures else "See the processing log for the worker error"
                    log(job.id, "Notebook stopped; downloading any completed processing checkpoints.")
                    try:
                        download_kaggle_artifacts(api, kernel_ref, output, folder, job.id)
                    except Exception as exc:
                        log(job.id, f"Checkpoint download failed: {exc}. Retry will try to recover it before starting new compute.")
                        with SessionLocal() as session:
                            live = session.get(Job, job.id)
                            if live:
                                live.payload = {**(live.payload or {}), "checkpoint_ref": kernel_ref}
                                session.commit()
                    raise RuntimeError(f"Kaggle notebook {state}: {message}. Open its link for the full notebook log.")
                poll_wait(job.id)
            log(job.id, "Downloading transcript, enhanced audio and processing checkpoints.")
            try:
                download_kaggle_artifacts(api, kernel_ref, output, folder, job.id)
            except Exception:
                with SessionLocal() as session:
                    live = session.get(Job, job.id)
                    if live:
                        live.payload = {**(live.payload or {}), "checkpoint_ref": kernel_ref}
                        session.commit()
                raise
            results = list(output.rglob("result.json"))
            if not results:
                raise RuntimeError("Kaggle completed without result.json; inspect the notebook log.")
            result = json.loads(results[0].read_text(encoding="utf-8"))
            enhanced = next(iter(output.rglob("enhanced.m4a")), None)
            if enhanced is None:
                raise RuntimeError("Kaggle completed without enhanced audio.")
            shutil.copyfile(enhanced, folder / "enhanced.m4a")
            result["enhanced_path"] = "enhanced.m4a"
            result["gpu_seconds"] = max(float(result.get("gpu_seconds", 0)), remote_gpu)
            (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return result
        except Exception as exc:
            response = getattr(exc, "response", None)
            if response is not None:
                detail = safe_message(response.text)[:2000]
                log(job.id, f"Kaggle HTTP {response.status_code} response: {detail}")
                raise RuntimeError(f"Kaggle HTTP {response.status_code}: {detail}") from exc
            raise
        finally:
            if live_stream:
                live_stream.close()
            # Keep mounted inputs if monitoring stops before the remote run ends.
            with SessionLocal() as session:
                live = session.get(Job, job.id)
                if live:
                    live.payload = {**(live.payload or {}), "resume_remote": submitted and not remote_finished,
                                    "remote_finished": remote_finished}
                    session.commit()
            if created and (not submitted or remote_finished):
                delete_dataset(api, job.id, dataset_ref)
            elif created:
                log(job.id, "Private input dataset retained while the notebook may still be running. Retry resumes monitoring of the existing run.")
            if staging.exists() and staging.resolve().is_relative_to(folder.resolve()):
                shutil.rmtree(staging)


KERNEL_SCRIPT = r'''import ctypes.util, json, os, shutil, subprocess, sys, tempfile, traceback, zipfile
from pathlib import Path

output = Path('/kaggle/working')
output.mkdir(parents=True, exist_ok=True)
log_handle = (output/'worker.log').open('w', encoding='utf-8')
recording = None
project = None
runtime = None

def emit(message):
    message = str(message)
    for name in ('HF_TOKEN', 'KAGGLE_KEY', 'APP_PASSWORD'):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, '[secret]')
    print(message, end='' if message.endswith('\n') else '\n', flush=True)
    log_handle.write(message if message.endswith('\n') else message+'\n')
    log_handle.flush()

def run(arguments, env=None):
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', env=env)
    for line in process.stdout:
        emit(line)
    code = process.wait()
    if code:
        raise subprocess.CalledProcessError(code, arguments)

def main():
    global recording, project, runtime
    source = next(Path('/kaggle/input').rglob('source.bundle'), None)
    if source is None:
        raise RuntimeError('Worker bundle is missing from mounted Kaggle inputs. Check the attached private dataset.')
    inputs = source.parent
    project = Path(tempfile.mkdtemp(prefix='workshop-'))
    with zipfile.ZipFile(source) as archive:
        archive.extractall(project)
    sys.path.insert(0, str(project))
    os.environ.update(json.loads((inputs/'env.json').read_text()))
    worker_env = dict(os.environ)
    # A venv cannot isolate packages if Kaggle injects its global Python path.
    worker_env.pop('PYTHONPATH', None)
    worker_env.pop('PYTHONHOME', None)
    # Kaggle also puts sitecustomize in the standard library, outside the venv.
    # Override only that cloud-integration hook in our private subprocesses.
    bootstrap = project/'bootstrap'
    bootstrap.mkdir()
    (bootstrap/'sitecustomize.py').write_text('"""Isolated audio worker: no Kaggle cloud integration hooks."""\n')
    worker_env['PYTHONPATH'] = str(bootstrap)
    worker_env['PYTHONNOUSERSITE'] = '1'
    worker_env['PYTHONUNBUFFERED'] = '1'
    worker_env['HF_HOME'] = str(project/'hf-home')
    worker_env['HF_XET_CACHE'] = str(project/'xet-cache')
    worker_env['HF_XET_HIGH_PERFORMANCE'] = '1'
    worker_env['OMP_NUM_THREADS'] = '1'
    worker_env['UV_CONCURRENT_DOWNLOADS'] = '8'
    worker_env['UV_CONCURRENT_INSTALLS'] = '2'
    worker_env['UV_CONCURRENT_BUILDS'] = '1'
    worker_env['UV_CACHE_DIR'] = str(project/'uv-cache')
    worker_env['UV_LINK_MODE'] = 'copy'

    missing = []
    if not shutil.which('ffmpeg'):
        missing.append('ffmpeg')
    if not ctypes.util.find_library('gomp'):
        missing.append('libgomp1')
    if missing:
        emit('[setup] Installing missing system tools: '+', '.join(missing))
        run(['apt-get', 'update', '-qq'])
        run(['apt-get', 'install', '-y', '-qq', *missing])
    else:
        emit('[setup] ffmpeg and OpenMP are available; no apt update is needed.')

    emit('[setup] Creating an isolated Python environment for this worker.')
    environment = project/'venv'
    # Kaggle's Debian Python can omit ensurepip; pip's --python option manages
    # a pip-less venv without installing anything into the shared interpreter.
    run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], env=worker_env)
    python = str(environment/'bin/python')
    pip = [sys.executable, '-m', 'pip', '--python', python]
    from worker.runtime_cache import prepare as prepare_runtime, mark_ready
    runtime = prepare_runtime(project, environment, output, Path('/kaggle/input'),
                              project/'worker/requirements.txt', emit)
    worker_env['MODEL_CACHE'] = str(runtime['models'])
    worker_env['HF_HUB_CACHE'] = str(runtime['models'])
    if not runtime['reused']:
        emit('[setup] Installing uv for parallel package downloads in the isolated worker.')
        run([*pip, 'install', '--disable-pip-version-check', 'uv>=0.9,<1'], env=worker_env)
        emit('[setup] Installing worker dependencies with up to 8 concurrent downloads.')
        run([python, '-m', 'uv', 'pip', 'install', '--python', python, '-r',
             str(project/'worker/requirements.txt')], env=worker_env)
        # The uv download cache duplicates installed wheels; retain only the
        # installed environment and model files in notebook outputs.
        shutil.rmtree(project/'uv-cache', ignore_errors=True)
    emit('[setup] Checking dependency compatibility.')
    run([*pip, 'check'], env=worker_env)
    emit('[setup] Checking model-library imports and GPU access before processing audio.')
    preflight = (
        'import torch, torchaudio, torchcodec, ctranslate2, numpy; '
        'from pyannote.audio import Pipeline; '
        'from silero_vad import load_silero_vad; '
        'assert torch.cuda.is_available(), "PyTorch cannot access the Kaggle GPU"; '
        'assert ctranslate2.get_cuda_device_count() > 0, "CTranslate2 cannot access the Kaggle GPU"; '
        'print("[setup] Ready: torch="+torch.__version__+", torchaudio="+torchaudio.__version__+'
        '", numpy="+numpy.__version__+", GPUs="+str(torch.cuda.device_count())+" x "+torch.cuda.get_device_name(0))'
    )
    run([python, '-u', '-c', preflight], env=worker_env)
    mark_ready(runtime)

    recording = project/'recording'
    recording.mkdir()
    sys.path.insert(0, str(project))
    from app.checkpoints import unpack_checkpoints
    if (inputs/'checkpoints.bundle').is_file():
        emit('[resume] Restoring checkpoints from the previous run.')
        unpack_checkpoints(inputs/'checkpoints.bundle', recording)
    settings = json.loads((inputs/'settings.json').read_text())
    settings['original_path'] = str(inputs/settings['original_path'])
    (recording/'settings.json').write_text(json.dumps(settings))
    run([python, '-u', str(project/'worker/run.py'), str(recording)], env=worker_env)
    for name in ('result.json', 'enhanced.m4a', 'preview.json', 'coverage.json'):
        shutil.copyfile(recording/name, output/name)

try:
    main()
except Exception:
    emit(traceback.format_exc())
    raise
finally:
    if recording is not None:
        try:
            for name in ('preview.json','coverage.json'):
                if (recording/name).is_file():
                    shutil.copyfile(recording/name, output/name)
            from app.checkpoints import pack_checkpoints
            emit('[checkpoint] Saving completed work for future retries, including after a worker failure.')
            if pack_checkpoints(recording, output/'checkpoints.bundle'):
                emit('[checkpoint] Processing checkpoints saved.')
        except Exception:
            emit('[checkpoint] Could not save checkpoints:\n'+traceback.format_exc())
    if runtime is not None:
        try:
            from worker.runtime_cache import finish
            finish(runtime, emit)
        except Exception:
            emit('[setup] Runtime cache could not be finalized:\n'+traceback.format_exc())
            # An incomplete multi-GB cache must not crowd out the transcript
            # and recording checkpoints in Kaggle's persisted outputs.
            from worker.runtime_cache import remove_directory
            remove_directory(runtime['path'], output)
    log_handle.close()
'''


def run_cpu(job, recording):
    payload = job.payload or {}
    ids = payload.get("segment_ids", [payload.get("segment_id")])
    for segment_id in filter(None, ids):
        check_cancelled(job.id)
        with SessionLocal() as session:
            segment = session.get(Segment, segment_id)
            if not segment or segment.is_deleted:
                continue
            revision = segment.revision
            snapshot = serialize_segment(segment)
        if job.kind == "translate":
            from .translate import translate
            log(job.id, f"Translating segment at {snapshot['start']:.1f}s on CPU.")
            result = translate(snapshot["edited_text"], snapshot["language"])
        else:
            from .inference import retranscribe
            log(job.id, f"Retranscribing segment at {snapshot['start']:.1f}s on CPU.")
            settings = job_settings(recording)
            result = retranscribe(recording_folder(recording.id), settings, snapshot, payload.get("language", snapshot["language"]), payload.get("model_key"))
        check_cancelled(job.id)
        with SessionLocal() as session:
            segment = session.get(Segment, segment_id)
            if not segment or segment.is_deleted:
                continue
            if segment.revision != revision:
                log(job.id, f"Skipped segment {segment_id}: it was edited while this job ran. Run again to use the latest text.")
                continue
            if job.kind == "translate":
                apply_translation(session, segment, result)
            else:
                apply_retranscription(session, segment, result)
            session.commit()


def dispatch(cpu=False):
    while not _stop.is_set():
        job_id = None
        try:
            with SessionLocal() as session:
                query = select(Job).where(Job.status == "queued")
                query = query.where(Job.kind != "pipeline" if cpu else Job.kind == "pipeline")
                job = session.scalar(query.order_by(Job.created_at))
                if not job:
                    _stop.wait(0.8)
                    continue
                job_id = job.id
                _active[job_id] = job.recording_id
                recording = session.get(Recording, job.recording_id)
                if not recording:
                    continue
                if not cpu and job.mode == "kaggle":
                    config = app_config(session)
                    if config["gpu_hours"] >= float(config["weekly_gpu_limit"]):
                        raise RuntimeError("The configured weekly GPU budget is used. Increase the limit in Settings or retry after usage ages out.")
                job.status, job.step = "processing", "Starting"
                job.started_at = job.started_at or utcnow()
                if not cpu:
                    recording.status, recording.error = "processing", ""
                session.commit()
            log(job_id, f"Starting {job.kind} job with {job.mode} worker.")
            if cpu:
                run_cpu(job, recording)
            else:
                runner = KaggleRunner() if job.mode == "kaggle" else LocalRunner()
                result = runner.run(job, recording)
                check_cancelled(job_id)
                import_result(recording.id, result, job_id=job_id)
            with SessionLocal() as session:
                live = session.get(Job, job_id)
                if live and live.status != "cancelled":
                    live.status, live.step, live.ended_at = "complete", "Complete", utcnow()
                    session.commit()
            log(job_id, "Complete.")
        except Exception as exc:
            if job_id:
                message = safe_message(exc)
                log(job_id, message if isinstance(exc, Cancelled) else traceback.format_exc())
                with SessionLocal() as session:
                    live = session.get(Job, job_id)
                    if live and live.status != "cancelled":
                        if _stop.is_set() and live.mode == "kaggle" and (live.payload or {}).get("submitted"):
                            live.status, live.step = "queued", "Monitoring paused until application restarts"
                        else:
                            live.status, live.error, live.step, live.ended_at = "failed", message, "Failed", utcnow()
                            rec = session.get(Recording, live.recording_id)
                            if rec and live.kind == "pipeline":
                                rec.status, rec.error = "failed", message
                        session.commit()
            else:
                print(safe_message(traceback.format_exc()), flush=True)
                _stop.wait(2)
        finally:
            if job_id:
                _active.pop(job_id, None)


def start_dispatcher():
    if any(thread.is_alive() for thread in _threads):
        return
    _stop.clear()
    with SessionLocal() as session:
        for job in session.scalars(select(Job).where(Job.status == "processing")):
            if job.mode == "kaggle" and (job.payload or {}).get("submitted"):
                job.status = "queued"
            else:
                job.status, job.error, job.ended_at = "failed", "Interrupted by an application restart. Retry to continue.", utcnow()
                rec = session.get(Recording, job.recording_id)
                if rec and job.kind == "pipeline":
                    rec.status, rec.error = "failed", job.error
        session.commit()
    for cpu in (False, True):
        thread = threading.Thread(target=dispatch, args=(cpu,), daemon=True, name="cpu-editor" if cpu else "pipeline")
        _threads.append(thread)
        thread.start()


def stop_dispatcher():
    _stop.set()
    for thread in _threads:
        thread.join(timeout=3)
