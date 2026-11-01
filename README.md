# Workshop Transcriber

Review English, isiXhosa and Afrikaans workshops in one local workspace.
Requires Docker Desktop with its Linux engine running.
Copy `.env.example` to `.env` and set the following values:
- `APP_PASSWORD`: your single-user sign-in password (required).
- `HF_TOKEN`: Hugging Face read token; accept both the diarization and Sunbird model terms using the same account.
- `KAGGLE_USERNAME` and `KAGGLE_KEY`: credentials for private GPU jobs.
- `JOB_MODE`: `kaggle` (default) or `local`; change it in Settings too.
- `WEEKLY_GPU_LIMIT`: estimated weekly GPU-hour budget, default `30`.
- `COOKIE_SECURE`: set `true` when serving behind HTTPS.

Run: `docker compose up --build`
For background mode use `docker compose up --build -d`; follow runtime/job logs with `docker compose logs -f --tail=200 web`. Save a snapshot with `docker compose logs --no-color --tail=500 web > runtime-logs.txt`.
Open http://localhost:8000 and sign in with `APP_PASSWORD`.
All recordings, transcripts, upload chunks and cached models stay under `data/`.
Files placed in `data/inbox/` are imported and queued on startup.
Model repositories and language mappings are configured only in `models.yaml`.
Local jobs need Python 3.11, ffmpeg and `worker/requirements.txt` installed on the host; run the web app on that host with `WORKER_PYTHON` pointing to that interpreter. The Docker web image intentionally has no PyTorch, so use Kaggle in Docker.
Kaggle cancellation is local; stop the linked private notebook on Kaggle as well.
Kaggle workers use isolated Python environments and check dependencies/GPU access before audio processing. The first model run downloads weights; missing credentials or failed jobs show a retry action.
