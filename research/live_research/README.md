# Live Research Runtime State

This directory is intentionally kept in git, but its generated contents are not.

The Docker research services write files such as:

- `latest.json`
- `feed.jsonl`
- `auto_explore.json`
- `l2_latest.json`
- `l2_progress.json`
- `shadow_lanes.json`

Those files are runtime state, not source code. They must stay local to the
deployment or be copied as explicit trial/research evidence when needed. Keeping
them out of git preserves deploy provenance: `git rev-parse HEAD` identifies the
code that is running, while generated research output cannot make the working
tree look like an unreviewed code change.
