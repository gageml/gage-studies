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
