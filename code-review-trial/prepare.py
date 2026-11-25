#!/usr/bin/env python3
"""Prepare one commit for the code-review-trial scanner.

A scanner task cannot run a process, so it cannot ask git for a commit.
This script does that work ahead of the scan. It exports the repository
as of the commit, writes the commit's diff, message, and session under
`.trial/` in the export, and adds the export to the trial dataset as a
`commit-tree` attachment whose target is the session that made the
commit.

Run it in the repository, with the project store in place:

    .gage/scanners/code-review-trial/prepare.py <commit>
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

DATASET = "code-review-trial"
ATTACHMENT = "commit-tree"

# The files of the export that the attachment holds. An attachment
# holds at most 10 MiB and 1000 files, so the website, the images, and
# the lock file are left out.
INCLUDE = [
    "**/*.rs",
    "**/*.rn",
    "**/*.toml",
    "**/*.j2",
    "**/*.md",
    "**/*.py",
    "**/*.sh",
    "justfile",
    ".trial/*",
]
EXCLUDE = ["website", "target", "node_modules"]

# Lines of unchanged text around each change in the diff
DIFF_CONTEXT = 10


def run(*args, cwd=None, stdin=None):
    done = subprocess.run(
        args, cwd=cwd, input=stdin, capture_output=True, text=True
    )
    if done.returncode != 0:
        sys.exit(f"{' '.join(args)}\n{done.stdout}{done.stderr}")
    return done.stdout


def gage_json(sql):
    out = run("gage", "query", "-f", "json", sql)
    # A development build of gage prints a cargo line before its output
    return json.loads(out[out.index("[") :]) if "[" in out else []


def commit_session(commit, subject):
    """The session that ran the commit command, and the line it ran at.

    The command is found by the commit's subject. The session record
    the reviewer reads ends on the line before it.
    """
    quoted = subject.replace("'", "''").replace("%", "").replace("_", "\\_")
    rows = gage_json(
        "SELECT session_id, line FROM tool_use "
        "WHERE name = 'Bash' "
        "AND (input_raw LIKE '%jj commit%' OR input_raw LIKE '%git commit%' "
        "OR input_raw LIKE '%jj describe%') "
        f"AND input_raw LIKE '%{quoted}%' "
        "ORDER BY result_timestamp, line"
    )
    if not rows:
        sys.exit(f"no stored session ran a commit command for {commit}: {subject}")
    sessions = {row["session_id"] for row in rows}
    if len(sessions) > 1:
        sys.exit(f"more than one session committed {subject!r}: {sorted(sessions)}")
    return rows[0]["session_id"], rows[0]["line"]


def ensure_dataset():
    out = run("gage", "dataset", "list")
    if DATASET not in out:
        run("gage", "dataset", "add", "--tag", DATASET, "--yes")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("commit")
    args = parser.parse_args()

    commit = run("git", "rev-parse", "--verify", f"{args.commit}^{{commit}}").strip()
    subject = run("git", "log", "-1", "--format=%s", commit).strip()
    session_id, commit_line = commit_session(commit, subject)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = subprocess.run(
            ["git", "archive", commit], capture_output=True, check=True
        ).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)

        trial = root / ".trial"
        trial.mkdir()
        (trial / "diff.patch").write_text(
            run("git", "show", "--format=", f"-U{DIFF_CONTEXT}", commit)
        )
        (trial / "message.txt").write_text(
            run("git", "log", "-1", "--format=%B", commit)
        )
        (trial / "commit.json").write_text(
            json.dumps(
                {
                    "commit": commit,
                    "subject": subject,
                    "session_id": session_id,
                    # The last session line the reviewer may read
                    "session_end": commit_line - 1,
                },
                indent=2,
            )
        )

        ensure_dataset()
        run("gage", "dataset", "link", DATASET, session_id, "--yes")
        exclude = [arg for pattern in EXCLUDE for arg in ("--exclude", pattern)]
        out = run(
            "gage", "attachment", "add",
            "--root", tmp,
            "--name", ATTACHMENT,
            "--key", commit,
            "--target", session_id,
            "--dataset", DATASET,
            "--yes",
            *exclude,
            *INCLUDE,
        )
    print(out.strip())
    print(f"commit   {commit[:8]} {subject}")
    print(f"session  {session_id} lines 1-{commit_line - 1}")


if __name__ == "__main__":
    main()
