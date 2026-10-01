# vps-dashboard

One page that shows **every website and app on a server** — is it up, what it's using, where it lives —
and keeps itself up to date. Built for small teams running many projects on one Docker + Traefik VPS.

- **Status the way a visitor sees it:** every URL is tested from outside (public DNS + HTTPS + certificate),
  not just "is the container running".
- **One row per project**, Production and Dev side by side: folder, address, status, RAM.
- **Updates itself:** a quick site check every minute (a site is only marked *Down* after two failures in a row)
  and a full scan every few minutes. New data is swapped in without reloading the page.
- **Remembers what happened:** timeline of status changes (with how long things were down), restarts and
  out-of-memory kills, RAM near its limit, high CPU, disk and memory lines crossed; uptime %, RAM trend per
  item, and a rough "days until the disk is full".
- **Read-only.** It never changes anything on your server. Standard library only (Python 3.11+).
- **Safe to forget an entry:** anything found on the server that you haven't named still appears, in an
  *Unclassified* section. Anything the checker can't see is listed under *What this check could not see*.

## Quick start

Needs Python 3.11+, the `dig`, `curl` and `openssl` commands, and SSH access to the server (or run it on the
server itself with `VPS_LOCAL=1`).

```bash
cp config.example.toml config.toml        # your server's address, folders, service names
cp catalog.example.toml catalog.toml      # what your projects are called (optional at first)
python3 build.py --open                   # one check -> out/dashboard.html
python3 serve.py                          # live: http://127.0.0.1:8765, updates itself
```

`config.toml` and `catalog.toml` are git-ignored — **keep your real values out of any public repo**
(or point `DASH_CONFIG` at a file stored elsewhere).

## Options

```bash
python3 serve.py --interval 180 --fast 60 --port 8765
```
| | |
|---|---|
| `--interval` | seconds between full scans (default 180, min 60): containers, RAM, disk, ports, DNS, certificates |
| `--fast` | seconds between quick site checks (default 60, min 20). Adds ~1 request per minute to each site's own logs |
| `DASH_CONFIG` | path to your config file |
| `DASH_DB` / `DASH_OUT` | where the history database / generated page go (default `./out`) |
| `DASH_RETENTION_DAYS` / `DASH_MAX_DB_MB` | history kept (14 days) and hard size cap (100 MB) |
| `DASH_PASSWORD` / `DASH_USER` | password protection (user defaults to `team`) |
| `VPS_LOCAL=1` | read the server directly instead of over SSH (when running on the server) |

## Security — read this

The page lists your whole infrastructure: project names, folders, ports, what is down. Treat it as private.

- `serve.py` listens on `127.0.0.1` only. It **refuses to listen on any other address without `DASH_PASSWORD`**.
- Basic auth over plain HTTP sends the password in clear. If it is reachable by anyone else, put it behind
  HTTPS (a reverse proxy) — or better, your own single sign-on.
- Reading containers needs access to the Docker socket, which is **root-equivalent**. See [docs/HOSTING.md](docs/HOSTING.md).
- Never commit `config.toml`, `catalog.toml`, `out/` or the history database.

## How it stays honest

- A made-up hostname is tested on every full scan. If it is ever *not* reported broken, the page says its colours
  can't be trusted (a reverse proxy can answer every hostname, so a bare "200" proves nothing).
- A site visitors can't reach (broken DNS) counts as **down** in uptime, even if the server itself answers.
- If a scan fails the last good data stays on screen with a banner; if the data gets old a red banner says so.

## Limits

- It tests each site's **homepage** only. A site can load its homepage and still be broken elsewhere.
- Checks run every minute; a problem shorter than that can be missed. If the dashboard runs on the same server it
  monitors, it goes down with it — pair it with an independent outside monitor.
- Trends and uptime need hours to days of data before they mean much.
- Things the SSH user can't read (other users' folders, root-only files) can't be checked; they are listed as blind spots.

## Tests

```bash
python3 -m unittest discover -s tests
```
No server or network needed.

## License

MIT — see [LICENSE](LICENSE).
