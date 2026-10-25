"""In-memory code snapshot built from a GitHub tarball or an uploaded archive.

Excluded before anything is analysed or sent to a model (agent-workflows §3, §7.3): files over
1 MB, binaries, dependency/build directories, ``.env*`` files, key material, and any file in
which the secret scanner finds a credential.
"""

from __future__ import annotations

import io
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from reliability.llm.redact import SECRET_PATTERNS

MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 150 * 1024 * 1024
MAX_FILES = 20000
SKIP_DIRS = {
    "node_modules",
    ".git",
    ".next",
    "dist",
    "build",
    "out",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".turbo",
    "vendor",
    "target",
    ".cache",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".svg",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".mp3",
    ".wasm",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".pyc",
    ".class",
    ".jar",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".der",
    ".keystore",
    ".lock",
}
KEEP_NAMES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock"}  # presence only
SECRET_NAMES = re.compile(r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|credentials(\.json)?|secrets?\.(json|ya?ml))$")


@dataclass
class CodeSnapshot:
    files: dict[str, str] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)  # path -> reason
    lockfiles: set[str] = field(default_factory=set)

    def glob(self, *suffixes: str) -> list[str]:
        return sorted(p for p in self.files if p.endswith(suffixes))

    def read(self, path: str) -> str:
        return self.files.get(path, "")

    @property
    def total_bytes(self) -> int:
        return sum(len(v) for v in self.files.values())


def _accept(snapshot: CodeSnapshot, path: str, data: bytes) -> None:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else path
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return
    if name in KEEP_NAMES:
        snapshot.lockfiles.add(path)
        return
    if SECRET_NAMES.search(path) and not name.endswith((".example", ".sample", ".template")):
        snapshot.excluded[path] = "secret-bearing file name"
        return
    if PurePosixPath(name).suffix.lower() in SKIP_SUFFIXES:
        return
    if len(data) > MAX_FILE_BYTES:
        snapshot.excluded[path] = "larger than 1 MB"
        return
    if b"\x00" in data[:8000]:
        return
    text = data.decode("utf-8", errors="replace")
    if any(p.search(text) for p in SECRET_PATTERNS):
        snapshot.excluded[path] = "contains a credential"
        return
    snapshot.files[path] = text


def _strip_root(names: list[str]) -> str:
    """GitHub tarballs wrap everything in ``<owner>-<repo>-<sha>/``."""
    tops = {n.split("/", 1)[0] for n in names if n}
    if len(tops) == 1 and all("/" in n or n.endswith("/") or n in tops for n in names):
        return next(iter(tops)) + "/"
    return ""


def _safe(path: str) -> bool:
    p = PurePosixPath(path)
    return not p.is_absolute() and ".." not in p.parts


def from_archive(data: bytes) -> CodeSnapshot:
    snapshot = CodeSnapshot()
    total = 0
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            root = _strip_root([i.filename for i in infos])
            for info in infos[:MAX_FILES]:
                if not _safe(info.filename) or info.file_size > MAX_FILE_BYTES * 4:
                    continue
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    break
                _accept(snapshot, info.filename.removeprefix(root), zf.read(info))
        return snapshot
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        root = _strip_root([m.name for m in members])
        for member in members[:MAX_FILES]:
            if not _safe(member.name) or member.size > MAX_FILE_BYTES * 4:
                continue
            total += member.size
            if total > MAX_TOTAL_BYTES:
                break
            fh = tf.extractfile(member)
            if fh is not None:
                _accept(snapshot, member.name.removeprefix(root), fh.read())
    return snapshot


def from_files(files: dict[str, str]) -> CodeSnapshot:
    snapshot = CodeSnapshot()
    for path, text in files.items():
        _accept(snapshot, path, text.encode())
    return snapshot
