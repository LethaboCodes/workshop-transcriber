import array
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import quote, urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text as sql_text, tuple_
from sqlalchemy.orm import Session, defer
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .db import DATA_DIR, ROOT, AppSetting, Job, Recording, Segment, SessionLocal, Speaker, Upload, app_config, draft_snapshot, enqueue_job, init_db, recording_folder, serialize_job, serialize_recording, serialize_segment
from .exports import export_transcript
from .edits import edit_response, editor_delta, undo as undo_edit

CHUNK_SIZE = 8 * 1024 * 1024
LANGUAGES = {"en", "xh", "af"}
upload_locks = {}
waveform_locks = {}
login_attempts = {}


def session_secret():
    if os.getenv("SESSION_SECRET"):
        return os.environ["SESSION_SECRET"]
    path = DATA_DIR / ".session-secret"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(48)
        try:
            with path.open("x") as handle:
                handle.write(value)
        except FileExistsError:
            return path.read_text().strip()
        return value


@asynccontextmanager
async def lifespan(app):
    if not os.getenv("APP_PASSWORD"):
        raise RuntimeError("Set APP_PASSWORD before starting the app.")
    init_db()
    from .bootstrap import import_inbox
    from .runners import start_dispatcher, stop_dispatcher
    import_inbox()
    from .audio import recover_available_enhancements
    await run_in_threadpool(recover_available_enhancements)
    from .progress import recover_checkpoint_preview
    with SessionLocal() as session:
        for recording in session.scalars(select(Recording).where(Recording.status != "ready")):
            try:
                await run_in_threadpool(recover_checkpoint_preview, recording)
            except (OSError, ValueError, KeyError):
                pass  # Optional previews never block job recovery or playback.
    start_dispatcher()
    yield
    stop_dispatcher()


app = FastAPI(title="Workshop Transcriber", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def password_signature():
    return hashlib.sha256(os.getenv("APP_PASSWORD", "").encode()).hexdigest()


@app.middleware("http")
async def authentication(request, call_next):
    if request.url.path.startswith("/api/"):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Request origin does not match this app."}, status_code=403)
        public = {"/api/login", "/api/session", "/api/logout"}
        if request.url.path not in public and not hmac.compare_digest(str(request.session.get("auth", "")), password_signature()):
            return JSONResponse({"detail": "Please sign in."}, status_code=401)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(SessionMiddleware, secret_key=session_secret(), session_cookie="workshop_session", same_site="strict", https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true", max_age=60 * 60 * 24 * 30)


def db(request: Request):
    with SessionLocal() as session:
        session.info['editor_started']=time.time()
        session.info['editor_delta']=request.query_params.get('editor_delta')=='1'
        if request.method in {'POST','PATCH','DELETE'}:
            path=request.url.path
            if path.startswith(('/api/segments/','/api/speakers/')) or (path.startswith('/api/recordings/') and (request.method=='PATCH' or path.endswith(('/segments','/speakers','/replace','/translate-outdated')))):
                from . import edits
                action=path.rsplit('/',1)[-1]
                session.info['edit_label']= {'merge':'Merge segments' if '/segments/' in path else 'Merge speakers','split':'Split segment','replace':'Find and replace','translate':'Translate text','translate-outdated':'Translate outdated text','retranscribe':'Re-transcribe text','choose-alternate':'Choose alternate text','segments':'Add segment','speakers':'Add speaker'}.get(action,
                    'Delete segment' if request.method=='DELETE' else 'Edit speaker' if '/speakers/' in path else 'Edit recording' if '/recordings/' in path else 'Edit segment')
            # Serialize short write transactions, including history and undo.
            if session.info.get('edit_label') or path.endswith('/undo'):
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        yield session


def get_recording(session, recording_id):
    row = session.get(Recording, recording_id)
    if not row:
        raise HTTPException(404, "Recording not found.")
    return row


def get_segment(session, segment_id):
    row = session.get(Segment, segment_id)
    if not row or row.is_deleted:
        raise HTTPException(404, "Segment not found.")
    return row


def get_job(session, job_id):
    row = session.get(Job, job_id)
    if not row:
        raise HTTPException(404, "Job not found.")
    return row


def get_speaker(session, speaker_id):
    row = session.get(Speaker, speaker_id)
    if not row:
        raise HTTPException(404, "Speaker not found.")
    return row


def active_segments(session, recording_id):
    return session.scalars(select(Segment).where(Segment.recording_id == recording_id, Segment.is_deleted == False)
        .options(defer(Segment.alternates),defer(Segment.machine_history),defer(Segment.machine_text),defer(Segment.machine_english),defer(Segment.words))
        .order_by(Segment.start, Segment.order, Segment.id)).all()


def reorder(session, recording_id):
    # Chronological ordering is indexed and computed on read. Gaps in the old
    # integer order do not require rewriting thousands of unrelated rows.
    session.flush()


def number(value, label, minimum=0, maximum=None):
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise HTTPException(400, f"{label} must be a number.")
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise HTTPException(400, f"{label} is outside the allowed range.")
    return result


def language_code(value):
    value = {"eng": "en", "english": "en", "xho": "xh", "isixhosa": "xh", "afr": "af", "afrikaans": "af"}.get(str(value).lower(), str(value).lower())
    if value not in LANGUAGES:
        raise HTTPException(400, "Choose English, isiXhosa, or Afrikaans.")
    return value


def recording_settings(value):
    settings = {"allowed_languages": ["en", "xh", "af"], "min_speakers": None, "max_speakers": None, "asr_input": "original", "word_list": [], "redaction_list": []}
    if not isinstance(value, dict):
        raise HTTPException(400, "Recording settings must be an object.")
    settings.update(value)
    settings["allowed_languages"] = list(dict.fromkeys(language_code(item) for item in settings["allowed_languages"]))
    if not settings["allowed_languages"]:
        raise HTTPException(400, "Choose at least one allowed language.")
    if settings["asr_input"] not in {"original", "enhanced", "both"}:
        raise HTTPException(400, "Invalid ASR audio input.")
    for key in ("min_speakers", "max_speakers"):
        settings[key] = int(number(settings[key], key, 1, 100)) if settings.get(key) not in (None, "") else None
    if settings["min_speakers"] and settings["max_speakers"] and settings["min_speakers"] > settings["max_speakers"]:
        raise HTTPException(400, "Minimum speakers cannot exceed maximum speakers.")
    for key in ("word_list", "redaction_list"):
        entries = settings.get(key) or []
        if isinstance(entries, str):
            entries = entries.splitlines()
        settings[key] = [str(item).strip() for item in entries if str(item).strip()]
    return settings


def safe_audio_path(recording, version):
    if version not in {"original", "enhanced"}:
        raise HTTPException(400, "Unknown audio version.")
    from .audio import available_audio_path
    path = available_audio_path(recording, version)
    if path is None:
        raise HTTPException(404, f"The {version} audio is not available yet.")
    return path


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/session")
def session_state(request: Request):
    return {"authenticated": hmac.compare_digest(str(request.session.get("auth", "")), password_signature())}


@app.post("/api/login")
def login(request: Request, data: dict = Body(...)):
    key = request.client.host if request.client else "local"
    now = time.monotonic()
    recent = [stamp for stamp in login_attempts.get(key, []) if stamp > now - 60]
    if len(recent) >= 10:
        raise HTTPException(429, "Too many attempts. Wait one minute and try again.")
    if not os.getenv("APP_PASSWORD") or not hmac.compare_digest(str(data.get("password", "")).encode(), os.environ["APP_PASSWORD"].encode()):
        login_attempts[key] = recent + [now]
        raise HTTPException(401, "Incorrect password.")
    login_attempts.pop(key, None)
    request.session.clear()
    request.session["auth"] = password_signature()
    return {"authenticated": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"authenticated": False}


@app.get("/api/settings")
def get_settings(session: Session = Depends(db)):
    return app_config(session)


@app.patch("/api/settings")
def patch_settings(data: dict = Body(...), session: Session = Depends(db)):
    for key in ("mode", "weekly_gpu_limit"):
        if key not in data:
            continue
        value = data[key]
        if key == "mode" and value not in {"local", "kaggle"}:
            raise HTTPException(400, "Mode must be local or kaggle.")
        if key == "weekly_gpu_limit":
            value = number(value, "Weekly GPU limit", 0.01, 1000)
        row = session.get(AppSetting, key)
        if row:
            row.value = value
        else:
            session.add(AppSetting(key=key, value=value))
    session.commit()
    return app_config(session)


@app.get("/api/models")
def models():
    from .inference import models_catalog
    return models_catalog()


@app.get("/api/recordings")
def recordings(session: Session = Depends(db)):
    return [serialize_recording(session, row) for row in session.scalars(select(Recording).order_by(Recording.created_at.desc()))]


@app.post("/api/recordings")
def create_recording(data: dict = Body(...), session: Session = Depends(db)):
    title = str(data.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "A recording title is required.")
    filename = re.split(r"[/\\]", str(data.get("filename", "recording.m4a")))[-1]
    if Path(filename).suffix.lower() not in {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac", ".mp4", ".webm", ".aiff", ".opus"}:
        raise HTTPException(400, "Select an audio file such as .m4a, .wav or .mp3.")
    size = int(number(data.get("size", 0), "File size", 1, 200 * 1024 ** 3))
    count = math.ceil(size / CHUNK_SIZE)
    if "total_chunks" in data and int(number(data["total_chunks"], "Chunk count", 1)) != count:
        raise HTTPException(400, "Upload chunks must be 8 MB, except the final chunk.")
    recording = Recording(title=title[:500], notes=str(data.get("notes", "")), workshop=str(data.get("workshop", "")), date=str(data.get("date", "")), settings=recording_settings(data.get("settings", {})), filename=filename, size=size, last_modified=str(data.get("last_modified", "")))
    session.add(recording)
    session.flush()
    folder = recording_folder(recording.id)
    (folder / "chunks").mkdir(parents=True, exist_ok=True)
    session.add(Upload(recording_id=recording.id, total_chunks=count))
    session.commit()
    return serialize_recording(session, recording, True)


@app.get("/api/recordings/{recording_id}")
def recording_detail(recording_id: str, session: Session = Depends(db)):
    return serialize_recording(session, get_recording(session, recording_id), True)


@app.get("/api/recordings/{recording_id}/editor-state")
def editor_state(recording_id: str, since: float = 0, session: Session = Depends(db)):
    from .db import EditAction
    cursor=time.time()
    get_recording(session,recording_id)
    changes=[]
    for action in session.scalars(select(EditAction).where(EditAction.recording_id==recording_id,EditAction.updated_at>=since)):
        changes.extend(action.changes)
    return editor_delta(session,recording_id,changes,cursor)


@app.post("/api/recordings/{recording_id}/undo")
def undo_recording_edit(recording_id: str, data: dict = Body(default={}), session: Session = Depends(db)):
    get_recording(session,recording_id)
    return undo_edit(session,recording_id,data.get("id"))


@app.get("/api/recordings/{recording_id}/coverage")
def coverage_report(recording_id: str, session: Session = Depends(db)):
    get_recording(session,recording_id)
    path=recording_folder(recording_id) / "coverage.json"
    if not path.is_file():
        raise HTTPException(404,"This run has no coverage report.")
    return FileResponse(path,media_type="application/json",filename="coverage.json",headers={"Cache-Control":"no-store"})


@app.post("/api/recordings/{recording_id}/transcriptions")
def new_transcription(recording_id: str, session: Session = Depends(db)):
    """An independent output; only immutable compatible preparation files are reused."""
    from .inference import config
    source=get_recording(session,recording_id)
    original=DATA_DIR / source.original_path
    if not source.original_path or not original.is_file():
        raise HTTPException(400,"The original audio is not available yet.")
    version=config().get("pipeline",{}).get("version","baseline-v1")
    recording=Recording(title=f"{source.title} - {version}",notes=source.notes,workshop=source.workshop,date=source.date,
                        duration=source.duration,settings={**(source.settings or {}),"source_recording_id":source.id},
                        filename=source.filename,size=source.size,status="queued")
    session.add(recording);session.flush()
    folder=recording_folder(recording.id)
    def reuse(old,new):
        new.parent.mkdir(parents=True,exist_ok=True)
        try:os.link(old,new)
        except OSError:shutil.copyfile(old,new)
    target=folder / original.name
    reuse(original,target)
    recording.original_path=target.relative_to(DATA_DIR).as_posix()
    checkpoints=recording_folder(source.id) / "checkpoints"
    if checkpoints.is_dir():
        for path in checkpoints.rglob("*"):
            if path.is_file() and (path.name=="prepared.wav" or path.name in {"enhanced.wav","enhanced.m4a"}
                                  or path.name.startswith(("vad-","speakers-")) and path.suffix==".json"):
                reuse(path,folder / "checkpoints" / path.relative_to(checkpoints))
    from .audio import publish_checkpoint_enhancement
    publish_checkpoint_enhancement(session,recording)
    enqueue_job(session,recording.id,"pipeline",{"pipeline_version":version,"source_recording_id":source.id})
    session.commit()
    return {"id":recording.id,"pipeline_version":version}


@app.patch("/api/recordings/{recording_id}")
def patch_recording(recording_id: str, data: dict = Body(...), session: Session = Depends(db)):
    recording = get_recording(session, recording_id)
    for key in ("title", "notes", "workshop", "date"):
        if key in data:
            if key == "title" and not str(data[key]).strip():
                raise HTTPException(400, "A recording title is required.")
            setattr(recording, key, str(data[key]))
    if "settings" in data:
        recording.settings = recording_settings({**recording.settings, **data["settings"]})
    session.commit()
    return edit_response(session,recording_id,{"id":recording_id}) if session.info.get("editor_delta") else serialize_recording(session,recording,True)


@app.delete("/api/recordings/{recording_id}")
async def delete_recording(recording_id: str, session: Session = Depends(db)):
    async with upload_locks.setdefault(recording_id, asyncio.Lock()):
        recording = get_recording(session, recording_id)
        running = session.scalars(select(Job).where(Job.recording_id == recording_id, Job.status.in_(["queued", "running", "processing"]))).all()
        remote_urls = []
        for job in running:
            job.status = "cancelled"
            if job.kaggle_ref:
                remote_urls.append(f"https://www.kaggle.com/code/{job.kaggle_ref}")
        session.commit()
        folder = recording_folder(recording_id)
        from .runners import wait_for_recording
        stopped = await run_in_threadpool(wait_for_recording, recording_id, 15)
        if not stopped:
            raise HTTPException(409, "The worker is still stopping. Retry deletion shortly; the recording has been kept.")
        if folder.exists():
            try:
                await run_in_threadpool(shutil.rmtree, folder)
            except OSError:
                raise HTTPException(409, "Some recording files are still in use. Close playback and retry deletion.")
        session.delete(recording)
        session.commit()
        return {"deleted": True, "kaggle_urls": remote_urls}


@app.get("/api/recordings/{recording_id}/upload")
def upload_state(recording_id: str, session: Session = Depends(db)):
    recording = get_recording(session, recording_id)
    upload = session.scalar(select(Upload).where(Upload.recording_id == recording_id))
    if not upload:
        return {"received": [], "total_chunks": 0, "completed": bool(recording.original_path), "filename": recording.filename, "size": recording.size, "last_modified": recording.last_modified}
    received = upload.received if upload.completed else [index for index in upload.received if (recording_folder(recording_id) / "chunks" / str(index)).is_file()]
    return {"received": sorted(received), "total_chunks": upload.total_chunks, "completed": upload.completed, "filename": recording.filename, "size": recording.size, "last_modified": recording.last_modified}


@app.put("/api/recordings/{recording_id}/upload/{index}")
async def upload_chunk(recording_id: str, index: int, request: Request, session: Session = Depends(db)):
    async with upload_locks.setdefault(recording_id, asyncio.Lock()):
        recording = get_recording(session, recording_id)
        upload = session.scalar(select(Upload).where(Upload.recording_id == recording_id))
        if not upload:
            raise HTTPException(404, "Upload not found.")
        if upload.completed:
            return {"received": upload.received, "completed": True}
        if index < 0 or index >= upload.total_chunks:
            raise HTTPException(400, "Invalid chunk index.")
        folder = recording_folder(recording_id) / "chunks"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / str(index)
        temporary = folder / f"{index}.part"
        expected = min(CHUNK_SIZE, recording.size - index * CHUNK_SIZE)
        count = 0
        try:
            with temporary.open("wb") as handle:
                async for block in request.stream():
                    count += len(block)
                    if count > expected:
                        raise HTTPException(400, "Chunk is larger than its declared size.")
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            if count != expected:
                raise HTTPException(400, "Chunk upload was incomplete. Retry this chunk.")
            temporary.replace(destination)
            upload.received = sorted(set(upload.received + [index]))
            session.commit()
        finally:
            temporary.unlink(missing_ok=True)
        return {"received": upload.received, "completed": False}


def assemble_upload(recording, upload):
    folder = recording_folder(recording.id)
    for index in range(upload.total_chunks):
        chunk = folder / "chunks" / str(index)
        expected = min(CHUNK_SIZE, recording.size - index * CHUNK_SIZE)
        if not chunk.is_file() or chunk.stat().st_size != expected:
            raise HTTPException(409, f"Chunk {index + 1} is missing or incomplete. Resume the upload.")
    original = folder / ("original" + Path(recording.filename).suffix.lower())
    temporary = folder / "original.assembling"
    with temporary.open("wb") as output:
        for index in range(upload.total_chunks):
            with (folder / "chunks" / str(index)).open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    if temporary.stat().st_size != recording.size:
        temporary.unlink(missing_ok=True)
        raise HTTPException(409, "The assembled recording does not match the uploaded file size.")
    temporary.replace(original)
    return original.relative_to(DATA_DIR).as_posix()


@app.post("/api/recordings/{recording_id}/upload/complete")
async def complete_upload(recording_id: str, session: Session = Depends(db)):
    async with upload_locks.setdefault(recording_id, asyncio.Lock()):
        recording = get_recording(session, recording_id)
        upload = session.scalar(select(Upload).where(Upload.recording_id == recording_id))
        if not upload:
            raise HTTPException(404, "Upload not found.")
        if not upload.completed:
            recording.original_path = await run_in_threadpool(assemble_upload, recording, upload)
            upload.completed = True
            enqueue_job(session, recording_id)
            session.commit()
            folder = recording_folder(recording_id) / "chunks"
            await run_in_threadpool(shutil.rmtree, folder)
        return serialize_recording(session, recording, True)


@app.get("/api/recordings/{recording_id}/audio")
def audio(recording_id: str, version: str = "original", session: Session = Depends(db)):
    return FileResponse(safe_audio_path(get_recording(session, recording_id), version))


def make_waveform(path, cache):
    probe = subprocess.run([os.getenv("FFPROBE_BINARY") or os.getenv("FFPROBE", "ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, text=True, timeout=60, check=True)
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    samples = max(1, math.ceil(duration * 8000 / 12000))
    command = [os.getenv("FFMPEG_BINARY") or os.getenv("FFMPEG", "ffmpeg"), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    peaks = []
    try:
        while True:
            block = process.stdout.read(samples * 2)
            if not block:
                break
            pcm = array.array("h")
            pcm.frombytes(block[:len(block) // 2 * 2])
            peaks.append(round(max((abs(value) for value in pcm), default=0) / 32768, 4))
        if process.wait() != 0:
            raise RuntimeError("ffmpeg could not generate the waveform.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    result = {"duration": duration, "peaks": [peaks], "size": path.stat().st_size, "mtime": path.stat().st_mtime}
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(cache)
    return result


@app.get("/api/recordings/{recording_id}/waveform")
def waveform(recording_id: str, version: str = "original", session: Session = Depends(db)):
    recording = get_recording(session, recording_id)
    path = safe_audio_path(recording, version)
    cache = recording_folder(recording_id) / f"waveform-{version}.json"
    with waveform_locks.setdefault(str(cache), threading.Lock()):
        if cache.is_file():
            result = json.loads(cache.read_text())
            if result.get("size") == path.stat().st_size and result.get("mtime") == path.stat().st_mtime:
                return result
        try:
            result = make_waveform(path, cache)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
            raise HTTPException(503, f"Waveform generation failed: {error}")
        if not recording.duration:
            recording.duration = result["duration"]
            session.commit()
        return result


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, session: Session = Depends(db)):
    return serialize_job(get_job(session, job_id))


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, session: Session = Depends(db)):
    old = get_job(session, job_id)
    if old.status not in {"failed", "cancelled", "interrupted"}:
        raise HTTPException(409, "Only failed or cancelled jobs can be retried.")
    if session.scalar(select(Job.id).where(Job.recording_id == old.recording_id, Job.kind == old.kind, Job.status.in_(["queued", "running", "processing"]))):
        raise HTTPException(409, "A job is already queued or running.")
    resume_remote = bool(old.mode == "kaggle" and old.kaggle_ref and (old.payload or {}).get("resume_remote"))
    payload = dict(old.payload or {}) if resume_remote else {key: value for key, value in (old.payload or {}).items() if key not in {"dataset_ref", "dataset_created", "submitted", "cleanup_pending", "resume_remote", "remote_finished", "runtime_cache_available", "runtime_cache_ref"}}
    job = enqueue_job(session, old.recording_id, old.kind, payload)
    if resume_remote:
        job.mode, job.kaggle_ref = "kaggle", old.kaggle_ref
        job.gpu_seconds, job.started_at = old.gpu_seconds, old.started_at
    session.commit()
    return serialize_job(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, session: Session = Depends(db)):
    job = get_job(session, job_id)
    if job.status not in {"queued", "running", "processing"}:
        return serialize_job(job)
    job.status = "cancelled"
    job.step = "Cancelled locally"
    job.error = "Cancelled. Kaggle runs must also be stopped manually at the Kaggle URL." if job.mode == "kaggle" and job.kaggle_ref else "Cancelled by the reviewer."
    recording = get_recording(session, job.recording_id)
    if job.kind == "pipeline":
        recording.status, recording.error = "failed", job.error
    session.commit()
    return serialize_job(job)


@app.get("/api/segments/{segment_id}")
def segment_detail(segment_id: str, session: Session = Depends(db)):
    return serialize_segment(get_segment(session, segment_id))


@app.patch("/api/segments/{segment_id}")
def patch_segment(segment_id: str, data: dict = Body(...), session: Session = Depends(db)):
    segment = get_segment(session, segment_id)
    for alias, field in (("text", "edited_text"), ("english", "edited_english")):
        if alias in data:
            data[field] = data[alias]
    if "edited_text" in data and str(data["edited_text"]) != segment.edited_text:
        segment.edited_text = str(data["edited_text"])
        segment.english_outdated = True
        segment.review_status = "unreviewed"
        # Alignment belongs to the old text; retaining it would seek to incorrect words.
        segment.words = []
    if "edited_english" in data:
        segment.edited_english = str(data["edited_english"])
        segment.english_machine = False
        segment.english_outdated = False
    if "english_machine" in data:
        segment.english_machine = bool(data["english_machine"])
    if "review_status" in data:
        if data["review_status"] not in {"unreviewed", "reviewed", "flagged"}:
            raise HTTPException(400, "Invalid review status.")
        segment.review_status = data["review_status"]
        if segment.review_status == "reviewed":
            segment.english_machine = False
    if "speaker_id" in data:
        speaker_id = data["speaker_id"] or None
        if speaker_id and get_speaker(session, speaker_id).recording_id != segment.recording_id:
            raise HTTPException(400, "Speaker belongs to another recording.")
        segment.speaker_id = speaker_id
    if "start" in data or "end" in data:
        start = number(data.get("start", segment.start), "Start")
        end = number(data.get("end", segment.end), "End")
        if end <= start:
            raise HTTPException(400, "The end must be later than the start.")
        segment.start, segment.end = start, end
    job = None
    if "language" in data:
        language = language_code(data["language"])
        if language != segment.language:
            segment.language = language
            segment.review_status = "unreviewed"
            job = enqueue_job(session, segment.recording_id, "retranscribe", {"segment_id": segment.id, "language": language})
    segment.revision += 1
    reorder(session, segment.recording_id)
    session.commit()
    result = serialize_segment(segment)
    if job:
        result["job"] = serialize_job(job)
    return edit_response(session,segment.recording_id,result)


def split_string(value, ratio):
    if not value:
        return "", ""
    target = round(len(value) * ratio)
    boundaries = [match.start() for match in re.finditer(r"\s+", value)]
    position = min(boundaries, key=lambda offset: abs(offset - target)) if boundaries else target
    return value[:position].rstrip(), value[position:].lstrip()


def derived_segment(source, **changes):
    fields = {column.name: getattr(source, column.name) for column in Segment.__table__.columns if column.name not in {"id", "is_deleted"}}
    fields.update(changes)
    fields["machine_history"] = list(source.machine_history or []) + [draft_snapshot(source)]
    fields["review_status"] = "unreviewed"
    fields["revision"] = 0
    return Segment(**fields)


@app.post("/api/segments/{segment_id}/split")
def split_segment(segment_id: str, data: dict = Body(...), session: Session = Depends(db)):
    segment = get_segment(session, segment_id)
    position = int(number(data.get("position", len(segment.edited_text) // 2), "Cursor position", 0, len(segment.edited_text)))
    if position <= 0 or position >= len(segment.edited_text):
        raise HTTPException(400, "Place the cursor inside the original text to split it.")
    ratio = position / len(segment.edited_text)
    split_time = number(data.get("time", segment.start + (segment.end - segment.start) * ratio), "Split time", segment.start, segment.end)
    if split_time <= segment.start or split_time >= segment.end:
        raise HTTPException(400, "The split time must lie inside the segment.")
    machine = split_string(segment.machine_text, ratio)
    machine_english = split_string(segment.machine_english, ratio)
    english = split_string(segment.edited_english, ratio)
    language_left = language_code(data.get("language_left", segment.language))
    language_right = language_code(data.get("language_right", segment.language))
    left = derived_segment(segment, end=split_time, edited_text=segment.edited_text[:position].rstrip(), machine_text=machine[0], machine_english=machine_english[0], edited_english=english[0], language=language_left, words=[word for word in segment.words if float(word.get("start", segment.start)) < split_time], alternates=[], english_outdated=True)
    right = derived_segment(segment, start=split_time, edited_text=segment.edited_text[position:].lstrip(), machine_text=machine[1], machine_english=machine_english[1], edited_english=english[1], language=language_right, words=[word for word in segment.words if float(word.get("start", segment.start)) >= split_time], alternates=[], english_outdated=True)
    segment.is_deleted = True
    session.add_all([left, right])
    session.flush()
    for child in (left, right):
        if child.language != segment.language:
            enqueue_job(session, child.recording_id, "retranscribe", {"segment_id": child.id, "language": child.language})
    reorder(session, segment.recording_id)
    session.commit()
    return edit_response(session,segment.recording_id,{"segments": [serialize_segment(left,compact=True), serialize_segment(right,compact=True)]})


@app.post("/api/segments/{segment_id}/merge")
def merge_segment(segment_id: str, session: Session = Depends(db)):
    segment = get_segment(session, segment_id)
    following = session.scalar(select(Segment).where(Segment.recording_id==segment.recording_id,Segment.is_deleted==False,
        tuple_(Segment.start,Segment.order,Segment.id)>tuple_(segment.start,segment.order,segment.id)).order_by(Segment.start,Segment.order,Segment.id).limit(1))
    if following is None:
        raise HTTPException(400, "There is no following segment to merge.")
    merged = derived_segment(segment, end=max(segment.end, following.end), edited_text=" ".join(filter(None, [segment.edited_text, following.edited_text])), machine_text=" ".join(filter(None, [segment.machine_text, following.machine_text])), machine_english=" ".join(filter(None, [segment.machine_english, following.machine_english])), edited_english=" ".join(filter(None, [segment.edited_english, following.edited_english])), words=list(segment.words or []) + list(following.words or []), alternates=[], english_outdated=segment.english_outdated or following.english_outdated, english_machine=segment.english_machine or following.english_machine)
    merged.machine_history += list(following.machine_history or []) + [draft_snapshot(following)]
    segment.is_deleted = following.is_deleted = True
    session.add(merged)
    reorder(session, segment.recording_id)
    session.commit()
    return edit_response(session,merged.recording_id,serialize_segment(merged,compact=True))


@app.delete("/api/segments/{segment_id}")
def delete_segment(segment_id: str, session: Session = Depends(db)):
    segment = get_segment(session, segment_id)
    segment.is_deleted = True
    segment.revision += 1
    reorder(session, segment.recording_id)
    session.commit()
    return edit_response(session,segment.recording_id,{"deleted": True})


@app.post("/api/recordings/{recording_id}/segments")
def add_segment(recording_id: str, data: dict = Body(...), session: Session = Depends(db)):
    recording = get_recording(session, recording_id)
    start = number(data.get("start", 0), "Start", 0, recording.duration or None)
    end = number(data.get("end", min(start + 5, recording.duration) if recording.duration else start + 5), "End")
    if end <= start:
        raise HTTPException(400, "Move the playhead before the end of the recording.")
    segment = Segment(recording_id=recording_id, start=start, end=end, language=language_code(data.get("language", "en")))
    session.add(segment)
    reorder(session, recording_id)
    session.commit()
    return edit_response(session,segment.recording_id,serialize_segment(segment,compact=True))


@app.post("/api/segments/{segment_id}/retranscribe")
def retranscribe_segment(segment_id: str, data: dict = Body(default={}), session: Session = Depends(db)):
    segment = get_segment(session, segment_id)
    language = language_code(data.get("language", segment.language))
    if data.get("model_key"):
        from .inference import models_catalog
        catalog = models_catalog()
        if data["model_key"] not in {model.get("key") for model in catalog}:
            raise HTTPException(400, "Unknown ASR model.")
    job = enqueue_job(session, segment.recording_id, "retranscribe", {"segment_id": segment.id, "language": language, "model_key": data.get("model_key")})
    session.commit()
    return edit_response(session,segment.recording_id,{"job": serialize_job(job,include_log=False)})


@app.post("/api/segments/{segment_id}/translate")
def translate_segment(segment_id: str, session: Session = Depends(db)):
    segment = get_segment(session, segment_id)
    job = enqueue_job(session, segment.recording_id, "translate", {"segment_ids": [segment_id]})
    session.commit()
    return edit_response(session,segment.recording_id,{"job": serialize_job(job,include_log=False)})


@app.post("/api/recordings/{recording_id}/translate-outdated")
def translate_outdated(recording_id: str, session: Session = Depends(db)):
    get_recording(session, recording_id)
    ids = list(session.scalars(select(Segment.id).where(Segment.recording_id==recording_id,Segment.is_deleted==False,Segment.english_outdated==True)))
    if not ids:
        return {"job": None, "count": 0}
    job = enqueue_job(session, recording_id, "translate", {"segment_ids": ids})
    session.commit()
    return edit_response(session,recording_id,{"job": serialize_job(job,include_log=False), "count": len(ids)})


@app.post("/api/segments/{segment_id}/choose-alternate")
def choose_alternate(segment_id: str, data: dict = Body(...), session: Session = Depends(db)):
    segment = get_segment(session, segment_id)
    index = int(number(data.get("index"), "Alternate", 0))
    if index >= len(segment.alternates):
        raise HTTPException(400, "Alternate not found.")
    choice = segment.alternates[index]
    previous = {"text": segment.edited_text, "english": segment.edited_english, "language": segment.language, "model": segment.model, "audio_version": segment.audio_version, "words": segment.words, "confidence": segment.confidence, "source": "previous text"}
    segment.alternates = [previous] + [item for i, item in enumerate(segment.alternates) if i != index]
    segment.edited_text = choice.get("text", "")
    segment.language = language_code(choice.get("language", segment.language))
    segment.model = choice.get("model", segment.model)
    segment.audio_version = choice.get("audio_version", segment.audio_version)
    segment.words = choice.get("words") or []
    segment.confidence = float(choice.get("confidence") or 0)
    segment.review_status = "unreviewed"
    segment.english_machine = True
    segment.revision += 1
    english = choice.get("english", choice.get("translation"))
    if english is not None:
        segment.edited_english = english
        segment.english_outdated = False
    else:
        segment.english_outdated = True
        enqueue_job(session, segment.recording_id, "translate", {"segment_ids": [segment.id]})
    session.commit()
    return edit_response(session,segment.recording_id,serialize_segment(segment,compact=True))


@app.post("/api/recordings/{recording_id}/replace")
def find_replace(recording_id: str, data: dict = Body(...), session: Session = Depends(db)):
    get_recording(session, recording_id)
    find = str(data.get("find", ""))
    if not find:
        raise HTTPException(400, "Enter text to find.")
    replacement = str(data.get("replace", ""))
    selected = data.get("field", "original")
    aliases = {"original": ["edited_text"], "text": ["edited_text"], "edited_text": ["edited_text"], "english": ["edited_english"], "edited_english": ["edited_english"], "both": ["edited_text", "edited_english"]}
    if selected not in aliases:
        raise HTTPException(400, "Choose original, English, or both fields.")
    expression = re.compile(re.escape(find), 0 if data.get("case_sensitive") else re.IGNORECASE)
    changes = 0
    for segment in active_segments(session, recording_id):
        changed = False
        for field in aliases[selected]:
            updated, count = expression.subn(lambda _: replacement, getattr(segment, field))
            if count:
                changes += count
                changed = True
                setattr(segment, field, updated)
                if field == "edited_text":
                    segment.english_outdated = True
                    segment.words = []
                else:
                    segment.english_machine = False
        if changed:
            segment.revision += 1
            segment.review_status = "unreviewed"
    session.commit()
    return edit_response(session,recording_id,{"replacements": changes})


@app.post("/api/recordings/{recording_id}/speakers")
def add_speaker(recording_id: str, data: dict = Body(...), session: Session = Depends(db)):
    get_recording(session, recording_id)
    name = str(data.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "Enter a speaker name.")
    speaker = Speaker(recording_id=recording_id, name=name)
    session.add(speaker)
    session.commit()
    return edit_response(session,recording_id,{"id": speaker.id, "name": speaker.name, "recording_id": recording_id})


@app.patch("/api/speakers/{speaker_id}")
def rename_speaker(speaker_id: str, data: dict = Body(...), session: Session = Depends(db)):
    speaker = get_speaker(session, speaker_id)
    name = str(data.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "Enter a speaker name.")
    speaker.name = name
    session.commit()
    return edit_response(session,speaker.recording_id,{"id": speaker.id, "name": name, "recording_id": speaker.recording_id})


@app.post("/api/speakers/{speaker_id}/merge")
def merge_speakers(speaker_id: str, data: dict = Body(...), session: Session = Depends(db)):
    source = get_speaker(session, speaker_id)
    target = get_speaker(session, str(data.get("target_id", "")))
    if source.id == target.id or source.recording_id != target.recording_id:
        raise HTTPException(400, "Choose a different speaker from this recording.")
    for segment in session.scalars(select(Segment).where(Segment.speaker_id == source.id).options(defer(Segment.words),defer(Segment.alternates),defer(Segment.machine_history),defer(Segment.machine_text),defer(Segment.machine_english))):
        segment.speaker_id = target.id
    session.delete(source)
    session.commit()
    return edit_response(session,target.recording_id,{"id": target.id, "name": target.name, "recording_id": target.recording_id})


@app.get("/api/search")
def search(q: str = "", session: Session = Depends(db)):
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    if not terms:
        return {"results": []}
    query = " AND ".join('"' + term.replace('"', '""') + '"*' for term in terms[:20])
    hits = session.execute(sql_text("SELECT segment_id FROM segment_fts WHERE segment_fts MATCH :query ORDER BY rank LIMIT 200"), {"query": query}).scalars().all()
    results = []
    for segment_id in hits:
        segment = session.get(Segment, segment_id)
        if not segment or segment.is_deleted:
            continue
        recording = session.get(Recording, segment.recording_id)
        speaker = session.get(Speaker, segment.speaker_id) if segment.speaker_id else None
        results.append({"id": segment.id, "segment_id": segment.id, "recording_id": recording.id, "recording_title": recording.title, "title": recording.title, "speaker": speaker.name if speaker else "Unknown", "start": segment.start, "end": segment.end, "language": segment.language, "text": segment.edited_text, "english": segment.edited_english})
    return {"results": results}


@app.get("/api/recordings/{recording_id}/export")
def export(recording_id: str, format: str = "docx", language: str = "both", speakers: bool = True, timestamps: bool = True, anonymize: bool = False, layout: str = "columns", font_size: int = Query(11, ge=9, le=18), inline: bool = False, session: Session = Depends(db)):
    if format not in {"docx", "pdf", "html", "srt", "vtt", "txt", "json"} or language not in {"original", "english", "both"} or layout not in {"columns", "stacked"}:
        raise HTTPException(400, "Unknown export format or language.")
    recording = get_recording(session, recording_id)
    speaker_rows = session.scalars(select(Speaker).where(Speaker.recording_id == recording_id).order_by(Speaker.id)).all()
    segments = active_segments(session, recording_id)
    if not segments:
        from types import SimpleNamespace
        from .progress import read_preview
        preview = read_preview(recording_folder(recording_id))
        if preview and preview.get("segments"):
            recording = SimpleNamespace(title=recording.title + " - Provisional draft", settings=recording.settings,
                                        workshop=recording.workshop, date=recording.date)
            speaker_rows = [SimpleNamespace(**row) for row in preview["speakers"]]
            segments = [SimpleNamespace(start=row["start"], end=row["end"], language=row["language"],
                                        speaker_id=row["speaker"], edited_text=row["text"], edited_english=row["english"],
                                        review_status="unreviewed", english_outdated=not row.get("translation_ready"),
                                        english_machine=True) for row in preview["segments"]]
    data, media_type, filename = export_transcript(recording, segments, speaker_rows, format, language, speakers, timestamps, anonymize, layout, font_size)
    disposition = "inline" if inline else "attachment"
    return Response(data, media_type=media_type, headers={"Content-Disposition": disposition + "; filename*=UTF-8''" + quote(filename), "Cache-Control": "no-store"})


@app.get("/api/accuracy")
def accuracy(session: Session = Depends(db)):
    import jiwer
    segments = session.scalars(select(Segment).where(Segment.is_deleted == False, Segment.review_status == "reviewed")).all()
    groups = {}
    for segment in segments:
        if not segment.machine_text and not segment.model:
            continue
        initial = (segment.machine_history or [{}])[0]
        key = (initial.get("model") or segment.model or "Unknown", initial.get("language") or segment.suggested_language or segment.language)
        groups.setdefault(key, []).append(segment)
    rows = []
    for (model, language), items in groups.items():
        reference = [segment.edited_text for segment in items]
        hypothesis = [segment.machine_text for segment in items]
        words = jiwer.process_words(reference, hypothesis)
        characters = jiwer.process_characters(reference, hypothesis)
        rows.append({"model": model, "language": language, "segments": len(items), "count": len(items), "wer": words.wer, "cer": characters.cer, "words": words.hits + words.substitutions + words.deletions, "substitutions": words.substitutions, "deletions": words.deletions, "insertions": words.insertions})
    return {"groups": sorted(rows, key=lambda row: (row["language"], row["model"])), "reviewed_segments": len(segments)}


frontend = ROOT / "frontend" / "dist"
if (frontend / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")


@app.get("/{path:path}")
def frontend_page(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "API route not found.")
    requested = (frontend / path).resolve()
    if requested.is_relative_to(frontend.resolve()) and requested.is_file():
        return FileResponse(requested)
    index = frontend / "index.html"
    if index.is_file():
        return FileResponse(index)
    return Response("Frontend has not been built. Run npm install and npm run build in frontend/.", status_code=503, media_type="text/plain")
