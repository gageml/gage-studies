#!/usr/bin/env python3
"""Answer questions from questions.toml by searching past sessions, once per arm.

Usage: run.py [--arm ARM] [-d TEXT] (N | --all)    start a new run
       run.py --run ID [--arm ARM] [N | --all]    resume or extend a run
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
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
# An arm is one configuration that each question is run under: whether the
# agent has Bash, whether it has the Gage query tool, and whether it is told
# where past sessions are
ARMS = {
    "rg": {"bash": True, "gage": False, "hint": False},
    "rg-hint": {"bash": True, "gage": False, "hint": True},
    "gage": {"bash": False, "gage": True, "hint": False},
    "both": {"bash": True, "gage": True, "hint": False},
}
SESSIONS = Path.home() / ".claude" / "projects"
MODEL = "opus"
# Added to every question. The score scanner checks for the evidence.
PROMPT_SUFFIX = (
    "Cite the session ID and a line number or a quote for your answer.\n\n"
    "Keep your response brief."
)
# Given to a hinted arm as a system prompt addition, the way a project's
# CLAUDE.md would give it, so that the user prompt is the same in every arm
HINT = f"Past sessions are JSONL files under {SESSIONS}."
# The session in which the questions and answers were written. Its
# transcript holds every answer, so it is hidden wherever it is stored.
AUTHORING_SESSION = "4b3c858a-4f39-40b6-ad9f-3a3221adf495"
GAGE = Path.home() / "Code/gage/target/debug/gage"  # the binary the Gage plugin runs
READ_ONLY = [
    "rg",
    "jq",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "cut",
    "ls",
    "cat",
    "grep",
    "tr",
]


def hidden() -> list[Path]:
    """The files and directories that hold answers. Every arm sees each one
    as empty."""
    return [
        HERE / "questions.toml",
        # responses to the same questions, from this run and from others
        RUNS,
        *HERE.glob("projects*"),
        # the transcript, and the directory of its subagents and tool results
        *SESSIONS.glob(f"*/{AUTHORING_SESSION}.jsonl"),
        *SESSIONS.glob(f"*/{AUTHORING_SESSION}"),
    ]


def hide(cmd: list[str], empty_file: str, empty_dir: str) -> list[str]:
    """Wrap a command so that it sees each hidden path as empty.

    bwrap gives the command its own mount namespace. Nothing changes for any
    other process, and the real files are untouched.
    """
    wrapped = ["bwrap", "--dev-bind", "/", "/"]
    for path in hidden():
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
        "question",
        type=int,
        nargs="?",
        metavar="N",
        help="question index in questions.toml",
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
    ap.add_argument("--list", action="store_true", help="list the runs and exit")
    args = ap.parse_args()
    if args.list:
        return args
    if args.question is None and not args.all and not args.run:
        ap.error("give a question index, --all, or --run ID")
    if args.run and args.description:
        ap.error("--description applies to a new run only")
    return args


# Runs


def new_run(description: str) -> dict:
    run = {
        "id": uuid.uuid4().hex[:8],
        "description": description,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL,
        "prompt_suffix": PROMPT_SUFFIX,
        "hint": HINT,
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


def result_of(session: Path) -> dict | None:
    """The final result event of a session file, or None when the session
    did not finish."""
    for line in reversed(session.read_text().splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            return event
    return None


def is_done(run: dict, arm: str, index: int) -> bool:
    return any(result_of(s) for s in session_dir(run, arm, index).glob("*.jsonl"))


def list_runs() -> None:
    runs = [json.loads(p.read_text()) for p in RUNS.glob("*/run.json")]
    rows = [("ID", "Created", "Model", "Questions", "Description")]
    for run in sorted(runs, key=lambda r: r["created"], reverse=True):
        # a question is done when it is done under every arm of the run
        done = sum(
            all(is_done(run, arm, q) for arm in run["arms"]) for q in run["questions"]
        )
        created = datetime.fromisoformat(run["created"]).astimezone()
        rows.append(
            (
                run["id"],
                created.strftime("%Y-%m-%d %H:%M"),
                run["model"],
                f"{done}/{len(run['questions'])}",
                run["description"],
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)), r[4], sep="  ")


# A case is one question under one arm


def ask(run: dict, arm: str, index: int, question: str) -> None:
    """Run one case and print its line."""
    bash, gage, hint = (ARMS[arm][k] for k in ("bash", "gage", "hint"))
    mcp_servers = {"gage": {"command": str(GAGE), "args": ["mcp"]}} if gage else {}
    allowed_tools = [f"Bash({c}:*)" for c in READ_ONLY] if bash else []
    if gage:
        allowed_tools.append("mcp__gage__query")
    prompt = f"{question}\n\n{run['prompt_suffix']}"
    message = json.dumps(
        {"type": "user", "message": {"role": "user", "content": prompt}}
    )
    directory = session_dir(run, arm, index)
    directory.mkdir(parents=True, exist_ok=True)
    # a session left by an interrupted attempt is not a result
    for unfinished in directory.glob("*.jsonl"):
        unfinished.unlink()
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
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps({"mcpServers": mcp_servers}),
        "--tools",
        "Bash" if bash else "",
        "--allowedTools",
        *allowed_tools,
    ]
    env = dict(os.environ)
    if hint:
        cmd += ["--append-system-prompt", run["hint"]]
    if bash:
        # drop ~/.local/bin so that Bash cannot call the gage command
        env["PATH"] = os.pathsep.join(
            p for p in env["PATH"].split(os.pathsep) if not p.endswith("/.local/bin")
        )
        cmd[0] = str(Path.home() / ".local/bin/claude")
        # Bash may read only the working directory unless another is granted
        cmd += ["--add-dir", str(SESSIONS)]

    start = time.monotonic()
    with (
        session.open("w") as out,
        tempfile.NamedTemporaryFile() as empty_file,
        tempfile.TemporaryDirectory() as empty_dir,
    ):
        subprocess.run(
            hide(cmd, empty_file.name, empty_dir),
            env=env,
            input=message + "\n",
            text=True,
            stdout=out,
            check=False,
        )
    wall = time.monotonic() - start

    label = f"{session.stem[:8]}  {arm} q{index}"
    result = result_of(session)
    if result is None:
        # the file stays for inspection; the next attempt removes it
        print(f"{label}: FAILED after {wall:.0f}s, no result; see {session}")
        return
    calls = {"bash": 0, "gage": 0}
    for line in session.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # a system event's message is a string; a turn's message is an object
        turn = event.get("message")
        content = turn.get("content") if isinstance(turn, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls["bash" if block["name"] == "Bash" else "gage"] += 1
    usage = result.get("usage", {})
    tokens_in = sum(
        usage.get(k, 0)
        for k in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    print(
        f"{label}: {wall:.0f}s, {result.get('num_turns', 0)} turns, "
        f"{tokens_in} tokens in, {usage.get('output_tokens', 0)} tokens out, "
        f"${result.get('total_cost_usd', 0):.2f}, "
        f"{calls['bash']} Bash calls, {calls['gage']} Gage calls, "
        f"{len(result.get('permission_denials') or [])} denied"
    )


def main() -> None:
    args = parse_args()
    if args.list:
        list_runs()
        return

    questions = read_questions()
    run = find_run(args.run) if args.run else new_run(args.description)

    # What this invocation asks for. A resumed run with nothing named asks
    # for what the run already holds.
    if args.all:
        asked = list(questions)
    elif args.question is not None:
        if args.question not in questions:
            raise SystemExit(f"questions.toml has no question {args.question}")
        asked = [args.question]
    else:
        asked = list(run["questions"])
    arms = [args.arm] if args.arm else (run["arms"] or list(ARMS))

    # The run records every question and arm it was asked for
    run["questions"] = sorted(set(run["questions"]) | set(asked))
    run["arms"] = [a for a in ARMS if a in set(run["arms"]) | set(arms)]
    write_run(run)

    print(f"Run {run['id']}")
    for index in asked:
        for arm in arms:
            if is_done(run, arm, index):
                continue
            ask(run, arm, index, questions[index])


if __name__ == "__main__":
    main()
