#!/usr/bin/env python3
"""Answer questions from questions.toml by searching past sessions, once per arm.

Usage: run.py [--arm ARM] (N | --all)
"""

import argparse
import json
import os
import subprocess
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
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
# Given to a hinted arm as a system prompt addition, the way a project's
# CLAUDE.md would give it, so that the user prompt is the same in every arm
# The session in which the questions and answers were written. Its
# transcript holds every answer, so it is hidden wherever it is stored.
AUTHORING_SESSION = "4b3c858a-4f39-40b6-ad9f-3a3221adf495"


def hidden() -> list[Path]:
    """The files and directories that hold answers. Every arm sees each one
    as empty."""
    return [
        HERE / "questions.toml",
        # earlier responses to the same questions
        PROJECTS,
        *HERE.glob("projects-*"),
        # the transcript, and the directory of its subagents and tool results
        *SESSIONS.glob(f"*/{AUTHORING_SESSION}.jsonl"),
        *SESSIONS.glob(f"*/{AUTHORING_SESSION}"),
    ]


# Asked of every arm, because the score scanner checks for it
EVIDENCE = "Cite the session ID and a line number or a quote for your answer."
HINT = f"Past sessions are JSONL files under {SESSIONS}."
MODEL = "opus"  # every run uses the same model
# Gage reads this directory as claude:HERE, with one project per arm and question:
# projects/<arm>-<question>/<session id>.jsonl
PROJECTS = HERE / "projects"
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
        help="run under this arm only (default: run under every arm)",
    )
    args = ap.parse_args()
    if args.question is None and not args.all:
        ap.error("give a question index or --all")
    return args


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


def run(arm: str, index: int, question: str) -> None:
    bash, gage, hint = (ARMS[arm][k] for k in ("bash", "gage", "hint"))
    mcp_servers = {"gage": {"command": str(GAGE), "args": ["mcp"]}} if gage else {}
    allowed_tools = [f"Bash({c}:*)" for c in READ_ONLY] if bash else []
    if gage:
        allowed_tools.append("mcp__gage__query")
    prompt = f"{question}\n\n{EVIDENCE}\n\nKeep your response brief."
    message = json.dumps(
        {"type": "user", "message": {"role": "user", "content": prompt}}
    )
    session = PROJECTS / f"{arm}-{index}" / f"{uuid.uuid4()}.jsonl"
    cmd = [
        "claude",
        "-p",
        "--model",
        MODEL,
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
        cmd += ["--append-system-prompt", HINT]
    if bash:
        # drop ~/.local/bin so that Bash cannot call the gage command
        env["PATH"] = os.pathsep.join(
            p for p in env["PATH"].split(os.pathsep) if not p.endswith("/.local/bin")
        )
        cmd[0] = str(Path.home() / ".local/bin/claude")
        # Bash may read only the working directory unless another is granted
        cmd += ["--add-dir", str(SESSIONS)]

    session.parent.mkdir(parents=True, exist_ok=True)
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
    result = {}
    calls = {"bash": 0, "gage": 0}
    for line in session.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
        # a system event's message is a string; a turn's message is an object
        turn = event.get("message")
        content = turn.get("content") if isinstance(turn, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls["bash" if block["name"] == "Bash" else "gage"] += 1
    if not result:
        raise SystemExit(f"claude returned no result; see {session}")

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
        f"{session.stem[:8]}  {arm} q{index}: {wall:.0f}s, {result.get('num_turns', 0)} turns, "
        f"{tokens_in} tokens in, {usage.get('output_tokens', 0)} tokens out, "
        f"${result.get('total_cost_usd', 0):.2f}, "
        f"{calls['bash']} Bash calls, {calls['gage']} Gage calls, "
        f"{len(result.get('permission_denials') or [])} denied"
    )


def main() -> None:
    args = parse_args()
    questions = read_questions()
    if not args.all:
        if args.question not in questions:
            raise SystemExit(f"questions.toml has no question {args.question}")
        questions = {args.question: questions[args.question]}
    arms = [args.arm] if args.arm else list(ARMS)
    for index, question in questions.items():
        for arm in arms:
            run(arm, index, question)


if __name__ == "__main__":
    main()
