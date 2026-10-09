# Architecture

A versioned beacon stores controlled goal fields with a canonical hash. A local SQLite WAL store owns mission mode, epoch/session writer identity, rollover state, owner messages, effect records and append-only events. Checkpoint validation requires explicit fields; independent verification compares goal hash/version, Git HEAD, snapshot bytes and evidence hashes/age.

The supervisor drives a session change through these states: requested, quiescing, checkpoint-ready, read-only successor-prepared, ownership-transferred, successor-active, predecessor-retired. The transfer increments the writer epoch with compare-and-swap and converts in-flight effects to OUTCOME_UNKNOWN. A successor must prove it is fresh before it is granted ownership. Unknown effects are not automatically replayed.

Codex uses a JSON-RPC app-server adapter; Claude Code uses background CLI sessions and generated per-session hooks (experimental). Native observe-only hooks collect telemetry and re-anchor after compaction without owning session replacement.

A sandboxed worker that cannot write to the state directory can hand fenced requests to a supervising process through a worker inbox directory named by `RLH_WORKER_INBOX`. The supervising process validates each request before it touches the ledger.

The core makes no network connections.
