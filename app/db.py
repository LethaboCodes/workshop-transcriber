"""SQLite storage, durable jobs, and immutable transcription draft history."""
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, JSON, String, Text, case, create_engine, event, func, select
from sqlalchemy.orm import declarative_base, defer, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DATA_DIR / 'transcriber.sqlite3'}", connect_args={"check_same_thread": False, "timeout": 60})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return uuid.uuid4().hex


@event.listens_for(engine, "connect")
def configure_sqlite(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=60000")


class Recording(Base):
    __tablename__ = "recording"
    id = Column(String, primary_key=True, default=new_id)
    title = Column(String, nullable=False)
    notes = Column(Text, default="")
    workshop = Column(String, default="")
    date = Column(String, default="")
    duration = Column(Float, default=0)
    status = Column(String, default="uploading")
    error = Column(Text, default="")
    original_path = Column(String, default="")
    enhanced_path = Column(String, default="")
    settings = Column(JSON, default=dict)
    filename = Column(String, default="")
    size = Column(Integer, default=0)
    last_modified = Column(String, default="")
    created_at = Column(String, default=utcnow)


class Upload(Base):
    __tablename__ = "upload"
    id = Column(String, primary_key=True, default=new_id)
    recording_id = Column(String, ForeignKey("recording.id", ondelete="CASCADE"), nullable=False, unique=True)
    total_chunks = Column(Integer, nullable=False)
    received = Column(JSON, default=list)
    completed = Column(Boolean, default=False)


class Job(Base):
    __tablename__ = "job"
    id = Column(String, primary_key=True, default=new_id)
    recording_id = Column(String, ForeignKey("recording.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String, default="pipeline")
    payload = Column(JSON, default=dict)
    mode = Column(String, default="local")
    step = Column(String, default="Queued")
    log = Column(Text, default="")
    status = Column(String, default="queued", index=True)
    gpu_seconds = Column(Float, default=0)
    kaggle_ref = Column(String, default="")
    started_at = Column(String, default="")
    ended_at = Column(String, default="")
    created_at = Column(String, default=utcnow)
    error = Column(Text, default="")


class Speaker(Base):
    __tablename__ = "speaker"
    id = Column(String, primary_key=True, default=new_id)
    recording_id = Column(String, ForeignKey("recording.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)


class Segment(Base):
    __tablename__ = "segment"
    id = Column(String, primary_key=True, default=new_id)
    recording_id = Column(String, ForeignKey("recording.id", ondelete="CASCADE"), nullable=False, index=True)
    order = Column(Integer, default=0)
    start = Column(Float, default=0)
    end = Column(Float, default=0)
    speaker_id = Column(String, ForeignKey("speaker.id", ondelete="SET NULL"), nullable=True)
    language = Column(String, default="en")
    suggested_language = Column(String, default="en")
    machine_text = Column(Text, default="")
    edited_text = Column(Text, default="")
    machine_english = Column(Text, default="")
    edited_english = Column(Text, default="")
    english_outdated = Column(Boolean, default=False)
    english_machine = Column(Boolean, default=True)
    confidence = Column(Float, default=0)
    words = Column(JSON, default=list)
    alternates = Column(JSON, default=list)
    review_status = Column(String, default="unreviewed")
    model = Column(String, default="")
    audio_version = Column(String, default="original")
    machine_history = Column(JSON, default=list)
    revision = Column(Integer, default=0)
    is_deleted = Column(Boolean, default=False)


class AppSetting(Base):
    __tablename__ = "app_setting"
    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False)


def init_db():
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE VIRTUAL TABLE IF NOT EXISTS segment_fts USING fts5(segment_id UNINDEXED, recording_id UNINDEXED, text, english, tokenize='unicode61')")
        connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS segment_fts_insert AFTER INSERT ON segment WHEN new.is_deleted=0 BEGIN INSERT INTO segment_fts(segment_id,recording_id,text,english) VALUES(new.id,new.recording_id,new.edited_text,new.edited_english); END")
        connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS segment_fts_update AFTER UPDATE ON segment BEGIN DELETE FROM segment_fts WHERE segment_id=old.id; INSERT INTO segment_fts(segment_id,recording_id,text,english) SELECT new.id,new.recording_id,new.edited_text,new.edited_english WHERE new.is_deleted=0; END")
        connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS segment_fts_delete AFTER DELETE ON segment BEGIN DELETE FROM segment_fts WHERE segment_id=old.id; END")
        connection.exec_driver_sql("DELETE FROM segment_fts")
        connection.exec_driver_sql("INSERT INTO segment_fts(segment_id,recording_id,text,english) SELECT id,recording_id,edited_text,edited_english FROM segment WHERE is_deleted=0")


def recording_folder(recording_id):
    if not recording_id or any(c not in "0123456789abcdef-" for c in recording_id):
        raise ValueError("Invalid recording id")
    folder = (DATA_DIR / "recordings" / recording_id).resolve()
    if not folder.is_relative_to(DATA_DIR):
        raise ValueError("Recording path is outside the data folder")
    return folder


def app_config(session):
    config = {"mode": os.getenv("JOB_MODE", "kaggle"), "weekly_gpu_limit": float(os.getenv("WEEKLY_GPU_LIMIT", "30"))}
    config.update({row.key: row.value for row in session.scalars(select(AppSetting))})
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    jobs = session.scalars(select(Job).where(Job.started_at >= since)).all()
    config["gpu_hours"] = sum(max(0, job.gpu_seconds or 0) for job in jobs) / 3600
    config["credentials"] = {"hf_token": bool(os.getenv("HF_TOKEN")), "kaggle_username": bool(os.getenv("KAGGLE_USERNAME")), "kaggle_key": bool(os.getenv("KAGGLE_KEY"))}
    return config


def enqueue_job(session, recording_id, kind="pipeline", payload=None):
    job = Job(recording_id=recording_id, kind=kind, payload=payload or {}, mode=app_config(session)["mode"] if kind == "pipeline" else "cpu")
    session.add(job)
    if kind == "pipeline":
        recording = session.get(Recording, recording_id)
        if recording:
            recording.status, recording.error = "queued", ""
    session.flush()
    return job


def append_log(job_id, message):
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job:
            job.log = (job.log or "") + str(message).rstrip() + "\n"
            session.commit()


def serialize_segment(segment, compact=False):
    omitted = {"is_deleted"}
    if compact:
        omitted.update({"alternates", "machine_history", "machine_text", "machine_english"})
    data = {column.name: getattr(segment, column.name) for column in Segment.__table__.columns if column.name not in omitted}
    data["alternate_count"] = getattr(segment, "_alternate_count", 0) if compact else len(segment.alternates or [])
    data["text"] = segment.edited_text
    data["english"] = segment.edited_english
    return data


def serialize_job(job, include_log=True):
    data = {column.name: getattr(job, column.name) for column in Job.__table__.columns if include_log or column.name != "log"}
    data["kaggle_url"] = f"https://www.kaggle.com/code/{job.kaggle_ref}" if job.kaggle_ref else None
    return data


def serialize_recording(session, recording, detail=False):
    from .audio import available_audio_path
    from .progress import read_preview
    data = {column.name: getattr(recording, column.name) for column in Recording.__table__.columns if column.name not in {"original_path", "enhanced_path"}}
    if detail:
        import json
        report_path = recording_folder(recording.id) / "coverage.json"
        if report_path.is_file():
            try:
                report=json.loads(report_path.read_text(encoding="utf-8"))
                data["quality_report"]={key:report.get(key) for key in ("version","recognition_windows","speech_without_words_seconds","recovery_windows","ambiguous_selections")}
                data["quality_report"]["unresolved_issues"]=len(report.get("issues",[]))
            except (OSError,ValueError):
                pass
    condition = (Segment.recording_id == recording.id, Segment.is_deleted == False)
    if detail:
        rows = session.execute(select(Segment, func.json_array_length(Segment.alternates)).where(*condition)
                               .options(defer(Segment.alternates), defer(Segment.machine_history),
                                        defer(Segment.machine_text), defer(Segment.machine_english))
                               .order_by(Segment.order, Segment.start)).all()
        segments = []
        for segment, alternate_count in rows:
            segment._alternate_count = alternate_count or 0
            segments.append(segment)
        data["segment_count"] = len(segments)
        data["reviewed_count"] = sum(s.review_status == "reviewed" for s in segments)
    else:
        data["segment_count"], data["reviewed_count"] = session.execute(
            select(func.count(Segment.id), func.coalesce(func.sum(case((Segment.review_status == "reviewed", 1), else_=0)), 0))
            .where(*condition)).one()
        segments = []
    data["review_progress"] = data["reviewed_count"] / data["segment_count"] if data["segment_count"] else 0
    data["has_original"] = bool(recording.original_path)
    data["has_enhanced"] = available_audio_path(recording, "enhanced") is not None
    data["audio"] = {"original": f"/api/recordings/{recording.id}/audio?version=original" if recording.original_path else None, "enhanced": f"/api/recordings/{recording.id}/audio?version=enhanced" if data["has_enhanced"] else None}
    preview = read_preview(recording_folder(recording.id)) if not data["segment_count"] and recording.status != "ready" else None
    if preview:
        data["preview_summary"] = {"stage": preview["stage"], "segments": len(preview.get("segments", [])),
                                   "speakers": len(preview.get("speakers", [])), "recognition_done": preview.get("recognition_done", 0),
                                   "recognition_total": preview.get("recognition_total", 0),
                                   "translations_ready": sum(bool(row.get("translation_ready")) for row in preview.get("segments", []))}
    jobs_query = select(Job).where(Job.recording_id == recording.id).options(defer(Job.log)).order_by(Job.created_at.desc())
    jobs = session.scalars(jobs_query if detail else jobs_query.limit(1)).all()
    data["latest_job"] = serialize_job(jobs[0], include_log=False) if jobs else None
    if detail:
        data["preview"] = preview
        data["segments"] = [serialize_segment(s, compact=True) for s in segments]
        data["speakers"] = [{"id": s.id, "name": s.name, "recording_id": s.recording_id} for s in session.scalars(select(Speaker).where(Speaker.recording_id == recording.id))]
        data["jobs"] = [serialize_job(job, include_log=False) for job in jobs]
    return data


def draft_snapshot(segment):
    return {"segment_id": segment.id, "text": segment.machine_text, "english": segment.machine_english, "model": (segment.machine_history or [{}])[0].get("model", segment.model), "language": (segment.machine_history or [{}])[0].get("language", segment.suggested_language), "start": segment.start, "end": segment.end}


def apply_translation(session, segment, english):
    segment.edited_english = str(english)
    segment.english_outdated = False
    segment.english_machine = True
    segment.revision += 1
    session.flush()


def apply_retranscription(session, segment, result):
    old = {"text": segment.edited_text, "english": segment.edited_english, "language": segment.language, "model": segment.model, "audio_version": segment.audio_version, "confidence": segment.confidence, "words": segment.words, "source": "previous text"}
    segment.alternates = [old] + list(result.get("alternates") or []) + list(segment.alternates or [])
    segment.edited_text = result.get("text", "")
    segment.edited_english = result.get("english", result.get("translation", ""))
    segment.words = result.get("words") or []
    segment.confidence = float(result.get("confidence") or 0)
    segment.language = result.get("language", segment.language)
    segment.model = result.get("model", segment.model)
    segment.audio_version = result.get("audio_version", segment.audio_version)
    segment.english_outdated = not bool(segment.edited_english) and bool(segment.edited_text)
    segment.english_machine = True
    segment.review_status = "unreviewed"
    segment.machine_history = list(segment.machine_history or [draft_snapshot(segment)]) + [{"text": segment.edited_text, "english": segment.edited_english, "model": segment.model, "language": segment.language, "source": "retranscription", "created_at": utcnow()}]
    segment.revision += 1
    session.flush()


def import_result(recording_id, result, job_id=None):
    """Import an entire result atomically; never replace an already reviewed transcript."""
    with SessionLocal() as session:
        recording = session.get(Recording, recording_id)
        job = session.get(Job, job_id) if job_id else session.scalar(select(Job).where(Job.recording_id == recording_id, Job.kind == "pipeline").order_by(Job.created_at.desc()))
        if not recording or (job and job.status == "cancelled"):
            return False
        existing = session.scalars(select(Segment).where(Segment.recording_id == recording_id, Segment.is_deleted == False)).all()
        if existing:
            raise ValueError("This recording already has a transcript; rerun individual segments to preserve review work.")
        speakers = {}
        for index, item in enumerate(result.get("speakers") or []):
            key = item.get("id", item.get("label", str(index))) if isinstance(item, dict) else str(item)
            name = item.get("name", key) if isinstance(item, dict) else key
            speaker = Speaker(recording_id=recording_id, name=name)
            session.add(speaker)
            session.flush()
            speakers[key] = speaker.id
        for index, item in enumerate(result.get("segments") or []):
            key = item.get("speaker_id", item.get("speaker", "Speaker 1"))
            if key not in speakers:
                speaker = Speaker(recording_id=recording_id, name=str(key or "Speaker 1"))
                session.add(speaker)
                session.flush()
                speakers[key] = speaker.id
            text = str(item.get("text", item.get("machine_text", "")))
            english = str(item.get("english", item.get("machine_english", item.get("translation", ""))))
            language = item.get("language", "en")
            model = item.get("model", "")
            segment = Segment(recording_id=recording_id, order=index, start=float(item["start"]), end=float(item["end"]), speaker_id=speakers[key], language=language, suggested_language=item.get("suggested_language", language), machine_text=text, edited_text=text, machine_english=english, edited_english=english, confidence=float(item.get("confidence") or 0), words=item.get("words") or [], alternates=item.get("alternates") or [], model=model, audio_version=item.get("audio_version", "original"), review_status="flagged" if item.get("review_status")=="flagged" else "unreviewed", machine_history=[{"text": text, "english": english, "model": model, "language": language, "source": "pipeline", "start": item["start"], "end": item["end"], "selection": item.get("selection"), "pipeline_version": result.get("pipeline_version", "baseline-v1")}])
            session.add(segment)
        recording.duration = float(result.get("duration") or max((float(s["end"]) for s in result.get("segments", [])), default=0))
        enhanced = result.get("enhanced_audio", result.get("enhanced_path", "enhanced.m4a"))
        candidate = recording_folder(recording_id) / str(enhanced)
        if candidate.resolve().is_relative_to(recording_folder(recording_id)) and candidate.is_file():
            recording.enhanced_path = candidate.relative_to(DATA_DIR).as_posix()
        recording.status, recording.error = "ready", ""
        if job:
            job.gpu_seconds = float(result.get("gpu_seconds", result.get("total_gpu_seconds", 0)) or 0)
        session.commit()
        return True
