"""Import recordings deliberately placed in the local inbox."""
import shutil
from sqlalchemy import select
from .db import DATA_DIR, Recording, SessionLocal, enqueue_job, recording_folder


def import_inbox():
    inbox = DATA_DIR / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for original in sorted(inbox.iterdir()):
        if not original.is_file() or original.suffix.lower() not in {".m4a", ".mp3", ".wav", ".mp4", ".ogg", ".flac"}:
            continue
        with SessionLocal() as session:
            record = Recording(title=original.stem.replace("_", " "), filename=original.name,
                               size=original.stat().st_size, status="queued",
                               settings={"allowed_languages": ["en", "xh", "af"], "asr_input": "original", "word_list": [], "redaction_list": []})
            session.add(record)
            session.flush()
            folder = recording_folder(record.id)
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / original.name
            try:
                shutil.move(str(original), str(destination))
                record.original_path = destination.relative_to(DATA_DIR).as_posix()
                enqueue_job(session, record.id)
                session.commit()
            except Exception:
                session.rollback()
                if destination.exists() and not original.exists():
                    shutil.move(str(destination), str(original))
                raise
