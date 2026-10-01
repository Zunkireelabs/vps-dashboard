# Running it for a team

A saved `out/dashboard.html` is only a **snapshot** — it does not update. For a live page the team can open,
`serve.py` must be running somewhere and the team opens its address.

## Option 1 — on your own machine
`python3 serve.py` (reads the server over SSH). Only you can see it, and only while it runs.

## Option 2 — on the server itself, for the whole team
Run `serve.py` with `VPS_LOCAL=1` behind your reverse proxy, with a login in front of it.

What to plan for:
- **Login.** Use single sign-on at the proxy, or at minimum `DASH_PASSWORD` over HTTPS.
- **Docker access is root-equivalent.** The collector reads `docker inspect` / `docker stats`. Prefer a read-only
  Docker socket proxy that only allows `GET /containers/*` and `/info`, and review what the collector runs
  (it is the `REMOTE` script in `build.py`).
- **Host details need host access.** Listening ports, host processes and system services are read from the host.
  A container only sees them with `pid: host` / `network_mode: host`, which widens its access — or run the
  collector as a small host service and let the web page read its output.
- **Give it a memory cap and a healthcheck.** `GET /healthz` returns 200 only while the data is fresh.
- **Disk.** The history database is size-capped (`DASH_MAX_DB_MB`), but check the server has room first.
- **Needs `dig`, `curl`, `openssl`** in whatever image or host it runs on.
- **Deploy it like your other services** (CI/CD, pinned image) and never edit it by hand on the server.

## Option 3 — independent outside monitor
A dashboard hosted on the server it watches goes dark when that server does. Pair it with a small monitor on a
different machine or a hosted uptime service that only asks "can the world reach our sites?" and sends alerts.
