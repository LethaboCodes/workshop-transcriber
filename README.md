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
New jobs save checkpoints for prepared audio, enhancement chunks, VAD, speakers, recognition and translation under each recording's `checkpoints/` folder. After a normal Kaggle worker failure the app downloads those checkpoints; Retry sends them to a fresh notebook and reuses matching work. Checkpoints from before this feature cannot be recovered, and forced notebook termination may prevent saving them. Enhancement uses two CPU workers when available. Changes to the source audio or relevant model/settings invalidate the affected checkpoints.
GPU recognition keeps every model/language pass and uses up to two isolated processes, each pinned to one GPU. Each model starts in fresh processes; real inference on every remaining model/device and translation is checked before long recognition passes. Worker logs include per-GPU UUID, utilization and memory. Fresh Kaggle installs use parallel package downloads; compatible dependencies and model files are then saved in private notebook outputs (runtime cache capped at 14 GiB). Later jobs automatically attach a previous cache within Kaggle, matching requirements and Python/platform ABI; missing models still download. Keep cache-producing private notebooks available for reuse. Existing running notebooks keep their original code.
Completed enhanced audio already stored locally is playable while transcription is running. The app recovers matching enhancement checkpoints on startup and after checkpoint downloads; a new Kaggle run's enhanced audio must first be downloaded before local preview is available.
Processing results are viewable in a read-only stage preview with speakers, word seeking, alternate drafts, translations, search and clearly labelled draft exports. New workers publish text resources through Kaggle's authenticated live log stream; local checkpoints provide a fallback. Transcript/translation files download before large checkpoint archives. Editing becomes available after final import, so previews never overwrite review work. Already-running older notebooks keep their original publication behavior until their files are exported by Kaggle.
