"""
Settings: which server to read, where its projects live, and what to call things.

Your real values live in a PRIVATE config file (never in this repo). The tool looks for it at:
  1. the path in the DASH_CONFIG environment variable
  2. ./config.toml
  3. config.toml next to this file
Start from config.example.toml. Python 3.11+ (standard library only).
"""
import os
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).parent

DEFAULTS = {
    "server": {"name": "My server", "ip": "", "ssh": "", "control_domain": "example.com"},
    "scan": {
        "project_dirs": [],
        "traefik_dynamic_dir": "",
        "traefik_static": "",
        "pm2_dirs": [],
        # host processes (outside Docker) whose command line contains one of these words are listed
        "process_patterns": ["node", "next-server", "gunicorn", "uvicorn", "php", "java", "deno", "bun"],
    },
    "shorthand": {},
    "services": {"docker": "docker"},
    "catalog": {"file": "catalog.toml"},
}


def _find():
    env = os.environ.get("DASH_CONFIG")
    if env:
        return Path(env).expanduser()
    for c in (Path("config.toml"), HERE / "config.toml"):
        if c.exists():
            return c
    return None


def load():
    path = _find()
    if path is None or not path.exists():
        sys.exit("No config found.\nCopy config.example.toml to config.toml (or point DASH_CONFIG at your file) and edit it — see README.md.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for section, vals in raw.items():
        if isinstance(vals, dict) and isinstance(cfg.get(section), dict):
            cfg[section].update(vals)
        else:
            cfg[section] = vals
    if not cfg["server"]["ip"]:
        sys.exit(f"{path}: [server] ip is required (the address your sites are served from).")
    cat = Path(cfg["catalog"]["file"]).expanduser()
    cfg["catalog_path"] = cat if cat.is_absolute() else (path.parent / cat)
    cfg["config_path"] = path
    return cfg


CFG = load()
