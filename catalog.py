"""
Catalog — the only hand-maintained part: what each thing on the server IS (section, name, prod/dev).

Discovery is automatic. Anything found on the server that is NOT listed here still appears,
in a red "Unclassified" section — so forgetting an entry can never hide something.

The catalog is a private TOML file (see catalog.example.toml for the format); its path comes
from [catalog] file in your config. Keys:
  <container name>   a Docker container
  route:<file>       a Traefik file-config route (dynamic/<file>.yml)
  proc:<id>          an app running directly on the host (PM2 etc.) — needs `match`
  svc:<name>         a system service (docker, postgresql, redis, fail2ban) — see [services] in the config
"""
import sys
import tomllib

from config import CFG

FIELDS = ("section", "name", "env", "note", "folder", "match", "hosts")


def load():
    p = CFG["catalog_path"]
    if not p.exists():
        sys.exit(f"Catalog file not found: {p}\nCopy catalog.example.toml and edit it (or set [catalog] file in your config).")
    out = {}
    for it in tomllib.loads(p.read_text(encoding="utf-8")).get("item", []):
        key = it["key"]
        if key in out:
            sys.exit(f"{p}: duplicate catalog key {key!r}")
        if it.get("section") not in ("web", "app", "infra") or it.get("env") not in ("prod", "dev"):
            sys.exit(f"{p}: {key!r} needs section = web|app|infra and env = prod|dev")
        out[key] = dict(section=it["section"], name=it["name"], env=it["env"], note=it.get("note", ""),
                        folder=it.get("folder", ""), match=it.get("match", ""), hosts=list(it.get("hosts", [])))
    return out


CATALOG = load()
