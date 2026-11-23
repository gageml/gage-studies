# Gage query vs ripgrep

The Gage `query` tool provides an SQL interface to session records.

So what? Why not use something speedy like ripgrep?

This study attempts to shed some light on that. What are the trade offs?

The authors are hopeless biased and these results must be taken with that in
mind.

The methods are available here for critique and for reuse on other datasets.

The questions are specific to a private dataset and so are useless for attempts
to re-run the analysis.

## Setup

The study measures a release build of Gage. Install one from a checkout of the
Gage repository:

```shell
git clone https://github.com/gageml/gage.git
cd gage
cargo install --path gage-cli
```

This installs `gage` to `~/.cargo/bin`, which is where `run.py` looks for it.
Use `--path` as shown. The `gage-cli` crate on crates.io is a different project.

`run.py` also needs these on the `PATH`:

- `claude` ([Claude Code](https://claude.com/claude-code)), signed in
- `bwrap` ([bubblewrap](https://github.com/containers/bubblewrap)), which hides
  the answers from the agents
- `rg` and `jq`, which the ripgrep arms use

## Known issues

## Results

These numbers come from one run of 20 questions on 2026-11-22. Each question
was put to Claude Code (Opus) three times, once per arm, one case at a time:

- `default-claude` is Claude Code as installed, with no hint about how to
  search.
- `with-rg` is the same, with a hint to search the session files with ripgrep.
- `with-gage` is Claude Code with the Gage plugin and no hint.

The sessions searched are a fixed snapshot of 1,912 sessions.

| Arm              | Pass | Correct | Median seconds | Mean seconds | Median turns | Median tokens in | Mean cost | Denied calls |
| ---------------- | ---- | ------- | -------------- | ------------ | ------------ | ---------------- | --------- | ------------ |
| `default-claude` | 14   | 16      | 45             | 60           | 13           | 643k             | $0.55     | 7.2          |
| `with-rg`        | 16   | 16      | 44             | 158          | 14           | 655k             | $0.79     | 9.3          |
| `with-gage`      | 18   | 18      | 29             | 38           | 7            | 270k             | $0.32     | 0.4          |

- **Correct** is the number of cases, out of 20, whose answer matches the
  reference answer. A second agent judges each answer.
- **Pass** is the number of correct cases that also cite a session that holds
  the answer, with a quotation or a line number.
- **Seconds** is the wall time of a case.
- **Tokens in** counts every input token of a case, cached tokens included.
- **Denied calls** is the mean number of tool calls per case that were refused
  because they needed approval. A case runs without a person to approve calls.

`with-gage` compared with `default-claude`:

| Measure          | Difference |
| ---------------- | ---------- |
| Median seconds   | 36% lower  |
| Median turns     | 46% lower  |
| Median tokens in | 58% lower  |
| Mean cost        | 42% lower  |

In the `with-gage` arm, the agent searched with Gage alone in 12 cases and with
Gage and the session files in 8.

### Limits

- The difference in accuracy is not reliable. An earlier run of the same
  questions, with eight cases at a time, gave 18, 16, and 18 passes. The same
  arm moved by four cases between the two runs, which is as much as the arms
  differ from each other.
- One `with-rg` case took 1,935 seconds because the agent ran a search in the
  background and waited for it. Without that case the `with-rg` mean is 65
  seconds. The median is the better measure of time.
- The run is a single pass over 20 questions written by the authors of Gage.
