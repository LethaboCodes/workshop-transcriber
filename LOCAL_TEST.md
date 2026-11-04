# Clean local acceptance test

The local library was reset for this test. Credentials in `.env` are retained; recordings, jobs, transcripts, upload chunks and local model caches were cleared. The original recording is preserved outside `data/` and excluded from Git and Docker build contexts:

`C:/Users/simon/repos/workshop-transcriber/test-audio/mw_day1_07_10_2024.m4a`

1. Open http://localhost:8000 and sign in with the existing app password.
2. Choose **New recording** and upload that file yourself.
3. Keep the existing English / isiXhosa / Afrikaans passes and original audio input. Leave **Generate English translations after transcription** unchecked.
4. The upload should resume after an interrupted connection. An incomplete upload must not queue a processing job.
5. Check **Show processing log** or run `docker compose logs -f --tail=100 web`.
6. Kaggle setup should pass dependency, CUDA-library, model-access and actual per-model/per-GPU inference checks. Both GPU UUIDs should appear in its logs.
7. Enhancement can overlap original-audio recognition. Confirm all four configured passes finish, recovery completes or reports its budget limits, and the app imports the result.
8. Review the previously problematic opening and the 00:57–09:27 interval in this independent output. Keep the coverage report and timing fields for comparison.
9. Verify playback/seeking, edit/delete/merge and Undo, PDF/export preview, and **Show translations**. Showing translations must not generate them; explicit **Generate missing translations** runs a separate CPU job.

No long run is queued automatically for this acceptance test. Cold setup still downloads dependencies and model weights; the first run is the least representative of warm startup speed. Synthetic failure checks and small real-model checks establish execution correctness, not a measured full-recording accuracy or T4 speedup.

## Deployment after acceptance

This is one Docker web service with persistent `data/` storage and a separate private Kaggle GPU notebook. Use a single web process/instance: its SQLite database and job dispatcher are local. Keep secrets in environment variables, serve through HTTPS, and set `COOKIE_SECURE=true` for the hosted URL. The container accepts a hosting provider's `PORT` and has a health check.

A static GitHub Pages site cannot host this backend. Vercel serverless deployment would require an architecture/storage change. Free services with ephemeral disks cannot safely retain this app's uploads/database through restarts. Free access from this computer is configured through the optional Tailscale Funnel companion described in [SHARING.md](SHARING.md). The computer must remain on. No paid cloud service is required.
