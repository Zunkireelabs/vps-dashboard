#!/usr/bin/env python3
"""
VPS dashboard — one page showing every website and app on a server.

    python3 build.py            # one check -> out/dashboard.html
    python3 build.py --open     # ...and opens it
    (see serve.py for the live version)

Read-only. One SSH call collects the inventory (nothing is installed or left running on the
server), then every URL is checked from here, the way a visitor sees it (public DNS + HTTPS + certificate).
Settings come from a config file — see config.example.toml.
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import CFG  # noqa: E402
from catalog import CATALOG  # noqa: E402
from history import History  # noqa: E402

VPS_IP = CFG["server"]["ip"]
SSH = ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", CFG["server"]["ssh"]]
CONTROL_HOST = f"never-existed-dashboard-control.{CFG['server']['control_domain']}"
TRAEFIK_404 = "404 page not found\n"
OUT = Path(os.environ.get("DASH_OUT") or Path(__file__).parent / "out") / "dashboard.html"
STATE = OUT.parent / "state.json"   # last status per item + recent changes (survives restarts)

# ─────────────────────────────────────────────────────────────────────────
# 1. Collect — runs on the server (python3 via stdin). Read-only. Settings are injected as SC.
# ─────────────────────────────────────────────────────────────────────────
REMOTE = r'''
import json, os, re, subprocess, glob, pwd
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout
def hosts_in(rule):
    hs = set()
    for m in re.finditer(r"Host\(([^)]*)\)", rule):
        hs.update(re.findall(r"`([^`]+)`", m.group(1)))
    return hs
out = {"blind": []}
out["whoami"] = sh("whoami").strip()

cons = []
ids = sh("docker ps -aq").split()
if ids:
    for c in json.loads(sh("docker inspect " + " ".join(ids))):
        lab = c["Config"]["Labels"] or {}
        hs = set(); ph = set()
        for k, v in lab.items():
            if k.startswith("traefik.http.routers.") and k.endswith(".rule"):
                hs |= hosts_in(v)
                if "Path" in v: ph |= hosts_in(v)
        st = c["State"]
        cons.append(dict(
            name=c["Name"].lstrip("/"), state=st["Status"],
            health=(st.get("Health") or {}).get("Status", ""),
            exit=st.get("ExitCode"), oom=st.get("OOMKilled"),
            started=st.get("StartedAt"), finished=st.get("FinishedAt"), restarts=c.get("RestartCount", 0),
            workdir=lab.get("com.docker.compose.project.working_dir", ""),
            image=c["Config"]["Image"], hosts=sorted(hs), pathhosts=sorted(ph),
            published=sorted({int(x["HostPort"]) for v in (c["NetworkSettings"].get("Ports") or {}).values() if v for x in v})))
def tomb(x):
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", x.strip(), re.I)
    if not m: return None
    f = {"b": 1/1048576, "kib": 1/1024, "kb": 0.9537/1000, "mib": 1, "mb": 0.9537, "gib": 1024, "gb": 953.7, "tib": 1048576}
    return float(m.group(1)) * f.get(m.group(2).lower(), 1)
stats = {}
for line in sh("timeout 40 docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}'").splitlines():
    q = line.split("|")
    if len(q) == 3:
        mu = q[1].split("/")
        lim = tomb(mu[1]) if len(mu) > 1 else None
        stats[q[0]] = dict(mem=tomb(mu[0]), lim=(lim if lim and lim < 6000 else None),
                           cpu=float(q[2].rstrip("%") or 0))
for c in cons:
    st_ = stats.get(c["name"], {})
    c["mem_mb"] = st_.get("mem"); c["lim_mb"] = st_.get("lim"); c["cpu"] = st_.get("cpu")
if not stats:
    out["blind"].append("Per-container memory (docker stats returned nothing)")
out["containers"] = cons

routes = []
for f in (sorted(glob.glob(os.path.join(SC["traefik_dynamic_dir"], "*.y*ml"))) if SC["traefik_dynamic_dir"] else []):
    try:
        t = open(f).read()
    except Exception:
        out["blind"].append("Traefik route file " + f); continue
    routes.append(dict(file=os.path.basename(f).rsplit(".", 1)[0],
                       hosts=sorted(hosts_in(t)),
                       backends=re.findall(r'url:\s*"?([^"\s]+)', t)))
out["routes"] = routes
if SC["traefik_static"] and not os.access(SC["traefik_static"], os.R_OK):
    out["blind"].append("Traefik main config (" + SC["traefik_static"] + ") — not readable as " + out["whoami"])

procs = []
for p in os.listdir("/proc"):
    if not p.isdigit(): continue
    try:
        cg = open(f"/proc/{p}/cgroup").read()
        if "docker" in cg or "containerd" in cg: continue
        cmd = open(f"/proc/{p}/cmdline").read().replace("\0", " ").strip()
        user = pwd.getpwuid(os.stat(f"/proc/{p}").st_uid).pw_name
    except Exception:
        continue
    if not re.search(r"\b(" + "|".join(map(re.escape, SC["process_patterns"])) + r")\b", cmd): continue
    try: rss = int(re.search(r"VmRSS:\s+(\d+)", open(f"/proc/{p}/status").read()).group(1)) / 1024
    except Exception: rss = 0
    procs.append(dict(pid=int(p), user=user, cmd=cmd[:200], rss_mb=round(rss, 1),
                      start=sh(f"ps -o lstart= -p {p}").strip()))
out["procs"] = procs

CONT = re.compile(r"docker-[0-9a-f]{12}|/docker/[0-9a-f]{12}")
def rss_of(pred):
    tot = 0
    for p in os.listdir("/proc"):
        if not p.isdigit(): continue
        try:
            if CONT.search(open(f"/proc/{p}/cgroup").read()): continue   # inside a container: already counted
            comm = open(f"/proc/{p}/comm").read().strip()
            args = open(f"/proc/{p}/cmdline").read().replace("\0", " ")
            if pred(comm, args):
                tot += int(re.search(r"VmRSS:\s+(\d+)", open(f"/proc/{p}/status").read()).group(1))
        except Exception:
            continue
    return round(tot / 1024, 1)
def active(u): return sh("systemctl is-active " + u).strip() or "unknown"
SVC_MEM = {"postgresql": lambda c, a: c == "postgres", "redis": lambda c, a: c == "redis-server",
           "fail2ban": lambda c, a: "fail2ban-server" in a, "docker": lambda c, a: c in ("dockerd", "containerd")}
out["svcs"] = {name: dict(state=active(unit), mem=rss_of(SVC_MEM[name]) if name in SVC_MEM else 0)
               for name, unit in SC["services"].items()}
ports = {}
for line in sh("ss -tlnpH").splitlines():
    f = line.split()
    if len(f) < 4: continue
    host, _, port = f[3].rpartition(":")
    host = host.strip("[]")
    if host.startswith("127.") or host == "::1" or "%lo" in host: continue
    m = re.search(r'users:\(\("([^"]+)"', line)
    ports.setdefault(int(port), dict(port=int(port), host=host, owner=m.group(1) if m else ""))
out["ports"] = sorted(ports.values(), key=lambda x: x["port"])
def cannot_enter(b):
    """True when `b` can't be looked at because a parent folder is closed to this user (as opposed to simply not existing)."""
    p = os.path.dirname(b)
    while p not in ("", "/"):
        if os.path.isdir(p):
            return not os.access(p, os.X_OK)
        p = os.path.dirname(p)
    return False
for d in SC["pm2_dirs"]:
    if (os.path.isdir(d) and not os.access(d, os.R_OK)) or (not os.path.isdir(d) and cannot_enter(d)):
        out["blind"].append(f"PM2 app list at {d} — can see the processes, not their settings")

bases = SC["project_dirs"]
folders = []
for b in bases:
    if not os.path.isdir(b):
        if cannot_enter(b):
            out["blind"].append("Folder " + b + " — cannot be checked as " + out["whoami"])
        continue
    try:
        names = sorted(os.listdir(b))
    except Exception:
        out["blind"].append("Folder " + b + " — not readable as " + out["whoami"]); continue
    for n in names:
        d = os.path.join(b, n)
        if not os.path.isdir(d) or d in bases: continue
        try: files = set(os.listdir(d))
        except Exception: files = set()
        kind = []
        if files & {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "Dockerfile"}: kind.append("Docker")
        if "package.json" in files: kind.append("Node")
        if files & {"requirements.txt", "pyproject.toml"}: kind.append("Python")
        folders.append(dict(path=d, kind=kind, mtime=int(os.stat(d).st_mtime)))
out["folders"] = folders

df = sh("df -P / | tail -1").split()
mem = sh("free -m | grep Mem:").split()
swp = sh("free -m | grep Swap:").split()
out["health"] = dict(disk_pct=int(df[4].rstrip("%")), disk_free_gb=round(int(df[3]) / 1048576, 1),
                     mem_total=int(mem[1]), mem_avail=int(mem[6]),
                     swap_used=int(swp[2]), swap_total=int(swp[1]),
                     load=open("/proc/loadavg").read().split()[:3],
                     cpus=int(sh("nproc").strip() or 0), uptime=sh("uptime -p").strip())
print(json.dumps(out))
'''


def collect():
    cmd = ["python3", "-"] if os.environ.get("VPS_LOCAL") else SSH + ["python3", "-"]
    script = "SC = " + repr(dict(CFG["scan"], services=CFG["services"])) + "\n" + REMOTE
    r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("could not read the server: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1][:160])
    return json.loads(r.stdout)


# ─────────────────────────────────────────────────────────────────────────
# 2. Check every URL from outside, like a visitor.
# ─────────────────────────────────────────────────────────────────────────
def run(cmd, timeout=25):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def dns(host):
    r = run(["dig", "+time=4", "+tries=2", "@1.1.1.1", "A", host])
    if not r:
        return "TIMEOUT", []
    m = re.search(r"status: (\w+)", r.stdout)
    ips = re.findall(r"\sIN\s+A\s+([\d.]+)", r.stdout)
    return (m.group(1) if m else "ERROR"), ips


def curl(host, resolve_to=None):
    cmd = ["curl", "-s", "-m", "15", "-o", "-", "-w", "\n__META__%{http_code} %{time_total}"]
    if resolve_to:
        cmd += ["--resolve", f"{host}:443:{resolve_to}"]
    r = run(cmd + [f"https://{host}/"])
    if r is None:
        return dict(code=0, secs=None, err="timed out", body="")
    body, _, meta = r.stdout.rpartition("\n__META__")
    code, secs = (meta.split() + ["0", "0"])[:2]
    err = {0: "", 6: "domain does not resolve", 7: "connection refused", 28: "timed out",
           35: "TLS handshake failed", 51: "certificate name mismatch",
           60: "certificate invalid or expired"}.get(r.returncode, f"curl error {r.returncode}")
    return dict(code=int(code), secs=float(secs), err=err, body=body[:200])


def cert_expiry(host):
    r = run(["bash", "-c", f"echo | openssl s_client -servername {host} -connect {VPS_IP}:443 2>/dev/null"
                           " | openssl x509 -noout -enddate -issuer 2>/dev/null"])
    if not r or "notAfter=" not in r.stdout:
        return None, ""
    end = re.search(r"notAfter=(.+)", r.stdout).group(1).strip()
    issuer = "Let's Encrypt" if "Let's Encrypt" in r.stdout else ("Traefik default" if "TRAEFIK" in r.stdout else "other")
    try:
        d = dt.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z")
        return (d - dt.datetime.now(dt.UTC).replace(tzinfo=None)).days, issuer
    except ValueError:
        return None, issuer


def classify_http(r, cert_days):
    """Turn one HTTPS answer into (level, plain reason). Shared by the full check and the fast probe."""
    c = r["code"]
    if r["err"]:
        return "red", r["err"].capitalize()
    if c == 0:
        return "red", "No answer"
    if c == 404 and r["body"] == TRAEFIK_404:
        return "red", "Nothing is answering at this address"
    if c >= 500:
        return "red", f"Server error {c}"
    if c == 404:
        return "amber", "App answers, but its homepage returns 404 (normal for APIs/webhooks)"
    why = {401: "Password protected", 405: "API — homepage not used"}.get(c, "")
    if cert_days is not None and cert_days < 14:
        return "amber", f"Certificate expires in {cert_days} days"
    if r["secs"] and r["secs"] > 4:
        why = (why + "; " if why else "") + f"slow ({r['secs']:.1f}s)"
    return "green", why


def fast_probe(host, base, strikes):
    """Cheap once-a-minute check (one HTTPS request, no DNS or certificate work).
    Returns (ok_for_uptime, updates_for_the_check_or_None, new_strike_count).
    A site is only marked Down after 2 failures in a row, so one dropped request is not an outage."""
    if not base.get("reach", True):
        return False, None, 0                     # visitors cannot reach it (DNS) — the full check owns that status
    r = curl(host, VPS_IP)
    level, why = classify_http(r, base.get("cert_days"))
    upd = dict(code=r["code"], secs=r["secs"], level=level, why=why)
    if level == "red":
        if strikes + 1 < 2:
            return False, None, strikes + 1       # first failure: remember it, don't alarm yet
        return False, upd, strikes + 1
    return True, upd, 0


def check(host):
    status, ips = dns(host)
    res = dict(host=host, dns=status, ips=ips)
    days, issuer = cert_expiry(host)
    res.update(cert_days=days, issuer=issuer)
    if status != "NOERROR" or not ips:
        if host.startswith("www."):
            res.update(level="amber", code=0, secs=None, reach=False,
                       why=f"www. alias has no DNS record ({status}) — the main address is checked separately")
            return res
        srv = curl(host, VPS_IP)  # does the server itself serve it?
        res.update(level="red", code=srv["code"], secs=srv["secs"], reach=False,
                   why=f"Domain not resolving for visitors (DNS {status})"
                       + (" — the server itself still serves it" if 200 <= srv["code"] < 500 and srv["body"] != TRAEFIK_404 else ""))
        return res
    if VPS_IP not in ips:
        pub = curl(host)
        res.update(level="red", code=pub["code"], secs=pub["secs"], reach=False,
                   why=f"Domain points to another server ({', '.join(ips)})" + (f" — {pub['err']}" if pub["err"] else ""))
        return res
    r = curl(host, VPS_IP)
    res.update(code=r["code"], secs=r["secs"], reach=True)
    res["level"], res["why"] = classify_http(r, days)
    return res


# ─────────────────────────────────────────────────────────────────────────
# 3. Join everything into rows.
# ─────────────────────────────────────────────────────────────────────────
def iso_epoch(iso):
    if not iso or iso.startswith("0001"):
        return None
    return dt.datetime.fromisoformat(iso[:19]).replace(tzinfo=dt.timezone.utc).timestamp()


def since(iso):
    if not iso or iso.startswith("0001"):
        return ""
    t = dt.datetime.fromisoformat(iso[:19])
    s = (dt.datetime.now(dt.UTC).replace(tzinfo=None) - t).total_seconds()
    for unit, n in (("month", 2592000), ("week", 604800), ("day", 86400), ("hour", 3600), ("min", 60)):
        if s >= n:
            v = int(s // n)
            return f"{v} {unit}{'s' if v > 1 else ''}"
    return "just now"


RANK = {"red": 0, "amber": 1, "gray": 2, "green": 3}


def build_rows(inv, checks):
    rows, seen = [], set()
    procs = inv["procs"]

    def proc_for(match):
        return next((p for p in procs if match and match in p["cmd"]), None)

    def rss_for(match):
        v = [p["rss_mb"] for p in procs if match and match in p["cmd"]]
        return round(sum(v), 1) if v else None

    def age_s(iso):
        if not iso or iso.startswith("0001"):
            return None
        return (dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.datetime.fromisoformat(iso[:19])).total_seconds()

    for c in inv["containers"]:
        meta = CATALOG.get(c["name"])
        seen.add(c["name"])
        row = dict(key=c["name"], meta=meta, folder=c["workdir"] or "— started by hand (docker run), no folder",
                   runtime="Docker", hosts=c["hosts"], extra=[],
                   mem_mb=c.get("mem_mb"), lim_mb=c.get("lim_mb"), cpu=c.get("cpu"), uptime_s=None)
        if c["state"] == "running":
            row["uptime"] = since(c["started"])
            row["uptime_s"] = age_s(c["started"])
            if c["health"] == "unhealthy":
                row.update(level="red", why="Container is running but unhealthy")
        else:
            why = f"Stopped {since(c['finished'])} ago (exit code {c['exit']}"
            why += ", out of memory)" if c["oom"] or c["exit"] == 137 else ")"
            row.update(level="red" if c["exit"] not in (0, None) else "gray", why=why, uptime="",
                       since_hint=iso_epoch(c["finished"]))
        rows.append(row)

    for r in inv["routes"]:
        key = "route:" + r["file"]
        meta = CATALOG.get(key)
        seen.add(key)
        p = proc_for(meta and meta["match"])
        row = dict(key=key, meta=meta, folder=(meta or {}).get("folder") or "?",
                   runtime=f"Host process ({p['user']})" if p else "Host process",
                   hosts=r["hosts"], uptime="", extra=[f"Traefik file route → {', '.join(r['backends'])}"],
                   mem_mb=rss_for(meta and meta["match"]), lim_mb=None, cpu=None, uptime_s=None)
        if meta and meta["match"] and not p:
            row.update(level="red", why="The process behind this route is not running")
        rows.append(row)

    for key, meta in CATALOG.items():
        if key in seen:
            continue
        if key.startswith("svc:"):
            svc = inv.get("svcs", {}).get(key[4:])
            if svc is None:
                continue
            row = dict(key=key, meta=meta, folder="System service", hosts=[], uptime="", runtime="System service",
                       extra=[], mem_mb=svc["mem"] or None, lim_mb=None, cpu=None, uptime_s=None)
            if svc["state"] != "active":
                row.update(level="red", why=f"Service not running ({svc['state']})")
            rows.append(row)
            continue
        if key.startswith("proc:"):
            p = proc_for(meta["match"])
            rows.append(dict(key=key, meta=meta, folder=meta["folder"], hosts=[], uptime="",
                             runtime=f"Host process ({p['user']})" if p else "Host process", extra=[],
                             mem_mb=rss_for(meta["match"]), lim_mb=None, cpu=None, uptime_s=None,
                             **({} if p else dict(level="red", why="Process not running"))))
            if p:
                p["claimed"] = True
        else:
            rows.append(dict(key=key, meta=meta, folder="—", runtime="—", hosts=meta["hosts"], uptime="",
                             extra=[], level="red", why="Missing — expected on the server but not found",
                             mem_mb=None, lim_mb=None, cpu=None, uptime_s=None))

    for row in rows:
        row["checks"] = [checks[h] for h in row["hosts"] if h in checks]
        if "level" not in row:
            worst = min((ch["level"] for ch in row["checks"]), key=RANK.get, default="green")
            row["level"] = worst
            row["why"] = next((ch["why"] for ch in row["checks"] if ch["level"] == worst and ch["why"]), "")
            if not row["hosts"] and row["runtime"] != "—":
                row["why"] = row["why"] or "Running — internal, no public URL"
        row["cert_days"] = min((ch["cert_days"] for ch in row["checks"] if ch["cert_days"] is not None), default=None)

    # host processes not tied to anything in the catalog
    claimed = [m["match"] for m in CATALOG.values() if m["match"]]
    loose = [p for p in procs if not any(m in p["cmd"] for m in claimed)
             and not p["cmd"].startswith("postgres:")]
    return rows, loose


def classify_ports(inv):
    names = {}
    for r in inv["routes"]:
        for b in r["backends"]:
            m = re.search(r":(\d+)(?:/|$)", b)
            if m:
                names[int(m.group(1))] = (CATALOG.get("route:" + r["file"]) or {}).get("name", r["file"]) + " (Traefik route)"
    pub = {p: f"Container {c['name']}" for c in inv["containers"] for p in c.get("published", [])}
    known = {22: "SSH (remote login)", 80: "Traefik — web traffic", 443: "Traefik — secure web traffic"}
    if inv.get("svcs", {}).get("cups", {}).get("state") == "active":
        known[631] = "CUPS printing service"
    return [dict(port=p["port"], host=p["host"], owner=p["owner"],
                 what=known.get(p["port"]) or names.get(p["port"]) or pub.get(p["port"]) or "")
            for p in inv.get("ports", [])]


def attention(rows, inv, unused, loose, control):
    items = []
    if control.get("level") != "red":
        items.append(("red", "Checks may be unreliable", "A made-up hostname did NOT return 'nothing answering' — status colours can't be trusted this run."))
    owners = {}
    pathscoped = {h for c in inv["containers"] for h in c.get("pathhosts", [])}
    for r in rows:
        if r["runtime"] == "Docker" and r.get("level") != "gray":
            for h in r["hosts"]:
                if h not in pathscoped:
                    owners.setdefault(h, []).append(r["key"])
    for h, ks in owners.items():
        if len(ks) > 1:
            items.append(("amber", h, f"Claimed by {len(ks)} apps ({', '.join(ks)}) — check which one actually gets the traffic"))
    for r in sorted(rows, key=lambda r: (r["meta"] or {}).get("env", "z") != "prod"):
        if r["level"] == "red":
            name = r["meta"]["name"] if r["meta"] else r["key"]
            env = (r["meta"] or {}).get("env", "?")
            items.append(("red", f"{name} ({'production' if env == 'prod' else 'dev/stage'})", r["why"]))
    for r in rows:
        if not r["meta"]:
            items.append(("amber", r["key"], "New / unclassified — add it to the catalog file"))
    for p in loose:
        items.append(("amber", f"Host process {p['pid']} ({p['user']})", f"Not linked to any site: {p['cmd'][:80]}"))
    for p in inv.get("ports_info", []):
        if not p["what"]:
            items.append(("amber", f"Port {p['port']} is open and nothing explains it",
                          "Something on the server listens on this port. It is not a known site or service. "
                          "Finding the owner needs root access. Whether the firewall blocks it from outside was not checked."))
    return items


from render import LABEL, render  # noqa: E402


# ── history, status tracking, events ─────────────────────────────────────
_hist = None


def hist():
    """The history store (created on first use). Set DASH_DB to use another file, e.g. for tests."""
    global _hist
    if _hist is None:
        _hist = History(os.environ.get("DASH_DB") or OUT.parent / "history.db",
                        int(os.environ.get("DASH_RETENTION_DAYS", 14)), int(os.environ.get("DASH_MAX_DB_MB", 100)))
        if not _hist.meta("started"):                       # one-time import of the earlier state.json
            try:
                st = json.loads(STATE.read_text())
                _hist.save_levels({k: dict(level=v["level"], since=v["since"], known=v.get("known", False),
                                           name=v.get("name", k), env=v.get("env", "?")) for k, v in st.get("levels", {}).items()})
                if st.get("levels"):
                    _hist.set_meta("started", st.get("started") or time.time())
            except Exception:
                pass
    return _hist


def dur(sec):
    m = int(sec // 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} days"


def track(h, rows, now):
    """Compare every item with last time; record changes in the timeline; set each row's 'since'."""
    levels = h.levels()
    started = float(h.meta("started") or 0)
    baseline = not started
    if baseline:
        h.set_meta("started", now)
    new = {}
    for r in rows:
        k, m = r["key"], r["meta"] or {}
        name, env, hint, prev = m.get("name", k), m.get("env", "?"), r.get("since_hint"), levels.get(k)
        if prev is None:
            new[k] = dict(level=r["level"], since=hint or now, known=bool(hint) or not baseline, name=name, env=env)
            if not baseline:
                h.add_event(now, "new", k, name, env, None, r["level"])
        elif prev["level"] != r["level"]:
            since = hint if (hint and r["level"] in ("red", "gray")) else now
            new[k] = dict(level=r["level"], since=since, known=True, name=name, env=env)
            note = f"was {LABEL[prev['level']]} for {dur(now - prev['since'])}" if r["level"] == "green" and prev["level"] != "green" else ""
            h.add_event(now, "status", k, name, env, prev["level"], r["level"], note)
        else:
            new[k] = dict(prev, name=name, env=env)
            if hint and not prev["known"] and r["level"] in ("red", "gray"):
                new[k].update(since=hint, known=True)       # learned the real stop time from Docker
        r["since"], r["since_known"] = new[k]["since"], new[k]["known"]
    for k, prev in levels.items():
        if k not in new:
            h.add_event(now, "removed", k, prev["name"], prev["env"], prev["level"], None)
    h.save_levels(new)


def watch(h, now, flag, key, name, env, is_on, is_off, on_msg, off_msg, need=1, quiet=False):
    """Raise a timeline event when a number crosses a line, and another when it comes back (with a gap, so it can't flap)."""
    f = h.flag(flag)
    if not f["on"]:
        if is_on:
            if f["count"] + 1 >= need:
                h.set_flag(flag, True, 0, now)
                if not quiet:
                    h.add_event(now, "warn", key, name, env, detail=on_msg)
            else:
                h.set_flag(flag, False, f["count"] + 1, now)
        elif f["count"]:
            h.set_flag(flag, False, 0, now)
    elif is_off:
        h.set_flag(flag, False, 0, now)
        h.add_event(now, "ok", key, name, env, detail=off_msg)


def detect(h, inv, rows, now, quiet=False):
    """Events for things that status alone misses: restarts / out-of-memory, RAM near its limit, high CPU, disk and memory."""
    mb = lambda v: f"{v / 1024:.1f} GB" if v >= 1024 else f"{v:.0f} MB"
    last = h.last_restarts()
    for c in inv["containers"]:
        prev, n = last.get(c["name"]), c.get("restarts", 0)
        if prev is not None and n > prev:
            m = CATALOG.get(c["name"]) or {}
            h.add_event(now, "restart", c["name"], m.get("name", c["name"]), m.get("env", "?"),
                        detail=f"restarted {n - prev} time{'s' if n - prev > 1 else ''}" + (" — it ran out of memory" if c.get("oom") else ""))
    for r in rows:
        m = r["meta"] or {}
        name, env, k = m.get("name", r["key"]), m.get("env", "?"), r["key"]
        if r.get("mem_mb") and r.get("lim_mb"):
            pct = r["mem_mb"] / r["lim_mb"] * 100
            watch(h, now, f"ram:{k}", k, name, env, pct >= 85, pct < 80,
                  f"memory at {pct:.0f}% of its limit ({mb(r['mem_mb'])} of {mb(r['lim_mb'])})", f"memory back to normal ({pct:.0f}%)", quiet=quiet)
        if r.get("cpu") is not None:
            watch(h, now, f"cpu:{k}", k, name, env, r["cpu"] >= 100, r["cpu"] < 70,
                  f"using a lot of CPU ({r['cpu']:.0f}% of one core) for 3 checks in a row", f"CPU back to normal ({r['cpu']:.0f}%)", need=3, quiet=quiet)
    hl = inv["health"]
    mp = (hl["mem_total"] - hl["mem_avail"]) / hl["mem_total"] * 100
    watch(h, now, "host:mem", "host", "Server memory", "prod", mp >= 90, mp < 85,
          f"server memory is {mp:.0f}% used", f"server memory back to normal ({mp:.0f}%)", quiet=quiet)
    pct, free = hl["disk_pct"], hl["disk_free_gb"]
    f = h.flag("host:disk")
    old = f["count"] if f["on"] else 0                       # highest line already crossed (85 / 90 / 95)
    cur = max([lv for lv in (85, 90, 95) if pct >= lv], default=0)
    if cur > old:
        h.set_flag("host:disk", True, cur, now)
        if not quiet:
            h.add_event(now, "warn", "host", "Server disk", "prod", detail=f"disk passed {cur}% — now {pct}% full ({free} GB left)")
    elif old and pct < old - 2:
        h.set_flag("host:disk", cur > 0, cur, now)
        h.add_event(now, "ok", "host", "Server disk", "prod", detail=f"disk back under {old}% ({pct}% full, {free} GB left)")
    h.add_samples(now, [(r["key"], r.get("mem_mb"), r.get("cpu"), next((c.get("restarts", 0) for c in inv["containers"] if c["name"] == r["key"]), None))
                        for r in rows],
                  dict(mem_used=hl["mem_total"] - hl["mem_avail"], mem_total=hl["mem_total"], swap_used=hl["swap_used"],
                       disk_pct=hl["disk_pct"], disk_free_gb=hl["disk_free_gb"], load1=float(hl["load"][0])))


def disk_eta(h):
    pts = h.host_series(72)
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 12 * 3600:
        return None
    span_days = (pts[-1][0] - pts[0][0]) / 86400
    slope = (pts[-1][1] - pts[0][1]) / span_days          # GB per day (negative = shrinking)
    hours = round((pts[-1][0] - pts[0][0]) / 3600)
    if slope < -0.05:
        return dict(hours=hours, days=pts[-1][1] / -slope)
    return dict(hours=hours, days=None)


# ── one slow scan + a fast re-assemble ───────────────────────────────────
def scan():
    """Slow part (~20 s): read the server over SSH and test every URL, with DNS and certificates."""
    inv = collect()
    hosts = set(CONTROL_HOST.split())
    for c in inv["containers"]:
        hosts.update(c["hosts"])
    for r in inv["routes"]:
        hosts.update(r["hosts"])
    for m in CATALOG.values():
        hosts.update(m["hosts"])
    with ThreadPoolExecutor(12) as ex:
        checks = {r["host"]: r for r in ex.map(check, sorted(hosts))}
    control = checks.pop(CONTROL_HOST)
    return dict(inv=inv, checks=checks, control=control, at=time.time())


def assemble(snap, record, ctx=None):
    """Fast part: turn a scan (plus any newer probe results) into rows, events and the page.
    record=True only after a real scan — that is when the numbers are saved to history."""
    h, inv = hist(), snap["inv"]
    rows, loose = build_rows(inv, snap["checks"])
    inv["ports_info"] = classify_ports(inv)
    used = [c["workdir"] for c in inv["containers"] if c["workdir"]] + \
           [m["folder"] for m in CATALOG.values() if m["folder"].startswith("/")]
    unused = [f for f in inv["folders"]
              if not any(u == f["path"] or u.startswith(f["path"] + "/") for u in used)]
    items = attention(rows, inv, unused, loose, snap["control"])
    now = time.time()
    if record:
        detect(h, inv, rows, now, quiet=not h.meta("seeded"))   # first ever record: existing problems are not "new" events
        h.set_meta("seeded", 1)
        h.prune()
    track(h, rows, now)
    hosts = {x for r in rows for x in r["hosts"]}
    ctx = dict(ctx or {})
    extra = dict(generated=now, probe_at=ctx.get("probe_at", snap["at"]), inv_at=snap["at"], version=ctx.get("version", int(now * 1000)),
                 stale_probe=ctx.get("stale_probe", 600), stale_inv=ctx.get("stale_inv", 600),
                 events=h.recent_events(12), started=float(h.meta("started") or now),
                 uptime={x: h.uptime(x) for x in hosts}, series=h.series(24), eta=disk_eta(h))
    when = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    page = render(inv, rows, unused, loose, items, snap["control"], when, extra)
    counts = {k: sum(1 for r in rows if r["level"] == k) for k in LABEL}
    return dict(html=page, counts=counts, n=len(rows), generated=now)


def cycle(interval=None):
    """One full check (scan + page), used by the one-off command."""
    t0 = time.time()
    snap = scan()
    res = assemble(snap, True)
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(res["html"])
    os.replace(tmp, OUT)
    return dict(res, seconds=round(time.time() - t0, 1))


def main():
    print("Checking the server…")
    try:
        res = cycle()
    except Exception as e:
        sys.exit(f"Check failed: {e}")
    c = res["counts"]
    print(f"Done in {res['seconds']}s: {res['n']} items — {c['green']} live, {c['amber']} check, {c['red']} down, {c['gray']} stopped")
    print(f"→ {OUT}")
    if "--open" in sys.argv:
        webbrowser.open(OUT.as_uri())


if __name__ == "__main__":
    main()
