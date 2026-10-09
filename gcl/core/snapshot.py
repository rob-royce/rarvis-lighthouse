"""Capture a worktree without writing its real Git index or history."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from .regular_file import read_regular


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, index: Path, *args: str) -> bytes:
    env = dict(os.environ, GIT_INDEX_FILE=str(index), GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "--no-pager", *args], cwd=repo, env=env,
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _copy_index(repo: Path, temp: Path) -> Path:
    git_index = subprocess.run(["git", "rev-parse", "--git-path", "index"], cwd=repo,
                               check=True, capture_output=True, text=True).stdout.strip()
    source = Path(git_index)
    if not source.is_absolute():
        source = repo / source
    target = temp / "index"
    if source.exists():
        shutil.copy2(source, target)
    return target


def _contents(repo: Path, temp: Path):
    index = _copy_index(repo, temp)
    head = _git(repo, index, "rev-parse", "HEAD").decode().strip()
    diff = _git(repo, index, "-c", "core.quotePath=false", "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    names = _git(repo, index, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    untracked = []
    for raw in names:
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = repo / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"unsupported untracked path: {name}")
        untracked.append({"path": name, "sha256": _hash(read_regular(path))})
    return head, diff, untracked


def capture(repo_dir, out_dir) -> dict:
    repo = Path(repo_dir).resolve()
    out = Path(out_dir).resolve()
    if out == repo or repo in out.parents:
        raise ValueError("snapshot output must be outside source repo")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out) as temporary:
        head, diff, untracked = _contents(repo, Path(temporary))
    diff_path = out / "tracked.diff"
    tar_path = out / "untracked.tar"
    diff_path.write_bytes(diff)
    with tarfile.open(tar_path, "w") as archive:
        for entry in untracked:
            data = read_regular(repo / entry["path"])
            if _hash(data) != entry["sha256"]:
                raise ValueError(f"untracked file changed during capture: {entry['path']}")
            info = tarfile.TarInfo(entry["path"])
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return {"base_head": head, "diff_path": str(diff_path), "diff_sha256": _hash(diff),
            "untracked": untracked, "tar_path": str(tar_path), "tar_sha256": _hash(tar_path.read_bytes())}


def verify(manifest: dict, repo_dir) -> bool:
    try:
        if _hash(Path(manifest["diff_path"]).read_bytes()) != manifest["diff_sha256"]:
            return False
        if _hash(Path(manifest["tar_path"]).read_bytes()) != manifest["tar_sha256"]:
            return False
        with tempfile.TemporaryDirectory() as temporary:
            head, diff, untracked = _contents(Path(repo_dir).resolve(), Path(temporary))
        return (head == manifest["base_head"] and _hash(diff) == manifest["diff_sha256"]
                and untracked == manifest["untracked"])
    except (OSError, KeyError, subprocess.CalledProcessError, ValueError):
        return False


def restore(manifest: dict, target_repo_dir) -> None:
    target = Path(target_repo_dir).resolve()
    with tempfile.TemporaryDirectory() as temporary:
        index = _copy_index(target, Path(temporary))
        head = _git(target, index, "rev-parse", "HEAD").decode().strip()
    if head != manifest["base_head"]:
        raise ValueError("target HEAD differs from snapshot base")
    diff = Path(manifest["diff_path"]).read_bytes()
    if _hash(diff) != manifest["diff_sha256"]:
        raise ValueError("tracked diff checksum mismatch")
    tar = Path(manifest["tar_path"]).read_bytes()
    if _hash(tar) != manifest["tar_sha256"]:
        raise ValueError("untracked tar checksum mismatch")
    # Validate everything before touching the worktree, so a bad snapshot can
    # never leave the target half-restored.
    expected = {entry["path"]: entry["sha256"] for entry in manifest["untracked"]}
    files = []
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as archive:
        members = archive.getmembers()
        if {member.name for member in members} != set(expected):
            raise ValueError("tar manifest mismatch")
        for member in members:
            destination = (target / member.name).resolve()
            if target not in destination.parents or not member.isfile():
                raise ValueError("unsafe untracked file")
            data = archive.extractfile(member).read()
            if _hash(data) != expected[member.name]:
                raise ValueError("mismatched untracked file")
            if destination.exists() and (not destination.is_file() or _hash(read_regular(destination)) != expected[member.name]):
                raise ValueError(f"untracked path already exists in target: {member.name}")
            files.append((destination, data))
    if diff:
        for check in (["--check"], []):
            subprocess.run(["git", "apply", "--binary", *check, "--", str(manifest["diff_path"])],
                           cwd=target, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for destination, data in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
