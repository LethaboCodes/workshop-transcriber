# Free sharing with Tailscale Funnel

The app runs on this computer. Tailscale runs in a companion Docker container,
sharing only the app's network namespace. It has no app-data or credential-file
mounts, no host-network mode, and no Windows VPN driver. Your girlfriend can use
the HTTPS address in an ordinary browser and enter the existing app password.

The computer must be awake, online, and running Docker Desktop. A Kaggle notebook
already submitted continues remotely if the computer is off, but the website is
unavailable until the computer and Docker are running again.

## Start the app and sharing

From the repository directory:

```powershell
docker compose -f compose.yaml -f compose.tailscale.yaml up -d
docker compose -f compose.yaml -f compose.tailscale.yaml exec -T tailscale tailscale funnel status
```

Funnel is configured persistently using `--bg`; it resumes after the companion
restarts. The Docker volume `tailscale-state` keeps its identity and configuration.
Do not delete that volume unless you intend to disconnect/re-register this device.
Sign-in has a 30-minute startup window so first-time account setup doesn't cause
the container to restart and invalidate its device link.

## First-time account connection

If status says logged out, retrieve its device link:

```powershell
docker compose -f compose.yaml -f compose.tailscale.yaml logs --tail=30 tailscale
```

Open the Tailscale authentication URL and finish connecting `workshop-transcriber`.
Use your free personal account. Then enable the app's public HTTPS route:

```powershell
docker compose -f compose.yaml -f compose.tailscale.yaml exec -T tailscale tailscale funnel --bg --yes 8001
```

If Tailscale provides a separate feature-approval URL, complete that in your
browser. The resulting `https://workshop-transcriber.<your-tailnet>.ts.net`
address is the link to share. Your girlfriend does not need a Tailscale account
or installation. The app password is still required.

## Stop and resume sharing

Temporarily stop sharing while keeping the app available locally:

```powershell
docker compose -f compose.yaml -f compose.tailscale.yaml stop tailscale
```

Start it again with the start command above. To remove the public route while
keeping the companion connected:

```powershell
docker compose -f compose.yaml -f compose.tailscale.yaml exec -T tailscale tailscale funnel --https=443 off
```

The normal local address remains http://localhost:8000.

## App updates

Use both Compose files when deploying updates so the companion stays attached to
the current web container's network namespace:

```powershell
docker compose -f compose.yaml -f compose.tailscale.yaml up --build -d
```

If the web container was recreated separately, reconnect its companion with:

```powershell
docker compose -f compose.yaml -f compose.tailscale.yaml up -d --no-deps --force-recreate sharing-proxy tailscale
```

The same identity and background Funnel route are retained in the state volume.
No worker or application-code changes are needed for sharing.

## Faster recording opening

The sharing-proxy companion compresses transcript JSON and frontend assets before
sending them through Funnel. It listens only on loopback port 8001 in the app's
network namespace and forwards to the unchanged app on port 8000. It has no
recording-data or credential mounts. Audio range requests, cookies and save
requests pass through. Transcript/API responses are not cached; successful audio
responses permit five minutes of private browser caching to reuse range requests.
The proxy itself stores no response cache. Funnel targets port 8001.
