#!/usr/bin/env python3
"""Build a dataset from an archive of Claude Code session files.

Usage: make_dataset.py ARCHIVE [DIR]

ARCHIVE is a tar file whose top-level directory is `projects`, as made by

    tar -czf sessions.tar.gz -C ~/.claude projects

DIR is the dataset directory to create. It defaults to the archive's path
without its extension. The dataset holds:

    projects/       the session files, extracted from the archive
    gage/           a Gage home: a store of those sessions, its indexes, and
                    the Gage plugin for Claude Code
    dataset.json    the archive's name and checksum, the Gage version, and
                    how long each step took

The Gage home is built with the sessions mounted at their usual location, so
a stored session has the same ID that it would have in the live store. The
indexes are built here, once, so that no case of a run pays for them.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from run import GAGE, gage_version, mounts


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


STUB_CLAUDE = """#!/bin/sh
case "$1" in
  --version) echo "0.0.0 (stand-in)" ;;
  plugin) [ "$2" = list ] && echo "[]" ;;
esac
exit 0
"""


def timed(label: str, cmd: list[str], seconds: dict, env: dict | None = None) -> None:
    print(f"{label} ...", flush=True)
    start = time.monotonic()
    subprocess.run(cmd, check=True, env=env)
    seconds[label] = round(time.monotonic() - start, 1)
    print(f"{label}: {seconds[label]}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a dataset from an archive of Claude Code session files."
    )
    ap.add_argument("archive", type=Path, help="tar file of a projects directory")
    ap.add_argument("dir", type=Path, nargs="?", help="dataset directory to create")
    args = ap.parse_args()

    archive = args.archive.resolve()
    name = archive.name.removesuffix(".gz").removesuffix(".tar").removesuffix(".tgz")
    dataset = (args.dir or archive.with_name(name)).resolve()
    if dataset.exists():
        raise SystemExit(f"{dataset} exists; remove it to build the dataset again")
    version = gage_version()

    (dataset / "gage").mkdir(parents=True)
    seconds = {}
    timed("extract", ["tar", "-xf", str(archive), "-C", str(dataset)], seconds)
    if not (dataset / "projects").is_dir():
        raise SystemExit(f"{archive} has no top-level projects directory")

    gage = [*mounts(dataset), str(GAGE)]
    # `gage init` creates the store and writes the plugin into the Gage home.
    # It then registers the plugin with Claude Code by running `claude`. A
    # stand-in for `claude` takes those calls, so that building a dataset
    # leaves the Claude Code setup of this machine as it is.
    with tempfile.TemporaryDirectory() as stub_dir:
        stub = Path(stub_dir) / "claude"
        stub.write_text(STUB_CLAUDE)
        stub.chmod(0o755)
        env = {**os.environ, "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"}
        init = [*gage, "init", "-y", "--driver", "claude", "--default", "claude"]
        timed("init", init, seconds, env=env)
    # every session in the snapshot becomes a stored session
    timed("add sessions", [*gage, "session", "add", "--all", "-y", "-N", "claude:"], seconds)
    # the text index of the stored sessions, and that of the session files
    timed("index stored", [*gage, "index", "-S"], seconds)
    timed("index native", [*gage, "index", "-N", "claude:"], seconds)

    count = subprocess.run(
        [*gage, "query", "-q", "-f", "csv", "select count(*) from session"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[-1]
    about = {
        "archive": archive.name,
        "sha256": sha256(archive),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gage": version,
        "stored_sessions": int(count),
        "seconds": seconds,
    }
    (dataset / "dataset.json").write_text(json.dumps(about, indent=2) + "\n")
    print(f"dataset: {dataset}")


if __name__ == "__main__":
    main()
