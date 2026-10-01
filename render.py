"""HTML rendering for the VPS dashboard — one simple page, one row per project."""
import datetime as dt
import html

from config import CFG  # noqa: E402

E = html.escape
LABEL = {"green": "Live", "amber": "Check", "red": "Down", "gray": "Stopped"}
RANK = {"red": 0, "amber": 1, "gray": 2, "green": 3}
ICON = {"green": "✓", "amber": "!", "red": "✕", "gray": "–"}  # colour + shape + word
CTX = {}   # extra data for the page being rendered (uptime, trends) — set by render()
SECTIONS = (("web", "Websites", "Client websites"),
            ("app", "Apps", "Products and internal tools"),
            ("infra", "Infrastructure", "Routing, automation and databases"))


# ── small helpers ────────────────────────────────────────────────────────
def short_legend():
    if not CFG["shorthand"]:
        return ""
    items = ", ".join(f"<code>{E(b)}</code> = {E(a)}" for a, b in sorted(CFG["shorthand"].items(), key=lambda kv: -len(kv[0])))
    return f" · Folder shorthand: {items}"


def fmt_mb(v):
    if v is None:
        return "—"
    return f"{v / 1024:.1f} GB" if v >= 1024 else f"{v:.0f} MB"


def short(path):
    if path.startswith("—"):
        return "no folder (started by hand)"
    for a, b in sorted(CFG["shorthand"].items(), key=lambda kv: -len(kv[0])):   # longest prefix first
        if path.startswith(a):
            return b + path[len(a):]
    return path


def pill(level, text=None):
    return f'<span class="pill {level}"><b>{ICON[level]}</b>{E(text or LABEL[level])}</span>'


def reason_short(why, level):
    """One short phrase under the status. Empty when there is nothing worth saying."""
    w = why or ""
    for needle, out in (("Domain not resolving", "domain problem"), ("another server", "wrong server"),
                        ("Missing", "app removed"), ("out of memory", "crashed — out of memory"),
                        ("Nothing is answering", "not answering"), ("ertificate", "certificate problem"),
                        ("www. alias", "www address missing"), ("homepage returns 404", "no homepage (normal for APIs)"),
                        ("Password protected", "password protected"), ("API — homepage", "API only"),
                        ("unhealthy", "unhealthy"), ("not running", "not running")):
        if needle in w:
            return out
    if w.startswith("Stopped"):
        return "stopped on purpose" if level == "gray" else "stopped"
    if "slow" in w:
        return "slow to respond"
    return ""


def reason_plain(desc):
    """Plain-English sentence for the Needs attention list."""
    d = desc or ""
    if "Domain not resolving" in d:
        return "Visitors can't reach it — the domain's DNS settings are broken. The server itself is fine."
    if "another server" in d:
        return "The domain points to a different server, so visitors never reach this one."
    if d.startswith("Missing"):
        return "This app was removed from the server, but its address still points here."
    if "out of memory" in d:
        return "It crashed because it ran out of memory."
    if "Claimed by" in d:
        return "Two apps use this address. Check which one actually gets the traffic."
    if "Nothing is answering" in d:
        return "The address is set up but nothing answers."
    if d.startswith("New / unclassified"):
        return "Found on the server but not named yet (add it to the catalog file)."
    return d


def primary_host(hosts):
    non_www = [h for h in hosts if not h.startswith("www.")] or hosts
    return sorted(non_www, key=lambda h: (len(h), h))[0]


# ── one project row ──────────────────────────────────────────────────────
def env_cell(items):
    if not items:
        return '<td class="env none"><span class="muted">—</span></td>'
    out = []
    for r in items:
        hosts = r["hosts"]
        if hosts:
            p, more = primary_host(hosts), len(hosts) - 1
            url = (f'<a class="u" href="https://{E(p)}" target="_blank" rel="noopener" '
                   f'onclick="event.stopPropagation()">{E(p)}</a>' + (f'<span class="more"> +{more}</span>' if more else ""))
        else:
            url = '<span class="muted">no public address</span>'
        rs = reason_short(r.get("why", ""), r["level"])
        ram = fmt_mb(r.get("mem_mb"))
        out.append(f'<div class="item"><div class="eline">{pill(r["level"])}'
                   + (f'<span class="ram" title="RAM in use">{ram}</span>' if r.get("mem_mb") is not None else "") + f'</div><div class="url">{url}</div>'
                   + (f'<div class="rs">{E(rs)}</div>' if rs else "") + "</div>")
    return f'<td class="env">{"".join(out)}</td>'


class Raw(str):
    """Marks text that is already safe HTML (so it is not escaped again)."""


def fmt_ts(ts):
    """A time that the browser converts to the viewer's own time zone. The plain text (UTC) is the fallback."""
    utc = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%b %d, %H:%M UTC")
    return Raw(f'<time class="lt" data-ts="{ts:.0f}">{utc}</time>')


def changes_html(events, started):
    if not events:
        first = f" at {fmt_ts(started)}" if started else ""
        return (f'<p class="muted small">Nothing has changed since monitoring started{first}. '
                f'Sites going up or down, restarts, and memory, disk or CPU problems will be listed here.</p>')
    out = []
    for e in events:
        tag = "" if e["key"] == "host" else f' <span class="env-t">{"live" if e["env"] == "prod" else "dev" if e["env"] == "dev" else ""}</span>'
        who = f'<strong>{E(e["name"])}</strong>{tag}'
        k, note = e["kind"], (f' <span class="muted small">{E(e["detail"])}</span>' if e.get("detail") else "")
        if k == "status":
            what = f'{pill(e["frm"])} → {pill(e["to"])}{note}'
        elif k == "new":
            what = f'new on the server — {LABEL[e["to"]]}'
        elif k == "removed":
            what = "no longer on the server"
        elif k == "restart":
            what = f'<span class="evt warn">↻ {E(e["detail"])}</span>'
        elif k == "warn":
            what = f'<span class="evt warn">▲ {E(e["detail"])}</span>'
        else:
            what = f'<span class="evt okk">✓ {E(e["detail"])}</span>'
        out.append(f'<li><span class="ts">{fmt_ts(e["ts"])}</span><span>{who}</span><span class="chg">{what}</span></li>')
    return f'<ul class="chlist">{"".join(out)}</ul>'


def spark(vals, w=140, h=28):
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - 2 - (v - lo) / rng * (h - 4):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="RAM over the last 24 hours">'
            f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>')


def hours_text(n):
    return f"{n} h" if n < 48 else f"{n // 24} days"


def cert_text(d):
    return "—" if d is None else ("expired" if d < 0 else f"{d} days left")


def detail_html(items):
    blocks = []
    for r in items:
        m = r["meta"] or {}
        env = {"prod": "Production", "dev": "Dev / Test"}.get(m.get("env"), "Unclassified")
        lim = r.get("lim_mb")
        facts = [("Technical name", r["key"]), ("Runs as", r["runtime"]),
                 ("Status", r.get("why") or LABEL[r["level"]]),
                 ("Status since", Raw(fmt_ts(r["since"]) + ("" if r.get("since_known") else " or earlier (monitoring started then)")) if r.get("since") else "—"),
                 ("RAM", fmt_mb(r.get("mem_mb")) + (f" of {fmt_mb(lim)} limit" if lim else "")),
                 ("CPU", f'{r["cpu"]:.0f}%' if r.get("cpu") is not None else "—"),
                 ("Running for", r.get("uptime") or "—"), ("Certificate", cert_text(r["cert_days"])),
                 ("Folder", r["folder"])]
        if m.get("note"):
            facts.append(("Note", m["note"]))
        facts += [("Routing", x) for x in r["extra"]]
        urls = "".join(
            f'<div><i class="dot {ch["level"]}"></i><a href="https://{E(ch["host"])}" target="_blank" rel="noopener">{E(ch["host"])}</a>'
            f'<span class="muted"> · {ch["code"] or "no answer"}'
            + (f' · {ch["secs"]:.1f}s' if ch.get("secs") and ch["code"] else "") + (f' · {E(ch["why"])}' if ch.get("why") else "")
            + "</span></div>" for ch in r["checks"]) or '<div class="muted">No public address</div>'
        if r["hosts"]:
            u = CTX.get("uptime", {}).get(primary_host(r["hosts"]))
            facts.insert(4, ("Uptime", f"{u[0]:.2f}% over the last {hours_text(u[1])}" if u else "collecting history…"))
        dl = "".join(f"<dt>{E(k)}</dt><dd>{v if isinstance(v, Raw) else E(str(v))}</dd>" for k, v in facts)
        ser = CTX.get("series", {}).get(r["key"])
        trend = ""
        if ser and len(ser) >= 3:
            trend = (f'<div class="trend"><span class="muted">RAM, last 24 h</span>{spark(ser)}'
                     f'<span class="muted small">{fmt_mb(min(ser))} – {fmt_mb(max(ser))}</span></div>')
        blocks.append(f'<div class="dblock"><h4>{env}</h4><div class="durls">{urls}</div><dl>{dl}</dl>{trend}</div>')
    return f'<div class="dgrid">{"".join(blocks)}</div>'


def group(rows, sid):
    d = {}
    for r in rows:
        m = r["meta"]
        if m and m["section"] == sid:
            d.setdefault(m["name"], dict(name=m["name"], prod=[], dev=[]))[m["env"]].append(r)
    return list(d.values())


def project_html(g, show_dev):
    items = g["prod"] + g["dev"]
    rank = min(RANK[r["level"]] for r in items)
    ram = sum(r.get("mem_mb") or 0 for r in items)
    folders = []
    for env, lst in (("prod", g["prod"]), ("dev", g["dev"])):
        for r in lst:
            f = short(r["folder"])
            if f not in [x[0] for x in folders]:
                folders.append((f, env))
    if len(folders) > 1:
        fo = "".join(f'<div class="mono" title="{E(f)}"><span class="ft">{"live" if e == "prod" else "dev"}</span> {E(f)}</div>' for f, e in folders)
    else:
        fo = f'<div class="mono">{E(folders[0][0])}</div>' if folders else ""
    search = " ".join([g["name"], *[r["key"] for r in items], *[h for r in items for h in r["hosts"]], *[r["folder"] for r in items]]).lower()
    cols = 4 if show_dev else 3
    return (f'<tbody class="proj" data-rank="{rank}" data-ram="{ram:.1f}" data-name="{E(g["name"].lower())}" data-search="{E(search)}">'
            f'<tr class="main"><td class="pn"><span class="chev">▸</span><strong>{E(g["name"])}</strong></td><td class="fo">{fo}</td>'
            f'{env_cell(g["prod"])}{env_cell(g["dev"]) if show_dev else ""}</tr>'
            f'<tr class="detail"><td colspan="{cols}">{detail_html(items)}</td></tr></tbody>')


def section_html(sid, title, desc, rows):
    groups = group(rows, sid)
    items = [r for g in groups for r in g["prod"] + g["dev"]]
    if not items:
        return ""
    show_dev = sid != "infra"
    live = sum(r["level"] == "green" for r in items)
    down = sum(r["level"] == "red" for r in items)
    head = (f'<tr><th>Project</th><th>Folder</th><th>{"Production" if show_dev else "Status"}</th>'
            + ("<th>Dev / Test</th>" if show_dev else "") + "</tr>")
    return (f'<section id="{sid}"><div class="sh"><div><h2>{title}</h2><span class="muted">{desc}</span></div>'
            f'<div class="ss"><span><b>{len(groups)}</b> projects</span><span><b class="greent">{live}</b> live</span>'
            + (f'<span><b class="redt">{down}</b> down</span>' if down else "")
            + f'<span><b>{fmt_mb(sum(r.get("mem_mb") or 0 for r in items))}</b> RAM</span></div></div>'
            f'<div class="card tw"><table class="pt{" one" if not show_dev else ""}"><thead>{head}</thead>'
            f'{"".join(project_html(g, show_dev) for g in groups)}</table></div></section>')


# ── top of page ──────────────────────────────────────────────────────────
def gauge(label, pct, text, level, sub=""):
    return (f'<div class="gauge"><div class="gl"><span>{E(label)}</span><b>{E(text)}</b></div>'
            f'<div class="meter"><i class="{level}" style="width:{min(pct, 100):.0f}%"></i></div>'
            + (f'<div class="gsub">{E(sub)}</div>' if sub else "") + "</div>")


def lvl(v, warn, bad):
    return "red" if v >= bad else "amber" if v >= warn else "green"


def top_users(rows, n=8):
    ranked = sorted((r for r in rows if r.get("mem_mb")), key=lambda r: -r["mem_mb"])[:n]
    if not ranked:
        return '<p class="muted">No memory data.</p>'
    top = ranked[0]["mem_mb"]
    return "".join(
        f'<div class="hb"><div class="hn">{E((r["meta"] or {}).get("name", r["key"]))}'
        f'<span class="env-t">{"live" if (r["meta"] or {}).get("env") == "prod" else "dev"}</span></div>'
        f'<div class="hbar"><i style="width:{r["mem_mb"] / top * 100:.1f}%"></i></div><div class="hv">{fmt_mb(r["mem_mb"])}</div></div>'
        for r in ranked)


def render(inv, rows, unused, loose, items, control, when, extra=None):
    extra = extra or {}
    CTX.clear()
    CTX.update(extra)
    h = inv["health"]
    eta = extra.get("eta")
    eta_txt = ("Trend appears after 12 hours of history" if eta is None else
               f"About {eta['days']:.0f} days left at the current rate (rough estimate)" if eta["days"] else
               f"Not shrinking over the last {eta['hours']} h")
    counts = {k: sum(1 for r in rows if r["level"] == k) for k in LABEL}
    used = h["mem_total"] - h["mem_avail"]
    used_pct = used / h["mem_total"] * 100
    apps_mem = sum(r["mem_mb"] or 0 for r in rows if r["runtime"] == "Docker")
    host_mem = sum(r["mem_mb"] or 0 for r in rows if r["runtime"].startswith("Host process"))
    sys_mem = sum(r["mem_mb"] or 0 for r in rows if r["runtime"] == "System service")
    other_mem = max(0, used - apps_mem - host_mem - sys_mem)
    mem_sub = (f"Sites & apps {fmt_mb(apps_mem)} · Host apps {fmt_mb(host_mem)} · "
               f"Docker & databases {fmt_mb(sys_mem)} · Everything else {fmt_mb(other_mem)}")

    att = list(items)
    if h["disk_pct"] >= 85:
        att.insert(0, ("red" if h["disk_pct"] >= 90 else "amber", "Server disk is almost full",
                       f"{h['disk_pct']}% used, only {h['disk_free_gb']} GB left. A full disk takes every site down at once."))
    att_html = "".join(f'<li>{pill(l, "Problem" if l == "red" else "Check")}<div><strong>{E(t)}</strong>'
                       f'<div class="muted small">{E(reason_plain(d))}</div></div></li>' for l, t, d in att) \
        or '<li class="muted">Nothing — everything is fine.</li>'

    stats = "".join(f'<div class="stat {k}"><b>{counts[k]}</b><span>{LABEL[k]}</span></div>' for k in ("green", "amber", "red", "gray"))
    sections = "".join(section_html(sid, t, d, rows) for sid, t, d in SECTIONS)
    new = [r for r in rows if not r["meta"]]
    if new:
        g = [dict(name=r["key"], prod=[r], dev=[]) for r in new]
        sections += (f'<section id="new"><div class="sh"><div><h2 class="redt">Unclassified</h2>'
                     f'<span class="muted">Found on the server but not named yet — add one entry to the catalog file</span></div></div>'
                     f'<div class="card tw"><table class="pt one"><thead><tr><th>Technical name</th><th>Folder</th><th>Status</th></tr></thead>'
                     f'{"".join(project_html(x, False) for x in g)}</table></div></section>')

    fold = "".join(f'<tr><td class="mono">{E(short(f["path"]))}</td><td>{E(", ".join(f["kind"]) or "no app files")}</td>'
                   f'<td class="muted">{dt.datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")}</td></tr>' for f in unused)
    blind = "".join(f"<li>{E(b)}</li>" for b in inv["blind"]) or "<li>None</li>"
    ok = control.get("level") == "red"
    ports = inv.get("ports_info", [])
    unknown = [p for p in ports if not p["what"]]
    prow = "".join(f'<tr><td class="mono"><b>{p["port"]}</b></td><td class="muted">{E(p["host"])}</td>'
                   f'<td>{E(p["what"]) if p["what"] else "<span class=redt><b>Unknown</b> — nothing explains this port</span>"}</td></tr>'
                   for p in ports)
    bottom = f'''<section id="more"><details class="card pad"><summary>Folders with no running app <span class="count">{len(unused)}</span></summary>
<p class="muted small">Project folders on the server that nothing is using — old, archived or never deployed.</p>
<div class="tw"><table class="plain"><thead><tr><th>Folder</th><th>Contains</th><th>Last changed</th></tr></thead><tbody>{fold}</tbody></table></div></details>
<details class="card pad"><summary>Open ports <span class="count">{len(ports)}</span>{f' <span class="redt small">· {len(unknown)} unexplained</span>' if unknown else ""}</summary>
<p class="muted small">Ports the server listens on from outside itself (local-only ports are left out). Each one should map to a known site or service.</p>
<div class="tw"><table class="plain"><thead><tr><th>Port</th><th>Listens on</th><th>What it is</th></tr></thead><tbody>{prow}</tbody></table></div></details>
<details class="card pad"><summary>What this check could not see <span class="count">{len(inv["blind"])}</span></summary>
<p class="muted small">Checked while logged in as <code>{E(inv["whoami"])}</code>. Anything inside these is not shown above. Root access would close them.</p><ul>{blind}</ul></details>
<p class="muted small foot">Self-check: a made-up address returned {control.get("code")} ({E(control.get("why", ""))}) —
{"it was reported Down, so the colours can be trusted." if ok else "it was NOT reported Down, so the colours may be unreliable."}</p></section>'''

    return (PAGE.replace("{{WHEN}}", E(when))
            .replace("{{STATS}}", stats)
            .replace("{{GAUGES}}", gauge("Memory", used_pct, f"{used / 1024:.1f} of {h['mem_total'] / 1024:.1f} GB", lvl(used_pct, 80, 92), mem_sub)
                     + gauge("Disk", h["disk_pct"], f"{h['disk_pct']}% full · {h['disk_free_gb']} GB free", lvl(h["disk_pct"], 85, 95), eta_txt))
            .replace("{{ATT}}", att_html).replace("{{NATT}}", str(len(att)))
            .replace("{{TOP}}", top_users(rows)).replace("{{APPSMEM}}", fmt_mb(apps_mem))
            .replace("{{SECTIONS}}", sections).replace("{{BOTTOM}}", bottom)
            .replace("{{CHANGES}}", changes_html(extra.get("events", []), extra.get("started")))
            .replace("{{SERVER}}", E(f'{CFG["server"]["name"]} · {CFG["server"]["ip"]}'))
            .replace("{{SHORT}}", short_legend())
            .replace("{{VER}}", str(extra.get("version", 0))).replace("{{PROBE}}", f'{extra.get("probe_at", 0):.0f}')
            .replace("{{INV}}", f'{extra.get("inv_at", 0):.0f}').replace("{{SPROBE}}", str(extra.get("stale_probe", 600)))
            .replace("{{SINV}}", str(extra.get("stale_inv", 600))))


PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>VPS Dashboard</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--ink:#16191f;--ink2:#4a5160;--muted:#737b8a;--line:#e6e8ec;--navy:#0f1b33;
--green:#15803d;--greenbg:#e2f3e9;--amber:#a15c00;--amberbg:#fcefd5;--red:#c0262d;--redbg:#fbe4e5;--gray:#5b6472;--graybg:#eceef2;
--link:#1f5fd6;--bar:#3f6fd8;--track:#e9ecf1;--shadow:0 1px 2px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0e1117;--panel:#161b24;--ink:#e9ecf2;--ink2:#b6bdca;--muted:#8b93a3;--line:#262d3a;--navy:#0a1226;
--green:#52c98a;--greenbg:#143222;--amber:#f2b04a;--amberbg:#352810;--red:#f47a7f;--redbg:#3b1b1e;--gray:#9aa2b0;--graybg:#222833;
--link:#7fa9ff;--bar:#6c97f5;--track:#232b3a;--shadow:none}}
*{box-sizing:border-box}[hidden]{display:none!important}html{scroll-padding-top:64px}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
header{background:var(--navy);color:#fff;position:sticky;top:0;z-index:10}
.bar{max-width:1240px;margin:0 auto;padding:12px 20px;display:flex;gap:22px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;font-size:16px}.brand span{font-weight:500;opacity:.55;font-size:12px;margin-left:8px}
header a{color:#b9c5de;text-decoration:none;font-weight:600}header a:hover{color:#fff}.when{margin-left:auto;font-size:12px;opacity:.65}
main{max-width:1240px;margin:0 auto;padding:22px 20px 60px}
.summary{display:grid;grid-template-columns:auto 1fr;gap:18px 40px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 22px;box-shadow:var(--shadow)}
.stats{display:flex;gap:34px}.stat{display:flex;flex-direction:column}.stat b{font-size:34px;line-height:1.1;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.stat.green b{color:var(--green)}.stat.amber b{color:var(--amber)}.stat.red b{color:var(--red)}.stat.gray b{color:var(--gray)}
.gauges{display:grid;grid-template-columns:1fr 1fr;gap:10px 28px}.gl{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}.gl span{color:var(--muted);font-weight:600}
.meter{height:8px;background:var(--track);border-radius:4px;overflow:hidden}.meter i{display:block;height:100%;background:var(--green);border-radius:4px}.meter i.amber{background:var(--amber)}.meter i.red{background:var(--red)}.gsub{font-size:12px;color:var(--muted);margin-top:5px}
@media(max-width:860px){.summary{grid-template-columns:1fr}.gauges{grid-template-columns:1fr}.stats{gap:22px;flex-wrap:wrap}}
.two{display:grid;grid-template-columns:1.25fr 1fr;gap:16px;margin:16px 0 8px;align-items:start}@media(max-width:860px){.two{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);margin-bottom:14px;overflow:hidden}.card.pad{padding:16px 20px}
h2{margin:0;font-size:20px;display:inline-block;margin-right:12px}h3{margin:0 0 4px;font-size:15px}code{font:12px ui-monospace,Menlo,monospace;background:var(--graybg);padding:1px 5px;border-radius:4px}
.count{font-size:12px;font-weight:600;background:var(--graybg);color:var(--muted);padding:1px 8px;border-radius:10px;margin-left:6px}
.att{list-style:none;margin:8px 0 0;padding:0}.att li{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}.att li:first-child{border-top:0}
.hb{display:grid;grid-template-columns:minmax(110px,40%) 1fr 64px;gap:10px;align-items:center;padding:4px 0}.hn{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.env-t{font-size:11px;color:var(--muted);margin-left:6px}.hbar{height:8px;background:var(--track);border-radius:4px}.hbar i{display:block;height:100%;background:var(--bar);border-radius:0 4px 4px 0}
.hv{text-align:right;font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}
.tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:26px 0 4px;position:sticky;top:50px;z-index:5;background:var(--bg);padding:8px 0}
.tools input{flex:1;min-width:200px;max-width:360px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel)}
.seg button{border:0;background:none;color:var(--ink2);font:inherit;font-size:13px;padding:7px 13px;cursor:pointer;border-right:1px solid var(--line)}.seg button:last-child{border:0}
.seg button.on{background:var(--ink);color:var(--bg)}.lbl{font-size:12px;color:var(--muted);font-weight:600}
.sh{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin:26px 0 10px}.ss{display:flex;gap:18px;font-size:13px;color:var(--muted)}.ss b{color:var(--ink)}
.tw{overflow-x:auto}table{width:100%;border-collapse:collapse}table.pt{min-width:880px;table-layout:fixed}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700;background:var(--bg);padding:9px 16px;border-bottom:1px solid var(--line)}
table.pt th:nth-child(1){width:22%}table.pt th:nth-child(2){width:17%}
td{padding:13px 16px;vertical-align:top;border-bottom:1px solid var(--line)}tbody.proj:last-child td{border-bottom:0}
tr.main{cursor:pointer}tr.main:hover td{background:color-mix(in srgb,var(--bg) 55%,transparent)}
.chev{display:inline-block;width:14px;color:var(--muted);font-size:11px;transition:transform .15s}tbody.open .chev{transform:rotate(90deg)}
.pn strong{font-size:14px}.fo .mono{font:12px ui-monospace,Menlo,monospace;color:var(--ink2);overflow-wrap:anywhere}.ft{font:700 10px -apple-system,sans-serif;text-transform:uppercase;color:var(--muted);margin-right:4px}
.item+.item{margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}.eline{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.ram{font-size:12px;font-weight:600;color:var(--ink2);font-variant-numeric:tabular-nums}.url{font-size:13px;word-break:break-all}.url a,.dgrid a{color:var(--link);text-decoration:none}.url a:hover,.dgrid a:hover{text-decoration:underline}
.more{color:var(--muted);font-size:12px}.rs{font-size:12px;color:var(--muted);margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;padding:2px 10px 2px 6px;border-radius:999px;white-space:nowrap}
.pill b{display:inline-grid;place-items:center;width:15px;height:15px;border-radius:50%;font-size:10px;line-height:1;color:var(--panel)}
.pill.green{background:var(--greenbg);color:var(--green)}.pill.amber{background:var(--amberbg);color:var(--amber)}.pill.red{background:var(--redbg);color:var(--red)}.pill.gray{background:var(--graybg);color:var(--gray)}
.pill.green b{background:var(--green)}.pill.amber b{background:var(--amber)}.pill.red b{background:var(--red)}.pill.gray b{background:var(--gray)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px}.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}.dot.red{background:var(--red)}.dot.gray{background:var(--gray)}
.greent{color:var(--green)}.redt{color:var(--red)}.muted{color:var(--muted)}.small{font-size:12px}
tr.detail{display:none}tbody.open tr.detail{display:table-row}tr.detail td{background:var(--bg);padding:14px 20px 18px 36px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:22px}.dblock h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.durls{margin-bottom:8px;font-size:13px}dl{display:grid;grid-template-columns:110px 1fr;gap:3px 12px;margin:0;font-size:13px}dt{color:var(--muted)}dd{margin:0;word-break:break-word}
details summary{cursor:pointer;font-weight:600}details[open] summary{margin-bottom:8px}ul{padding-left:20px}
table.plain th,table.plain td{padding:7px 10px;font-size:13px}table.plain .mono{font:12px ui-monospace,Menlo,monospace}.foot{margin-top:18px}
.when{display:flex;gap:10px;align-items:center}#rfb{background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.25);border-radius:6px;font:inherit;font-size:12px;padding:4px 10px;cursor:pointer}
#rfb:hover{background:rgba(255,255,255,.22)}#rfb:disabled{opacity:.6;cursor:wait}
#banner .b{padding:8px 20px;font-size:13px;font-weight:600;text-align:center}#banner .b.red{background:var(--red);color:#fff}#banner .b.amber{background:var(--amber);color:#fff}
.chlist{list-style:none;margin:8px 0 0;padding:0}.chlist li{display:grid;grid-template-columns:110px minmax(180px,1fr) auto;gap:12px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:13px}
.chlist li:first-child{border-top:0}.ts{color:var(--muted);font-variant-numeric:tabular-nums}.chg{display:flex;gap:8px;align-items:center;color:var(--ink2)}
.evt{font-weight:600}.evt.warn{color:var(--amber)}.evt.okk{color:var(--green)}
.trend{display:flex;align-items:center;gap:10px;margin-top:10px;font-size:13px}.spark{color:var(--bar);flex:none}
</style></head><body>
<header><div class="bar"><div class="brand">VPS Dashboard<span>{{SERVER}}</span></div>
<a href="#web">Websites</a><a href="#app">Apps</a><a href="#infra">Infrastructure</a><a href="#more">More</a><div class="when"><span id="age" data-ver="{{VER}}" data-probe="{{PROBE}}" data-inv="{{INV}}" data-sprobe="{{SPROBE}}" data-sinv="{{SINV}}">Checked {{WHEN}}</span>
<button id="rfb" hidden>Refresh now</button></div></div><div id="banner" hidden></div></header>
<main>
<div id="dyn1">
<div class="summary"><div class="stats">{{STATS}}</div><div class="gauges">{{GAUGES}}</div></div>
<div class="two">
<div class="card pad"><h3>Needs attention <span class="count">{{NATT}}</span></h3><ul class="att">{{ATT}}</ul></div>
<div class="card pad"><h3>Biggest memory users</h3><div class="muted small" style="margin-bottom:8px">Sites and apps use {{APPSMEM}} of the server's memory in total</div>{{TOP}}</div>
</div>
<div class="card pad"><h3>Recent changes</h3>{{CHANGES}}</div>
</div>
<div class="tools"><input id="q" type="search" placeholder="Search a project, address or folder…">
<span class="lbl">Show</span><div class="seg" id="fseg"><button class="on" data-f="all">Everything</button><button data-f="prob">Problems only</button></div>
<span class="lbl">Sort by</span><div class="seg" id="sseg"><button class="on" data-s="status">Status</button><button data-s="name">Name</button><button data-s="ram">RAM</button></div></div>
<div id="dyn2">
{{SECTIONS}}
{{BOTTOM}}
<p class="muted small">Click a row for technical details{{SHORT}} · RAM is what the app uses right now.</p>
</div>
</main>
<script>
let mode="status",q="",prob=false;
function sortAll(){document.querySelectorAll("table.pt").forEach(t=>{const rows=[...t.querySelectorAll("tbody.proj")];
 rows.sort((a,b)=>{const A=a.dataset,B=b.dataset,n=A.name.localeCompare(B.name);
  if(mode==="name")return n;if(mode==="ram")return (B.ram-A.ram)||n;return (A.rank-B.rank)||(B.ram-A.ram)||n});
 rows.forEach(r=>t.appendChild(r))})}
function filt(){document.querySelectorAll("tbody.proj").forEach(t=>{t.hidden=!((!q||t.dataset.search.includes(q))&&(!prob||+t.dataset.rank<=1))})}
document.getElementById("q").oninput=e=>{q=e.target.value.toLowerCase().trim();filt()};
function seg(id,fn){const s=document.getElementById(id);s.onclick=e=>{const b=e.target.closest("button");if(!b)return;
 s.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));fn(b)}}
seg("fseg",b=>{prob=b.dataset.f==="prob";filt()});seg("sseg",b=>{mode=b.dataset.s;sortAll()});
document.addEventListener("click",e=>{const m=e.target.closest("tr.main");if(m)m.parentNode.classList.toggle("open")});
sortAll();
// ── live refresh: swap new data in place (no reload, nothing jumps) ──
const age=document.getElementById("age"),ban=document.getElementById("banner"),rfb=document.getElementById("rfb");
const served=location.protocol.startsWith("http");let srv={error:null,refreshing:false},fails=0;
function ago(ms){const s=Math.max(0,Math.round(ms/1000));if(s<60)return s+" sec";const m=Math.round(s/60);if(m<60)return m+" min";return Math.floor(m/60)+" h "+(m%60)+" min"}
function paint(){const d=age.dataset,now=Date.now(),pr=+d.probe*1000,iv=+d.inv*1000;
 age.textContent=(srv.refreshing?"Checking now… · ":"")+(d.probe===d.inv?"Checked "+ago(now-pr)+" ago":"Sites "+ago(now-pr)+" ago · Server details "+ago(now-iv)+" ago");
 const m=[];
 if(pr>0&&now-pr>+d.sprobe*1000)m.push(["red","Site checks are "+ago(now-pr)+" old — the checker may have stopped. Do not rely on this page."]);
 else if(iv>0&&now-iv>+d.sinv*1000)m.push(["red","Server details (RAM, disk, ports) are "+ago(now-iv)+" old — the checker may have stopped."]);
 if(fails>=3)m.push(["red","Lost connection to the dashboard server. Showing the last data received."]);
 else if(srv.error)m.push(["amber","The last full check failed ("+srv.error+"). Showing the last good data."]);
 else if(srv.probe_error)m.push(["amber","The last quick site check failed ("+srv.probe_error+")."]);
 ban.replaceChildren(...m.map(x=>{const e=document.createElement("div");e.className="b "+x[0];e.textContent=x[1];return e}));ban.hidden=!m.length}
function localize(){document.querySelectorAll("time.lt").forEach(t=>{const d=new Date(+t.dataset.ts*1000);
 t.textContent=d.toLocaleString(undefined,{month:"short",day:"2-digit",hour:"2-digit",minute:"2-digit",timeZoneName:"short"});t.title=d.toISOString()})}
function swapIn(text){const doc=new DOMParser().parseFromString(text,"text/html"),open=[...document.querySelectorAll("tbody.proj.open")].map(t=>t.dataset.name);
 ["dyn1","dyn2"].forEach(id=>{const n=doc.getElementById(id),o=document.getElementById(id);if(n&&o)o.replaceChildren(...n.childNodes)});
 const na=doc.getElementById("age");if(na)for(const k of ["ver","probe","inv","sprobe","sinv"])age.dataset[k]=na.dataset[k];
 sortAll();filt();document.querySelectorAll("tbody.proj").forEach(t=>{if(open.includes(t.dataset.name))t.classList.add("open")});localize();paint()}
async function poll(){if(!served)return;try{const r=await fetch("status.json",{cache:"no-store"});if(!r.ok)throw 0;const j=await r.json();fails=0;srv=j;
 if(String(j.version)!==age.dataset.ver){const p=await fetch(location.pathname,{cache:"no-store"});if(p.ok)swapIn(await p.text())}}catch(e){fails++}paint()}
if(served){rfb.hidden=false;rfb.onclick=async()=>{rfb.disabled=true;rfb.textContent="Checking…";
 try{await fetch("refresh",{method:"POST",headers:{"X-Requested-With":"dashboard"}})}catch(e){}
 let n=0;const t=setInterval(async()=>{await poll();if(++n>40||!srv.refreshing&&n>3){clearInterval(t);rfb.disabled=false;rfb.textContent="Refresh now"}},3000)};
 setInterval(poll,10000);poll()}
setInterval(paint,5000);paint();localize();
</script></body></html>'''
