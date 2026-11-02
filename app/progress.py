"""Read-only stage previews, independent of the editable final transcript."""
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import threading
import uuid
import zlib

from .checkpoints import fingerprint, save_json, source_key

PREFIX = "[resource] "
_sequence = 0
_last_translation_write = 0


def alternate_drafts(options):
    return [{key: item.get(key) for key in ("text", "language", "model", "audio_version", "confidence")}
            for item in options]


def draft(segments, candidates, languages, audio_version, duration, completed, total):
    rows = []
    for index, segment in enumerate(segments):
        options = sorted(candidates[index], key=lambda item: item["confidence"], reverse=True)
        winner = options[0] if options else {"text": "", "language": languages[0], "confidence": 0,
                                           "words": [], "model": "", "audio_version": audio_version}
        english = winner["text"] if winner["language"] == "en" else ""
        rows.append({**segment, "index": index, "text": winner["text"], "english": english,
                     "language": winner["language"], "confidence": winner["confidence"],
                     "words": winner["words"], "model": winner["model"], "alternates": alternate_drafts(options[1:]),
                     "translation_ready": winner["language"] == "en" or not winner["text"]})
    speakers = list(dict.fromkeys(row["speaker"] for row in rows))
    return {"version": 1, "stage": "transcription" if completed else "speakers", "duration": duration,
            "recognition_done": completed, "recognition_total": total, "segments": rows,
            "speakers": [{"id": key, "name": f"Speaker {index + 1}"} for index, key in enumerate(speakers)]}


def emit_event(event):
    global _sequence
    _sequence += 1
    event = {**event, "sequence": _sequence, "run_id": os.environ.get("RESOURCE_RUN_ID", "worker")}
    compressed = zlib.compress(json.dumps(event, ensure_ascii=False, allow_nan=False).encode(), level=1)
    encoded = base64.b64encode(compressed).decode()
    chunks = [encoded[index:index + 8192] for index in range(0, len(encoded), 8192)]
    identity = uuid.uuid4().hex
    checksum = hashlib.sha256(compressed).hexdigest()
    for index, chunk in enumerate(chunks):
        print(PREFIX + json.dumps({"id": identity, "part": index, "parts": len(chunks),
                                    "sha256": checksum, "data": chunk}), flush=True)


def publish_snapshot(folder, preview):
    preview = {**preview, "source": "worker", "updated_at": time.time()}
    save_json(Path(folder) / "preview.json", preview)
    emit_event({"kind": "snapshot", "data": preview})


def publish_translation(folder, preview, index, english):
    global _last_translation_write
    preview["segments"][index].update(english=english, translation_ready=True)
    preview["stage"] = "translation"
    emit_event({"kind": "translation", "index": index, "english": english})
    if time.monotonic() - _last_translation_write >= 2:
        save_json(Path(folder) / "preview.json", preview)
        _last_translation_write = time.monotonic()


def read_preview(folder):
    try:
        return json.loads((Path(folder) / "preview.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def preview_from_result(folder, result):
    rows = [{"start": row["start"], "end": row["end"], "language": row.get("language", "en"),
             "words": row.get("words", []), "model": row.get("model", ""), "confidence": row.get("confidence", 0),
             "alternates": alternate_drafts(row.get("alternates", [])),
             "index": index, "speaker": row.get("speaker", row.get("speaker_id", "Unknown")),
             "text": row.get("text", row.get("machine_text", "")),
             "english": row.get("english", row.get("machine_english", "")), "translation_ready": True}
            for index, row in enumerate(result.get("segments", []))]
    preview = {"version": 1, "source": "download", "stage": "finalizing", "segments": rows,
               "speakers": result.get("speakers", []), "duration": result.get("duration", 0),
               "recognition_done": len(rows), "recognition_total": len(rows), "updated_at": time.time()}
    save_json(Path(folder) / "preview.json", preview)


class ResourceReceiver:
    def __init__(self, folder, job_id):
        self.folder, self.job_id = Path(folder), job_id
        self.pending = {}
        self.preview = read_preview(folder)
        self.last_sequence = (self.preview or {}).get("sequence", 0) if (self.preview or {}).get("run_id") == job_id else 0
        self.dirty = False
        self.last_write = 0
        self.lock = threading.RLock()

    def accept(self, line):
        with self.lock:
            self._accept(line)

    def _accept(self, line):
        packet = json.loads(line[len(PREFIX):])
        count, index = int(packet["parts"]), int(packet["part"])
        if not (1 <= count <= 4096 and 0 <= index < count) or len(packet["data"]) > 8192:
            raise ValueError("Invalid resource packet size.")
        if packet["id"] not in self.pending and len(self.pending) >= 4:
            self.pending.pop(next(iter(self.pending)))
        state = self.pending.setdefault(packet["id"], {"parts": count, "sha256": packet["sha256"], "chunks": {}})
        if state["parts"] != count or state["sha256"] != packet["sha256"]:
            raise ValueError("Inconsistent resource packets.")
        state["chunks"][index] = packet["data"]
        if len(state["chunks"]) != count:
            return
        compressed = base64.b64decode("".join(state["chunks"][i] for i in range(count)), validate=True)
        del self.pending[packet["id"]]
        if hashlib.sha256(compressed).hexdigest() != state["sha256"]:
            raise ValueError("Resource checksum failed.")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, 32 * 1024**2 + 1)
        if len(raw) > 32 * 1024**2 or not decoder.eof:
            raise ValueError("Resource exceeds the preview limit.")
        event = json.loads(raw)
        if event.get("run_id") != self.job_id or int(event.get("sequence", 0)) <= self.last_sequence:
            return
        if event["kind"] == "snapshot":
            self.preview = event["data"]
        elif event["kind"] == "translation" and self.preview:
            segment = self.preview["segments"][int(event["index"])]
            segment.update(english=str(event["english"]), translation_ready=True)
            self.preview["stage"] = "translation"
        else:
            return
        self.last_sequence = int(event["sequence"])
        self.preview.update(run_id=self.job_id, sequence=self.last_sequence, source="live", updated_at=time.time())
        self.dirty = True
        self.flush(force=event["kind"] == "snapshot")

    def flush(self, force=False):
        with self.lock:
            if self.dirty and (force or time.monotonic() - self.last_write >= 2):
                save_json(self.folder / "preview.json", self.preview)
                self.dirty = False
                self.last_write = time.monotonic()


def recover_checkpoint_preview(recording):
    """Use only local checkpoints matching the source, settings and model IDs."""
    from .db import DATA_DIR, recording_folder
    from .inference import choices_for, config
    from worker.run import split_speakers
    folder = recording_folder(recording.id)
    previous = read_preview(folder)
    if previous and previous.get("source") != "checkpoint":
        return
    if not recording.original_path or not (folder / "checkpoints").is_dir():
        return
    checkpoint = folder / "checkpoints" / source_key(DATA_DIR / recording.original_path)
    catalog, settings = config(), recording.settings or {}
    enhancement_key = fingerprint(["enhancement-v2", catalog["enhancement"]])
    version = settings.get("asr_input", "original")
    vad_key = fingerprint(["vad-v1", version, enhancement_key if version != "original" else None, catalog["vad"]])
    regions_file = checkpoint / f"vad-{vad_key}.json"
    if not regions_file.is_file():
        return
    speakers = sorted(checkpoint.glob("speakers-*.json"))
    if not speakers:
        return
    speaker_key = fingerprint(["speakers-v1", catalog["diarization"], settings.get("min_speakers"), settings.get("max_speakers"), "cuda"])
    turns_file = checkpoint / f"speakers-{speaker_key}.json"
    if not turns_file.is_file():
        return
    segments = split_speakers(json.loads(regions_file.read_text()), json.loads(turns_file.read_text()))
    candidates = [[] for _ in segments]
    languages = list(dict.fromkeys(settings.get("allowed_languages") or ["en", "xh", "af"]))
    versions = ["original", "enhanced"] if version == "both" else [version]
    done = total = 0
    for language in languages:
        for key in choices_for(language):
            for audio in versions:
                total += len(segments)
                identity = fingerprint(["asr-v1", catalog["asr"][key], key, language, audio,
                                        enhancement_key if audio == "enhanced" else None, segments, settings.get("word_list", ""), "cuda"])
                for index in range(len(segments)):
                    try:
                        value = json.loads((checkpoint / ("asr-" + identity) / f"{index:05d}.json").read_text())
                        done += 1
                        if value:
                            candidates[index].append(value)
                    except (OSError, ValueError):
                        pass
    preview = draft(segments, candidates, languages, versions[0], recording.duration, done, total)
    for row in preview["segments"]:
        identity = fingerprint(["translation-v1", row["text"], row["language"], catalog["translation"]])
        try:
            row.update(english=json.loads((checkpoint / "translations" / f"{identity}.json").read_text()), translation_ready=True)
        except (OSError, ValueError):
            pass
    preview.update(source="checkpoint", updated_at=time.time())
    save_json(folder / "preview.json", preview)
