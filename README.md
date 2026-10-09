# RARVIS-Lighthouse

**Done means verified.**

Keep using your Codex. RARVIS-Lighthouse tells you what is still missing, which version the evidence belongs to, and what you need to do next.

Status: **v0.1.0 Community preview.** Expect rough edges and changes between commits.

## What it does

- Records the goal, the evidence and the check results of a mission on the supported paths. An agent saying "done" is not done: a mission completes when its `done_check` command passes or when you confirm it yourself.
- Continues across a session change with the goal, the evidence and the open items.
- Raises deterministic signals: an unauthorized change to the goal, long stretches of turns without evidence, and repeated errors.
- Makes no network connections in the core. Nothing is sent to a RARVIS-Lighthouse server.

## Support

| Agent | Status |
|---|---|
| Codex on macOS | Supported |
| Claude Code | Experimental |

Python 3.11 or newer and Git are required. Runtime code uses POSIX file locking and process semantics; Windows native execution is unverified.

## Quickstart

Install from the public repository:

```sh
pip install git+https://github.com/rob-royce/rarvis-lighthouse
```

Start a mission in a Git repository with a `done_check`. The mission completes only when the check command passes after the agent reports done:

```sh
rlh start --executor codex --cwd . --done-check "test -f proof.txt" -- "Create proof.txt containing the word done"
rlh status MISSION_ID
```

`rlh start` prints the mission id; use it in place of `MISSION_ID`. `rlh status` prints the mission's state as JSON. Read `runtime_status`: `ACTIVE` while work continues, `COMPLETED` once the check has passed.

Without a `done_check`, the agent's report only waits for you. Status shows `AWAITING_CONFIRMATION` until you confirm:

```sh
rlh start --executor codex --cwd . -- "Summarize the open TODO comments"
rlh status MISSION_ID
rlh confirm MISSION_ID
```

State lives in `~/.rarvis-lighthouse` unless you pass `--home PATH` or set `RLH_HOME`.

## Naming

The display name is RARVIS-Lighthouse. The package, the command and the state directory are `rarvis-lighthouse`, `rlh` and `~/.rarvis-lighthouse`. The Python import package is `gcl`, and the command `gcl` is an alias of `rlh`.

## Tests

```sh
python3 -m pip install -e '.[test]'
python3 -I -B tools/run_synthetic_tests.py "$(mktemp -d)" -q
```

The tests use fake executors and synthetic homes only. Do not point tests at real agent sessions.

## Trademarks

RARVIS-Lighthouse is a name used by the project owner. The Apache License 2.0 does not grant permission to use this name, related names or any logos, except as required to describe the origin of the work (Section 6).

## License and contributions

The core is licensed under the Apache License 2.0; see `LICENSE` and `THIRD_PARTY_NOTICES.md`. Outside contributions are not accepted yet. To report a vulnerability, see `SECURITY.md`.
