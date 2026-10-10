"""Download and update the Parquet datasets that DuckDB serves.

The data lives in monthly GitHub releases of ``scriptogre/biomedical-data``.
Each release is tagged ``<dataset>/<YYYY-MM-DD>`` and carries two assets:
``<dataset>.tar.gz`` (the Parquet files) and ``<dataset>.sha256`` (its checksum).

This module is deliberately independent of ``app.config`` so that it can run
before a ``.env`` file exists (``python -m app.data_sync update``). The target
directory comes from the ``PARQUET_DIR`` environment variable, defaulting to
``<repo>/data/parquet``.

Layout on disk after a successful download::

    data/parquet/<dataset>/*.parquet
    data/parquet/<dataset>/.release     # tag that produced these files
    data/parquet/<dataset>.sha256       # checksum of the archive

Usage from the application: ``start_update(name)`` schedules a background job,
``get_status()`` reports local and remote versions, ``JOBS`` exposes progress.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

REPO = "scriptogre/biomedical-data"
DATASETS = ("drugcentral", "clinicaltrials", "pharos")
API_URL = f"https://api.github.com/repos/{REPO}/releases"
RELEASE_FILE = ".release"
CACHE_TTL = 600  # seconds to keep the release list before asking GitHub again


def parquet_dir() -> Path:
    env = os.environ.get("PARQUET_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "parquet"


# --------------------------------------------------------------------------
# Remote release information
# --------------------------------------------------------------------------

@dataclass
class Release:
    tag: str
    date: str
    archive_url: str
    archive_size: int
    checksum_url: str | None


_release_cache: dict[str, Release] = {}
_release_cache_time = 0.0
_release_error: str | None = None


async def fetch_latest_releases(force: bool = False) -> dict[str, Release]:
    """Return the newest release per dataset, cached for CACHE_TTL seconds."""
    global _release_cache, _release_cache_time, _release_error
    if not force and _release_cache and time.time() - _release_cache_time < CACHE_TTL:
        return _release_cache

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "pharma-pilot"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    latest: dict[str, Release] = {}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            page = 1
            while page <= 5:
                resp = await client.get(API_URL, params={"per_page": 100, "page": page})
                resp.raise_for_status()
                releases = resp.json()
                if not releases:
                    break
                for rel in releases:
                    tag = rel.get("tag_name", "")
                    name, _, date = tag.partition("/")
                    if name not in DATASETS or not date:
                        continue
                    assets = {a["name"]: a for a in rel.get("assets", [])}
                    archive = assets.get(f"{name}.tar.gz")
                    if not archive:
                        continue
                    checksum = assets.get(f"{name}.sha256")
                    candidate = Release(
                        tag=tag,
                        date=date,
                        archive_url=archive["browser_download_url"],
                        archive_size=int(archive.get("size", 0)),
                        checksum_url=checksum["browser_download_url"] if checksum else None,
                    )
                    if name not in latest or candidate.date > latest[name].date:
                        latest[name] = candidate
                if len(releases) < 100:
                    break
                page += 1
        _release_error = None
    except Exception as exc:  # network down, rate limited, ...
        _release_error = f"{type(exc).__name__}: {exc}"
        if _release_cache:
            return _release_cache
        return {}

    _release_cache = latest
    _release_cache_time = time.time()
    return latest


# --------------------------------------------------------------------------
# Local state
# --------------------------------------------------------------------------

def local_info(name: str) -> dict:
    """Describe what is on disk for one dataset."""
    path = parquet_dir() / name
    files = [f for f in path.glob("*.parquet") if not f.name.startswith("._")] if path.exists() else []
    tag = None
    release_file = path / RELEASE_FILE
    if release_file.exists():
        tag = release_file.read_text(encoding="utf-8").strip() or None
    return {
        "name": name,
        "present": bool(files),
        "file_count": len(files),
        "size_bytes": sum(f.stat().st_size for f in files),
        "local_tag": tag,
        "local_date": tag.partition("/")[2] if tag else None,
    }


def missing_datasets() -> list[str]:
    return [name for name in DATASETS if not local_info(name)["present"]]


# --------------------------------------------------------------------------
# Jobs and progress
# --------------------------------------------------------------------------

@dataclass
class Job:
    name: str
    state: str = "queued"      # queued, downloading, verifying, extracting, installing, done, error
    done_bytes: int = 0
    total_bytes: int = 0
    message: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None

    @property
    def active(self) -> bool:
        return self.state not in ("done", "error")

    @property
    def percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, int(self.done_bytes * 100 / self.total_bytes))

    # -- persistence: the job file lets other worker processes see progress --
    _last_save: float = 0.0

    def path(self) -> Path:
        return parquet_dir() / ".tmp" / f"{self.name}.job.json"

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 0.5:
            return
        self._last_save = now
        data = {k: getattr(self, k) for k in
                ("name", "state", "done_bytes", "total_bytes", "message", "started", "finished")}
        data["updated"] = now
        try:
            self.path().parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path().with_suffix(".json.part")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path())
        except OSError:
            pass

    @classmethod
    def load(cls, name: str) -> "Job | None":
        path = parquet_dir() / ".tmp" / f"{name}.job.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        job = cls(name=name)
        for k in ("state", "done_bytes", "total_bytes", "message", "started", "finished"):
            if k in data:
                setattr(job, k, data[k])
        # A job that stopped reporting is dead (its worker restarted or crashed)
        if job.active and time.time() - data.get("updated", 0) > STALE_AFTER:
            job.state = "error"
            job.message = "Interrupted (no progress reported); start it again"
        return job


JOBS: dict[str, Job] = {}       # jobs owned by this process
STALE_AFTER = 120               # seconds without a progress write before a job counts as dead
_reload_hook = None  # callable invoked after a dataset is installed (set by the app)


def current_job(name: str) -> Job | None:
    """The job for ``name`` as seen by this process, falling back to the shared file."""
    job = JOBS.get(name)
    if job and job.active:
        return job
    return Job.load(name) or job


def set_reload_hook(func) -> None:
    global _reload_hook
    _reload_hook = func


def start_update(name: str) -> Job:
    """Schedule a background download for one dataset. Idempotent while running."""
    if name not in DATASETS:
        raise ValueError(f"unknown dataset: {name}")
    job = current_job(name)
    if job and job.active:
        return job
    job = Job(name=name)
    JOBS[name] = job
    job.save(force=True)
    asyncio.create_task(_run_job(job))
    return job


async def _run_job(job: Job) -> None:
    try:
        await download_dataset(job.name, job)
        if _reload_hook:
            job.state = "installing"
            job.message = "Reloading DuckDB views"
            job.save(force=True)
            await asyncio.to_thread(_reload_hook)
        job.state = "done"
        job.message = "Up to date"
    except Exception as exc:
        job.state = "error"
        job.message = f"{type(exc).__name__}: {exc}"
    finally:
        job.finished = time.time()
        job.save(force=True)


# --------------------------------------------------------------------------
# Download, verify, extract, install
# --------------------------------------------------------------------------

async def download_dataset(name: str, job: Job | None = None) -> Release:
    """Fetch the newest release of ``name`` and install it under PARQUET_DIR."""
    job = job or Job(name=name)
    releases = await fetch_latest_releases()
    if name not in releases:
        raise RuntimeError(f"no release found for '{name}' in {REPO}" + (f" ({_release_error})" if _release_error else ""))
    rel = releases[name]

    base = parquet_dir()
    tmp = base / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    archive = tmp / f"{name}.tar.gz"
    extract_dir = tmp / f"{name}_extract"

    # 1. Download, hashing as we go
    job.state = "downloading"
    job.total_bytes = rel.archive_size
    job.message = f"Downloading {rel.tag}"
    job.save(force=True)
    sha = hashlib.sha256()
    headers = {"User-Agent": "pharma-pilot"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), headers=headers, follow_redirects=True) as client:
        async with client.stream("GET", rel.archive_url) as resp:
            resp.raise_for_status()
            length = resp.headers.get("Content-Length")
            if length:
                job.total_bytes = int(length)
            with archive.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 20):
                    fh.write(chunk)
                    sha.update(chunk)
                    job.done_bytes += len(chunk)
                    job.save()

        # 2. Verify checksum when the release publishes one
        expected = None
        if rel.checksum_url:
            job.state = "verifying"
            job.message = "Verifying checksum"
            job.save(force=True)
            text = (await client.get(rel.checksum_url)).text
            expected = text.split()[0].strip().lower() if text.strip() else None
    actual = sha.hexdigest()
    if expected and expected != actual:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {name}: expected {expected[:12]}…, got {actual[:12]}…")

    # 3. Extract into a scratch directory
    job.state = "extracting"
    job.message = "Extracting Parquet files"
    job.save(force=True)
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir()
    await asyncio.to_thread(_extract, archive, extract_dir)
    parquet_files = [f for f in extract_dir.rglob("*.parquet") if not f.name.startswith("._")]
    if not parquet_files:
        raise RuntimeError(f"archive for {name} contained no .parquet files")

    # 4. Swap into place: old dir -> .old, staged -> live, delete .old
    job.state = "installing"
    job.message = "Installing"
    job.save(force=True)
    staged = tmp / f"{name}_staged"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir()
    for f in parquet_files:
        shutil.move(str(f), staged / f.name)
    (staged / RELEASE_FILE).write_text(rel.tag + "\n", encoding="utf-8")

    live = base / name
    old = tmp / f"{name}_old"
    if old.exists():
        shutil.rmtree(old)
    if live.exists():
        shutil.move(str(live), old)
    shutil.move(str(staged), live)
    (base / f"{name}.sha256").write_text(actual + "\n", encoding="utf-8")

    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(extract_dir, ignore_errors=True)
    archive.unlink(missing_ok=True)
    job.done_bytes = job.total_bytes
    return rel


def _extract(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target, filter="data")
        else:  # Python < 3.12
            tar.extractall(target)


# --------------------------------------------------------------------------
# Status for UI and CLI
# --------------------------------------------------------------------------

async def get_status(force: bool = False) -> dict:
    releases = await fetch_latest_releases(force=force)
    rows = []
    for name in DATASETS:
        info = local_info(name)
        rel = releases.get(name)
        job = current_job(name)
        info.update({
            "latest_tag": rel.tag if rel else None,
            "latest_date": rel.date if rel else None,
            "latest_size": rel.archive_size if rel else None,
            "update_available": bool(rel and info["local_date"] != rel.date),
            "job": job,
        })
        rows.append(info)
    return {
        "datasets": rows,
        "remote_error": _release_error,
        "any_active": any(r["job"] and r["job"].active for r in rows),
        "any_missing": any(not r["present"] for r in rows),
    }


def human_size(num: int | None) -> str:
    if not num:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


# --------------------------------------------------------------------------
# Command line: python -m app.data_sync [status|update|download] [names...]
# --------------------------------------------------------------------------

async def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    names = [n for n in argv[1:] if n in DATASETS] or list(DATASETS)
    if cmd == "status":
        status = await get_status(force=True)
        if status["remote_error"]:
            print(f"[warn] could not reach GitHub: {status['remote_error']}")
        print(f"{'dataset':<16}{'local':<14}{'latest':<14}{'files':>6}  {'size':>9}  state")
        for r in status["datasets"]:
            state = "missing" if not r["present"] else ("update available" if r["update_available"] else "up to date")
            print(f"{r['name']:<16}{(r['local_date'] or '-'):<14}{(r['latest_date'] or '?'):<14}"
                  f"{r['file_count']:>6}  {human_size(r['size_bytes']):>9}  {state}")
        return 0

    if cmd in ("update", "download"):
        status = await get_status(force=True)
        todo = []
        for r in status["datasets"]:
            if r["name"] not in names:
                continue
            if cmd == "download" and r["present"] and not r["update_available"]:
                print(f"{r['name']}: present and current, skipping")
                continue
            if cmd == "update" and r["present"] and not r["update_available"]:
                print(f"{r['name']}: already {r['local_date']}, skipping")
                continue
            todo.append(r["name"])
        for name in todo:
            job = Job(name=name)
            task = asyncio.create_task(download_dataset(name, job))
            last = -1
            while not task.done():
                await asyncio.sleep(0.5)
                if job.percent != last or job.state != "downloading":
                    last = job.percent
                    print(f"\r{name}: {job.state} {job.percent:3d}% {human_size(job.done_bytes)} / {human_size(job.total_bytes)}   ", end="", flush=True)
            print()
            rel = task.result()
            print(f"{name}: installed {rel.tag}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli(sys.argv[1:])))
