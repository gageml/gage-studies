# Correction rate

How often does the user have to correct the model, and does that differ by
model?

The data is one developer's Claude Code sessions from April to November 2026:
about 2,300 sessions over six main models, most of them work on one project.
The sessions are private. The scanner and the method are here in full.

## Method

The `correction-rate` scanner in `.gage/scanners/` does the work in two tasks.

`label_sessions` finds each session's replies. A reply is a typed user message
that follows an assistant turn. The first prompt of a session is not a reply,
and neither is anything the harness writes: interrupt markers, command output,
notifications, and the summary that opens a continued session. Finding the
replies involves no model.

Replies go to a labelling agent in batches of 25. For each reply the agent
sees the end of the assistant's turn and the reply, and records the replies
that are corrections. A correction is the user rejecting or redirecting what
the assistant just did, said, or proposed. Approval, an answer to the
assistant's question, a refinement of accepted work, and a new request are not
corrections. The whole instruction is in `label-prompt.md.j2`.

The agent supplies a line number, a kind, and a one-sentence summary. The
scanner looks up the model that was corrected and the month from the session,
so the agent cannot misstate either.

`tally` counts the notes and writes the tables. The rate is corrections over
replies. No number in a table is written by a model.

A session with a failed labelling batch is left out of the tally whole, so a
rate never has corrections without their replies.

## Running it

```
cd correction-rate
gage scan -s correction-rate SESSION...
gage scan -s 'correction-rate#{dry_run: true}' -a
```

The second form counts the replies without starting an agent and writes
nothing. A scan skips the sessions an earlier scan labelled and carries their
notes forward, so a run can be stopped and resumed.

## Known issues

- Model and time are confounded. The models were mostly used one after
  another, so a difference between two models is also a difference in the
  months, the tasks, and how the user worked.
- The labels have not been checked against hand labels. A read of 24 pilot
  corrections found them all to be corrections. Nothing yet says how many
  corrections the agent misses.
- The agent sees only the end of the assistant's turn, at most 1,800
  characters of it, and 1,500 characters of the reply.
- A session that used two models counts as a session for each.
- The labelling agent is itself a Claude model.

## Pilot

A pilot over 31 sessions, five picked for each of six models from sessions
with 6 to 30 user messages, plus one more. It checks that the scanner works
and what a run costs. It is too small to compare models, and its sessions were
picked for having a lot of back and forth.

| Model | Sessions | Replies | Corrections | Rate | After interrupt |
|---|---:|---:|---:|---:|---:|
| claude-opus-4-6 | 5 | 85 | 41 | 48.2% | 9.4% |
| claude-opus-4-7 | 6 | 70 | 27 | 38.6% | 5.7% |
| claude-opus-4-8 | 6 | 87 | 31 | 35.6% | 6.9% |
| claude-fable-5 | 5 | 43 | 19 | 44.2% | 2.3% |
| claude-fable-5-1 | 5 | 32 | 14 | 43.8% | 6.3% |
| claude-fable-5-2 | 6 | 108 | 14 | 13.0% | 1.9% |

The pilot ran 33 agents and cost $4.29, about one cent a reply. The whole
dataset has 14,643 replies in 1,278 sessions.

## Results

None yet.
