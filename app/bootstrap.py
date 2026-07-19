"""First-boot data bootstrap for hosted deployments (Streamlit Community Cloud).

Cloud containers start with an empty disk. Rather than re-ingesting weeks of
SEC filings on every boot (~40 minutes), the compressed SQLite snapshot is
pulled from this repo's GitHub release tagged `data`, which the daily GitHub
Actions workflow keeps fresh. A private repo needs a read-only token
(Streamlit secret or env var GITHUB_TOKEN) to fetch the asset.
"""
import gzip
import logging
import os
import shutil

import requests

from . import config

log = logging.getLogger(__name__)

REPO = "keithlimenspire-alt/insider-screener"
RELEASE_TAG = "data"
ASSET_NAME = "insider.db.gz"


def db_is_empty() -> bool:
    return not config.DB_PATH.exists() or config.DB_PATH.stat().st_size < 1_000_000


def ensure_db(token: str | None = None, repo: str = REPO) -> tuple[bool, str]:
    """Download and unpack the latest DB snapshot if the local DB is missing.

    Returns (ok, message)."""
    if not db_is_empty():
        return True, "database already present"
    token = token or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        rel = requests.get(
            f"https://api.github.com/repos/{repo}/releases/tags/{RELEASE_TAG}",
            headers=headers, timeout=30)
        rel.raise_for_status()
        assets = {a["name"]: a for a in rel.json().get("assets", [])}
        if ASSET_NAME not in assets:
            return False, f"release '{RELEASE_TAG}' has no asset {ASSET_NAME}"
        dl_headers = dict(headers)
        dl_headers["Accept"] = "application/octet-stream"
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_gz = config.DB_PATH.with_suffix(".db.gz.part")
        with requests.get(assets[ASSET_NAME]["url"], headers=dl_headers,
                          stream=True, timeout=600) as resp:
            resp.raise_for_status()
            with tmp_gz.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        tmp_db = config.DB_PATH.with_suffix(".db.part")
        with gzip.open(tmp_gz, "rb") as src, tmp_db.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp_gz.unlink(missing_ok=True)
        os.replace(tmp_db, config.DB_PATH)
        size_mb = config.DB_PATH.stat().st_size / 1e6
        return True, f"downloaded snapshot ({size_mb:.0f} MB)"
    except requests.RequestException as e:
        return False, f"snapshot download failed: {e}"
