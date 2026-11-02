"""Read Kaggle's authenticated live SSE stream without altering the notebook."""
import json
import threading
import time
import zlib
from sqlalchemy import func, update

from .db import Job, SessionLocal, recording_folder
from .progress import PREFIX, ResourceReceiver


class LiveKaggleLog:
    def __init__(self, api, reference, job_id, recording_id, report, stopped):
        self.api, self.reference, self.job_id = api, reference, job_id
        self.report, self.stopped = report, stopped
        self.receiver = ResourceReceiver(recording_folder(recording_id), job_id)
        self.closed = threading.Event()
        self.response = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="kaggle-live-log")
        self.thread.start()

    def run(self):
        from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelSessionLogsStreamRequest
        with SessionLocal() as session:
            job = session.get(Job, self.job_id)
            cursor = float((job.payload or {}).get("live_log_time", -1)) if job else -1
        while not self.closed.is_set() and not self.stopped():
            batch, buffer = [], ""
            last_flush = time.monotonic()
            latest = cursor
            def flush():
                nonlocal batch, last_flush, cursor
                if batch:
                    self.report("".join(batch))
                    batch = []
                if latest > cursor:
                    with SessionLocal() as session:
                        session.execute(update(Job).where(Job.id == self.job_id).values(
                            payload=func.json_set(Job.payload, "$.live_log_time", latest)))
                        session.commit()
                    cursor = latest
                self.receiver.flush()
                last_flush = time.monotonic()
            def consume(event):
                nonlocal buffer, latest
                value = str(event.get("data", ""))
                stamp = float(event.get("time", 0))
                buffer += value
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.startswith(PREFIX):
                        try:
                            self.receiver.accept(line)
                        except (ValueError, KeyError, IndexError, TypeError, zlib.error):
                            self.report("A live preview update could not be decoded; final results will still be downloaded normally.")
                    elif stamp > cursor:
                        batch.append(line + "\n")
                latest = max(latest, stamp)
                if len(batch) >= 40 or time.monotonic() - last_flush >= 1:
                    flush()
            try:
                owner, slug, _ = self.api.parse_kernel_string(self.reference)
                request = ApiGetKernelSessionLogsStreamRequest()
                request.user_name, request.kernel_slug = owner, slug
                request.wait_for_logs_url_seconds = 5
                with self.api.build_kaggle_client() as client:
                    self.response = client.kernels.kernels_api_client.get_kernel_session_logs_stream(request)
                    if "application/json" in self.response.headers.get("Content-Type", ""):
                        for event in self.response.json():
                            if self.closed.is_set() or self.stopped():
                                break
                            consume(event)
                    else:
                        for raw in self.response.iter_lines(chunk_size=1024, decode_unicode=True):
                            if self.closed.is_set() or self.stopped():
                                break
                            if raw and raw.startswith("data:"):
                                consume(json.loads(raw[5:].strip()))
                    flush()
            except Exception:
                # Streaming is optional; status monitoring and final downloads
                # remain available if the stream is interrupted.
                flush()
            finally:
                self.receiver.flush(force=True)
                if self.response is not None:
                    self.response.close()
                    self.response = None
            self.closed.wait(5)

    def close(self):
        self.closed.set()
        # Reader is daemonized and exits at the next SSE event or network timeout.
        try:
            self.receiver.flush(force=True)
        except OSError:
            pass
