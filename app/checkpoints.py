"""Portable worker checkpoints containing derived audio and inference results only."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()[:32]


def source_key(original, max_seconds=None):
    digest = hashlib.sha256()
    with Path(original).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return fingerprint(["prepared-audio-v1", digest.hexdigest(), max_seconds])


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def cached_json(path, compute):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8")), True
    except (OSError, ValueError):
        value = compute()
        save_json(path, value)
        return value, False


def pack_checkpoints(recording, destination):
    """Never include credentials, model weights, or incomplete file writes."""
    root = Path(recording) / "checkpoints"
    if not root.is_dir():
        return False
    files = [path for path in root.rglob("*") if path.is_file()
             and path.suffix in {".wav", ".m4a", ".json"} and ".partial" not in path.name]
    if not files:
        return False
    destination = Path(destination)
    temporary = destination.with_suffix(".partial")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
    temporary.replace(destination)
    return True


def unpack_checkpoints(source, recording):
    """Validate every member before extracting an app-generated checkpoint bundle."""
    root = (Path(recording) / "checkpoints").resolve()
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if sum(item.file_size for item in members) > 16 * 1024 ** 3:
            raise ValueError("Checkpoint bundle exceeds the 16 GB limit.")
        for item in members:
            target = (root / item.filename).resolve()
            if (not target.is_relative_to(root) or "\\" in item.filename
                    or target.suffix not in {".wav", ".m4a", ".json"}
                    or ".partial" in target.name or item.is_dir()):
                raise ValueError("Invalid file in checkpoint bundle.")
        for item in members:
            target = root / item.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".partial")
            with archive.open(item) as incoming, temporary.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            temporary.replace(target)
