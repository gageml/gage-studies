#!/usr/bin/env python3
"""Answer questions from questions.toml by searching past sessions, once per arm.

Usage: run.py --dataset DIR [--arm ARM] [-d TEXT] (N... | --all)    start a new run
       run.py --run ID [--arm ARM] [N... | --all]    resume or extend a run
       run.py --list                              list the runs

A run is one pass over a set of questions. It has an ID and a directory,
runs/<ID>, that holds run.json and one session for each case. A case is one
question under one arm. Resuming a run does only the cases that are not done.
"""

import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
# An arm is one configuration that each question is run under. Every arm has
# Claude Code's default tools. An arm differs in whether Gage is installed
# and in which hint, if any, the agent is given.
ARMS = {
    "default-claude": {"gage": False, "hint": None},
    "with-rg": {"gage": False, "hint": "rg"},
    "with-gage": {"gage": True, "hint": None},
}
SESSIONS = Path.home() / ".claude" / "projects"
MODEL = "opus"
# Added to every question. The score scanner checks for the evidence.
PROMPT_SUFFIX = (
    "Cite the session ID and a line number or a quote for your answer.\n\n"
    "Keep your response brief."
)
# A hint is given as a system prompt addition, the way a project's CLAUDE.md
# would give it, so that the user prompt is the same in every arm
HINTS = {
    "rg": f"Past sessions are JSONL files under {SESSIONS}. "
    "Use ripgrep (rg) to search them.",
}
# The gage that `cargo install` builds: a release build in Cargo's bin
# directory. Naming it keeps a development build elsewhere on PATH from
# being measured by accident.
CARGO_HOME = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
GAGE = CARGO_HOME / "bin" / "gage"
GAGE_HOME = Path(os.environ.get("GAGE_HOME", Path.home() / ".gage"))
# Where `gage init` writes the plugin, and the name Claude Code gives the
# plugin's query tool
PLUGIN = GAGE_HOME / ".plugin-marketplace"
GAGE_QUERY = "mcp__plugin_gage_gage__query"


def mounts(dataset: Path) -> list[str]:
    """The start of a bwrap command that puts a dataset in place of the live
    sessions and the live Gage home.

    bwrap gives the command its own mount namespace. Nothing changes for any
    other process, and the live files are untouched. A dataset is a directory
    that make_dataset.py builds: `projects` is a snapshot of session files and
    `gage` is a Gage home whose store and indexes were built from it.
    """
    return [
        "bwrap",
        "--dev-bind",
        "/",
        "/",
        # Read-only: Claude Code otherwise adds a directory for each
        # working directory it runs in
        "--ro-bind",
        str(dataset / "projects"),
        str(SESSIONS),
        # Writable: a Gage query takes lock files and rewrites its index
        # manifests. It does not change the store or the indexes.
        "--bind",
        str(dataset / "gage"),
        str(GAGE_HOME),
    ]


def sandbox(cmd: list[str], dataset: Path, empty_file: str, empty_dir: str) -> list[str]:
    """Wrap a command so that it sees the dataset, and sees as empty the
    files and directories of this study that hold answers."""
    wrapped = mounts(dataset)
    answers = [
        HERE / "questions.toml",
        # responses to the same questions, from this run and from others
        *HERE.glob("runs*"),
        *HERE.glob("projects*"),
    ]
    for path in answers:
        if path.is_dir():
            wrapped += ["--ro-bind", empty_dir, str(path)]
        elif path.exists():
            wrapped += ["--ro-bind", empty_file, str(path)]
    return wrapped + cmd


def read_questions() -> dict[int, str]:
    data = tomllib.loads((HERE / "questions.toml").read_text())
    return {q["id"]: q["question"].strip() for q in data["question"]}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Answer questions by searching past sessions, once per arm."
    )
    ap.add_argument(
        "questions",
        type=int,
        nargs="*",
        metavar="N",
        help="question index in questions.toml (repeatable)",
    )
    ap.add_argument("-a", "--all", action="store_true", help="run every question")
    ap.add_argument(
        "--arm",
        choices=list(ARMS),
        help="run under this arm only (default: every arm of the run)",
    )
    ap.add_argument(
        "--run",
        metavar="ID",
        help="resume or extend this run (default: start a new run)",
    )
    ap.add_argument(
        "-d", "--description", default="", help="description of a new run"
    )
    ap.add_argument(
        "--dataset",
        metavar="DIR",
        type=Path,
        help="dataset directory for a new run, as built by make_dataset.py",
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="cases to run at once (default: 1). Cases run at once compete "
        "for the machine, so their times are not valid measurements.",
    )
    ap.add_argument("--list", action="store_true", help="list the runs and exit")
    args = ap.parse_args()
    if args.list:
        return args
    if not args.questions and not args.all and not args.run:
        ap.error("give a question index, --all, or --run ID")
    if args.run and (args.description or args.dataset):
        ap.error("--description and --dataset apply to a new run only")
    if not args.run and not args.dataset:
        ap.error("a new run needs --dataset DIR")
    return args


# Runs


def gage_version() -> str:
    """The version of the installed gage, which every arm with Gage runs."""
    if not GAGE.exists():
        raise SystemExit(f"{GAGE} not found; see README.md for how to install it")
    out = subprocess.run([str(GAGE), "--version"], capture_output=True, text=True)
    return out.stdout.strip()


def read_dataset(path: Path) -> dict:
    """What a run records about its dataset: where it is and what
    make_dataset.py wrote about it."""
    path = path.resolve()
    about = path / "dataset.json"
    if not about.exists():
        raise SystemExit(f"{path} is not a dataset; build one with make_dataset.py")
    return {"path": str(path), **json.loads(about.read_text())}


def new_run(description: str, dataset: Path) -> dict:
    run = {
        "id": uuid.uuid4().hex[:8],
        "description": description,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL,
        "gage": gage_version(),
        "dataset": read_dataset(dataset),
        "prompt_suffix": PROMPT_SUFFIX,
        "hints": HINTS,
        "arms": [],
        "questions": [],
    }
    (RUNS / run["id"]).mkdir(parents=True)
    write_run(run)
    return run


def find_run(prefix: str) -> dict:
    matches = [p for p in RUNS.glob(f"{prefix}*") if (p / "run.json").exists()]
    if len(matches) != 1:
        found = ", ".join(sorted(p.name for p in matches)) or "none"
        raise SystemExit(f"--run {prefix} must name one run; found: {found}")
    return json.loads((matches[0] / "run.json").read_text())


def write_run(run: dict) -> None:
    (RUNS / run["id"] / "run.json").write_text(json.dumps(run, indent=2) + "\n")


def session_dir(run: dict, arm: str, index: int) -> Path:
    # Gage reads runs/<ID> as a session source, with one project per arm and
    # question: projects/<arm>-<question>/<session id>.jsonl
    return RUNS / run["id"] / "projects" / f"{arm}-{index}"


def results_of(session: Path) -> list[dict]:
    """The result events of a session file, in order. A session has more
    than one when the agent ended a turn while a background task was still
    running and Claude Code resumed it afterward."""
    results = []
    for line in session.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            results.append(event)
    return results


def result_of(session: Path) -> dict | None:
    """The final result event of a session file, or None when the session
    did not finish."""
    results = results_of(session)
    return results[-1] if results else None


def failure_of(result: dict | None) -> str | None:
    """Why a session does not count as a completed case, or None when it does."""
    if result is None:
        return "no result: the session did not finish"
    if result.get("is_error") or result.get("subtype") != "success":
        return f"the session ended with an error: {result.get('subtype')}"
    if not str(result.get("result") or "").strip():
        return "the session ended with an empty response"
    return None


def is_done(run: dict, arm: str, index: int) -> bool:
    return any(
        failure_of(result_of(s)) is None
        for s in session_dir(run, arm, index).glob("*.jsonl")
    )


LOG_LOCK = threading.Lock()


def log(run: dict, event: str, **fields) -> None:
    """Append one event to the run's log, runs/<ID>/log.jsonl. The log is the
    record of what the script did: each invocation, each case with its
    outcome, and each summary."""
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    with LOG_LOCK, (RUNS / run["id"] / "log.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


def list_runs() -> None:
    runs = [json.loads(p.read_text()) for p in RUNS.glob("*/run.json")]
    rows = [("ID", "Created", "Model", "Questions", "Cases", "Description")]
    for run in sorted(runs, key=lambda r: r["created"], reverse=True):
        # a question is done when it is done under every arm of the run
        done = sum(
            all(is_done(run, arm, q) for arm in run["arms"]) for q in run["questions"]
        )
        cases = sum(
            is_done(run, arm, q) for q in run["questions"] for arm in run["arms"]
        )
        created = datetime.fromisoformat(run["created"]).astimezone()
        rows.append(
            (
                run["id"],
                created.strftime("%Y-%m-%d %H:%M"),
                run["model"],
                f"{done}/{len(run['questions'])}",
                f"{cases}/{len(run['questions']) * len(run['arms'])}",
                run["description"],
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)), r[5], sep="  ")


# A case is one question under one arm


def ask(run: dict, arm: str, index: int, question: str) -> bool:
    """Run one case, log it, and print its line. True when the case completed."""
    gage, hint = ARMS[arm]["gage"], ARMS[arm]["hint"]
    prompt = f"{question}\n\n{run['prompt_suffix']}"
    message = json.dumps(
        {"type": "user", "message": {"role": "user", "content": prompt}}
    )
    directory = session_dir(run, arm, index)
    directory.mkdir(parents=True, exist_ok=True)
    # A session left by a failed attempt is not a result. It is kept, outside
    # the directories that Gage reads, so that the failure can be examined.
    for failed in directory.glob("*.jsonl"):
        kept = RUNS / run["id"] / "failed" / directory.name
        kept.mkdir(parents=True, exist_ok=True)
        failed.rename(kept / failed.name)
    session = directory / f"{uuid.uuid4()}.jsonl"
    cmd = [
        "claude",
        "-p",
        "--model",
        run["model"],
        # The prompt goes in on stdin so that Claude Code echoes it into
        # the stream, which otherwise holds no user prompt
        "--input-format",
        "stream-json",
        "--replay-user-messages",
        "--output-format",
        "stream-json",
        "--verbose",  # required by stream-json
        "--session-id",
        session.stem,
        "--no-session-persistence",
        "--setting-sources",
        "",  # no user settings, hooks, or plugins
        # Claude Code's own rules decide what the default tools may do.
        # They may read only the working directory unless another is granted.
        "--add-dir",
        str(SESSIONS),
    ]
    if gage:
        # Gage as `gage init` installs it: a plugin with the query server and
        # the skills. The dataset's Gage home holds the plugin. An MCP tool
        # needs a grant; the default tools do not.
        cmd += ["--plugin-dir", str(PLUGIN), "--allowedTools", GAGE_QUERY]
    if hint:
        cmd += ["--append-system-prompt", run["hints"][hint]]

    start = time.monotonic()
    with (
        session.open("w") as out,
        tempfile.NamedTemporaryFile() as empty_file,
        tempfile.TemporaryDirectory() as empty_dir,
    ):
        proc = subprocess.run(
            sandbox(cmd, Path(run["dataset"]["path"]), empty_file.name, empty_dir),
            input=message + "\n",
            text=True,
            stdout=out,
            stderr=subprocess.PIPE,
            check=False,
        )
    wall = time.monotonic() - start

    result = result_of(session)
    reason = failure_of(result)
    if reason is None and proc.returncode != 0:
        reason = f"claude exited with status {proc.returncode}"
    # Bash and Read search the session files. A Gage call is a query. Any
    # other tool, such as a lookup of a tool's definition, is not a search.
    calls = {"bash": 0, "read": 0, "gage": 0, "other": 0}
    tool_errors = 0
    for line in session.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # a system event's message is a string; a turn's message is an object
        turn = event.get("message")
        content = turn.get("content") if isinstance(turn, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block["name"]
                kind = "gage" if name.endswith("gage__query") else name.lower()
                calls[kind if kind in calls else "other"] += 1
            elif block.get("type") == "tool_result" and block.get("is_error"):
                tool_errors += 1
    # Turns, tokens, and denials are reported per result event, so they are
    # summed. The cost is cumulative, so the last one stands.
    results = results_of(session)
    result = result or {}
    turns = sum(r.get("num_turns") or 0 for r in results)
    tokens_in = sum(
        (r.get("usage") or {}).get(k, 0)
        for r in results
        for k in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    tokens_out = sum((r.get("usage") or {}).get("output_tokens", 0) for r in results)
    denied = sum(len(r.get("permission_denials") or []) for r in results)
    # The session file states its own wall time, which no result event
    # holds: a result leaves out the wait for a background task. The score
    # scanner reads this entry.
    with session.open("a") as out:
        summary = {"type": "case_summary", "wall_ms": round(wall * 1000)}
        out.write(json.dumps(summary) + "\n")
    log(
        run,
        "case",
        arm=arm,
        question=index,
        session=session.stem,
        status="failed" if reason else "ok",
        reason=reason,
        exit=proc.returncode,
        seconds=round(wall, 1),
        turns=turns,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        usd=result.get("total_cost_usd"),
        calls=calls,
        # a tool error is a call that returned an error; a denied call is one
        # of them, refused by Claude Code before it ran
        tool_errors=tool_errors,
        denied=denied,
        stderr=proc.stderr[-2000:],
    )

    label = f"{session.stem[:8]}  {arm} q{index}"
    if reason:
        # the file stays for inspection; the next attempt moves it to failed/
        print(f"{label}: FAILED after {wall:.0f}s: {reason}; see {session}")
        return False
    print(
        f"{label}: {wall:.0f}s, {turns} turns, "
        f"{tokens_in} tokens in, {tokens_out} tokens out, "
        f"${result.get('total_cost_usd', 0):.2f}, "
        f"{calls['bash']} Bash, {calls['read']} Read, {calls['gage']} Gage, "
        f"{calls['other']} other calls, "
        f"{tool_errors} tool errors of which {denied} denied",
        flush=True,
    )
    return True


def main() -> None:
    args = parse_args()
    if args.list:
        list_runs()
        return

    questions = read_questions()
    run = (
        find_run(args.run)
        if args.run
        else new_run(args.description, args.dataset)
    )

    # What this invocation asks for. A resumed run with nothing named asks
    # for what the run already holds.
    if args.all:
        asked = list(questions)
    elif args.questions:
        unknown = [q for q in args.questions if q not in questions]
        if unknown:
            raise SystemExit(f"questions.toml has no question {unknown[0]}")
        asked = list(dict.fromkeys(args.questions))
    else:
        asked = list(run["questions"])
    arms = [args.arm] if args.arm else (run["arms"] or list(ARMS))

    # The run records every question and arm it was asked for
    run["questions"] = sorted(set(run["questions"]) | set(asked))
    run["arms"] = [a for a in ARMS if a in set(run["arms"]) | set(arms)]
    write_run(run)

    print(f"Run {run['id']}")
    log(run, "invoked", questions=asked, arms=arms, jobs=args.jobs)
    counts = {"ok": 0, "failed": 0, "already done": 0}
    todo = []
    for index in asked:
        for arm in arms:
            if is_done(run, arm, index):
                counts["already done"] += 1
            else:
                todo.append((arm, index))
    # Cases run at once compete for the machine, so their times are longer
    # than those of cases run one at a time. The log records the job count.
    with ThreadPoolExecutor(max(1, args.jobs)) as pool:
        outcomes = pool.map(lambda c: ask(run, c[0], c[1], questions[c[1]]), todo)
        for completed in outcomes:
            counts["ok" if completed else "failed"] += 1

    # The state of the whole run, not only of what this invocation asked for
    missing = [
        f"{arm}-{q}"
        for q in run["questions"]
        for arm in run["arms"]
        if not is_done(run, arm, q)
    ]
    log(run, "summary", **counts, missing=missing)
    print(
        f"{counts['ok']} ok, {counts['failed']} failed, "
        f"{counts['already done']} already done"
    )
    if missing:
        print(f"Run {run['id']} is incomplete. Missing: {', '.join(missing)}")
        print(f"See runs/{run['id']}/log.jsonl. Do not score the run until it is complete.")
        raise SystemExit(1)
    print(f"Run {run['id']} is complete: every case has a response.")


if __name__ == "__main__":
    main()
