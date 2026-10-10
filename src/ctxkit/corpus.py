"""The knowledge base on disk: file discovery, folder roles and reachability.

Reachability is the structural idea behind the whole tool. An agent enters at
``README.md`` in the corpus root and follows names it finds there. A file is
healthy if it can be reached in at most three reads:

1. the root README names the file, or
2. the root README names the file's folder and that folder's README names the
   file, or
3. the folder README names a ``*-index.md`` cluster file that names the file.
   The cluster file must itself be named, otherwise it cannot vouch for anything.

There is no requirement that the root list everything. That requirement makes the
root grow with the collection, and every session pays for the whole catalogue.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .config import Config


@lru_cache(maxsize=8192)
def _mention_pattern(needle: str) -> re.Pattern[str]:
    # A mention must not sit inside a longer file or folder name, so that
    # "notes.md" is not vouched for by "api-notes.md" or "notes.md.bak". A trailing
    # full stop is allowed so a name at the end of a sentence still counts.
    return re.compile(r"(?<![\w.-])" + re.escape(needle) + r"(?![\w-]|\.\w)")


def mentions(haystack: str, needle: str) -> bool:
    """True when ``needle`` appears in ``haystack`` as a whole name, not as a fragment of a longer one."""
    return bool(needle) and _mention_pattern(needle).search(haystack) is not None


class Corpus:
    """A directory tree of markdown files plus the configuration that describes it."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.root
        self._files: list[Path] | None = None
        self._texts: dict[Path, str] = {}

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    @property
    def is_git_repo(self) -> bool:
        return (self.root / ".git").exists()

    def files(self) -> list[Path]:
        """Every ``*.md`` file under the root, sorted. Hidden folders are skipped."""
        if self._files is None:
            found: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in filenames:
                    if name.endswith(".md"):
                        found.append(Path(dirpath) / name)
            self._files = sorted(found)
        return self._files

    def rel(self, path: Path) -> str:
        """Path relative to the root, with forward slashes."""
        return path.relative_to(self.root).as_posix()

    def _folder_parts(self, path: Path) -> tuple[str, ...]:
        return path.relative_to(self.root).parts[:-1]

    def is_archived(self, path: Path) -> bool:
        return self.cfg.archive_dirname in self._folder_parts(path)

    def is_excluded(self, path: Path) -> bool:
        """True for files under a folder named in ``excluded_dirs`` (material that is not agent context)."""
        return any(part in self.cfg.excluded_dirs for part in self._folder_parts(path))

    @staticmethod
    def is_index_file(path: Path) -> bool:
        return path.name == "README.md" or path.name.endswith("-index.md")

    def text(self, path: Path) -> str:
        if path not in self._texts:
            try:
                self._texts[path] = path.read_text(errors="ignore")
            except OSError:
                self._texts[path] = ""
        return self._texts[path]

    def readme_text(self, folder: Path) -> str:
        readme = folder / "README.md"
        return self.text(readme) if readme.is_file() else ""

    def standing_paths(self) -> list[Path]:
        return [self.root / name for name in self.cfg.standing_files]

    def age_days(self, path: Path, now: float) -> float | None:
        try:
            return (now - path.stat().st_mtime) / 86400
        except OSError:
            return None

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        """Size and mtime of every markdown file, to detect writes during a read-only run."""
        entries = []
        for path in sorted(self.root.rglob("*.md")) if self.root.is_dir() else []:
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((path.relative_to(self.root).as_posix(), st.st_size, st.st_mtime_ns))
        return tuple(entries)


def find_unreachable(corpus: Corpus) -> list[str]:
    """Relative paths of files that cannot be reached within three reads of the root README."""
    root_text = corpus.readme_text(corpus.root)
    cluster_cache: dict[Path, str] = {}
    problems: list[str] = []

    def cluster_text(folder: Path, folder_text: str) -> str:
        if folder not in cluster_cache:
            texts = []
            for index in sorted(folder.glob("*-index.md")):
                if mentions(folder_text, index.name) or mentions(root_text, index.name):
                    texts.append(corpus.text(index))
            cluster_cache[folder] = "\n".join(texts)
        return cluster_cache[folder]

    for path in corpus.files():
        if path.name == "README.md":
            continue
        rel = corpus.rel(path)
        if mentions(root_text, rel) or mentions(root_text, path.name):
            continue
        folder = path.parent
        folder_rel = "" if folder == corpus.root else corpus.rel(folder)
        folder_text = corpus.readme_text(folder)
        routed = bool(folder_rel) and mentions(root_text, folder_rel)
        if routed and mentions(folder_text, path.name):
            continue
        if (routed or folder == corpus.root) and mentions(cluster_text(folder, folder_text), path.name):
            continue
        problems.append(rel)
    return problems


def find_stale_folder_indexes(corpus: Corpus) -> list[tuple[str, list[str]]]:
    """Folders whose README fails to name some sibling markdown files.

    A sibling also counts as named when a ``*-index.md`` file in the same folder
    names it, provided the folder README names that cluster file.
    """
    by_folder: dict[Path, list[Path]] = {}
    for path in corpus.files():
        by_folder.setdefault(path.parent, []).append(path)
    problems: list[tuple[str, list[str]]] = []
    for folder, members in sorted(by_folder.items()):
        if folder == corpus.root or not (folder / "README.md").is_file():
            continue
        readme = corpus.readme_text(folder)
        clusters = "\n".join(
            corpus.text(m) for m in members
            if m.name.endswith("-index.md") and mentions(readme, m.name)
        )
        missing = [
            m.name for m in members
            if m.name != "README.md" and not mentions(readme, m.name) and not mentions(clusters, m.name)
        ]
        if missing:
            problems.append((corpus.rel(folder), sorted(missing)))
    return problems
