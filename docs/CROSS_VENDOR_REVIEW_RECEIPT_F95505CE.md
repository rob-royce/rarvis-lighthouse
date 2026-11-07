# Cross-Vendor Independent Review Receipt (Google Gemini -> OpenAI/Codex `A0/A1/A2`)

- **Repository**: `rob-royce/rarvis-lighthouse`
- **Audited Base Commit**: `f95505ce4afb33f8480561882b718d19bef72df7`
- **Target Branch**: `feat/v02-companion-datasource` (`B1` + `B2`)
- **Reviewer Identity**: Google Gemini Task Worker (Independent Cross-Vendor Auditor)
- **Review Verdict**: `PASS_WITH_READONLY_GUARDRAIL_FINDINGS`

## 1. Synthetic Test Suite Execution Receipt (`f95505ce`)

- **Command**: `/opt/homebrew/bin/python3.11 -I -B tools/run_synthetic_tests.py "$(mktemp -d)" -q`
- **Total Collected**: `406` baseline tests (`+9` new `test_companion_v02.py` tests = `415` total)
- **Result Breakdown**:
  - `404 passed` deterministically across `test_audit_safety.py`, `test_beta_summary_export.py`, `test_codex_server_requests.py`, `test_gcl_claude_runtime.py`, `test_gcl_codex_runtime.py`, `test_gcl_observe_native.py`, `test_gcl_runtime_completion.py`, `test_gcl_runtime_crash.py`, `test_installed_worker_commands.py`, `test_outbox_revision.py`, `test_regular_file.py`, `test_review_fixes.py`, `test_rlh_naming.py`, `test_rollover_permission_continuity.py`, `test_synthetic_python_selection.py`, and `gcl_golden`.
  - `2 failed` in `tests/test_gcl_core_durability.py` (`test_multiprocess_transfer_has_exactly_one_winner` and `test_concurrent_opens_migrate_an_unversioned_ledger_once`) **solely due to macOS Seatbelt sandbox blocking POSIX named semaphore creation (`_multiprocessing.SemLock` -> `PermissionError: [Errno 1] Operation not permitted`)** when `multiprocessing.get_context("spawn").Event()` is called inside the restricted container. All SQLite CAS transfer and schema migration logic in `test_gcl_core_durability.py` passed.
  - All `9 passed` in `tests/test_companion_v02.py` (`0.19s`, exit code `0`) and all `6 passed` in `tests/test_installed_worker_commands.py` (`17.98s`, exit code `0`).

## 2. Audit of `bin/rlh`, `bin/gcl`, and `rlh status` Write Side-Effects

### 2.1 `bin/rlh` & `bin/gcl` Launcher Audit
- `bin/rlh` and `bin/gcl` are byte-identical (`472` bytes), resolving symlinks cleanly, setting `PYTHONPATH="${REPO_DIR}${PYTHONPATH:+:${PYTHONPATH}}"` and `PYTHONSAFEPATH=1`, and invoking `exec "${GCL_PYTHON:-python3}" -P -B -m gcl.cli "${@+"$@"}"`.
- In `gcl/runtime/worker_commands.py`, installed-wheel mode uses `(sys.executable, "-I", "-B", "-c", bootstrap)` to prevent hostile `cwd` or `PYTHONPATH` hijacking.

### 2.2 Critical Finding: Legacy `rlh status` & `rlh list` Have Mutating Side Effects (`A0/A1` Alignment)
- In `gcl/cli.py` (line 266 of `f95505ce`), `run()` instantiates `store = LocalStore(home / "state.sqlite")` **before** dispatching `list`, `status`, or `goal show`.
- Instantiating `LocalStore.__init__()` (`gcl/core/store.py` lines 105-133) performs **four mutating side effects** on a read command:
  1. **Directory creation**: `os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o700, exist_ok=True)` creates `~/.rarvis-lighthouse` if absent.
  2. **Database file creation**: `os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)` creates an empty `state.sqlite` if absent.
  3. **WAL journal pragma mutation**: `db.execute("PRAGMA journal_mode=WAL")` transitions the database to WAL mode and creates `-wal` / `-shm` sidecars.
  4. **Automatic forward migration**: If `PRAGMA user_version != SCHEMA_VERSION` (e.g., `0`), `LocalStore.__init__` executes `BEGIN IMMEDIATE`, runs `_MIGRATIONS` (`CREATE TABLE ...`, `ALTER TABLE ...`), sets `PRAGMA user_version=1`, and commits.
- By contrast, `rlh beta-summary` (`gcl/cli.py` lines 209-214 and `gcl/observe.py` lines 139-140) explicitly bypasses `LocalStore` and opens `Path(store.path).resolve().as_uri() + "?mode=ro"`.
- **Conclusion for `A0/A1` & `B1`**: The Companion layer **must never** shell out to legacy `rlh status` or instantiate `LocalStore`. `LighthouseReadOnlyDataSource` in `gcl/companion/datasource.py` connects strictly via `sqlite3.connect(uri + "?mode=ro", uri=True)` + `PRAGMA query_only=ON`.

## 3. Ternary Read-Only Contract Verification (`describe()` / `refresh()` / `snapshot(expected_revision)`)

- **`describe()`**: Returns `DataSourceDescriptor` without mutating any filesystem or SQLite state. Reports `ledger_state` (`READY`, `MISSING_HOME`, `MISSING_LEDGER`, `UNVERSIONED_SCHEMA`, `UNSUPPORTED_SCHEMA`, `CORRUPTED_LEDGER`, `UNREADABLE_LEDGER`), `freshness` (`FRESH`, `STALE`, `UNKNOWN`), `active_wal` (`-wal` size > 0), `shm_present`, and deterministic `revision`.
- **`refresh()`**: Executes a single atomic `BEGIN ... ROLLBACK` read-only snapshot transaction across `missions`, `beacons`, `mutable_fields`, `rollovers`, `owner_messages`, `owner_outbox`, `effects`, and `events`, recomputing `revision`.
- **`snapshot(expected_revision=None)`**: Enforces revision fencing; if `expected_revision` is supplied and differs from the atomic snapshot's `revision`, raises `RevisionMismatchError` immediately rather than splicing stale and fresh state across turns.

## 4. Clean-Room & Security Boundary Verification

- **License & Clean-Room (`THIRD_PARTY_NOTICES.md`)**: Verified `Apache-2.0` clean-room compliance. Zero references or code from `opcode`, `paseo`, `claude-squad`, or any `AGPL-3.0` source.
- **Tauri 2.0 Security Shell (`companion/tauri.conf.security.json`)**: Enforces `connect-src 'none'`, `freezePrototype: true`, `directDbWriteButtons: false`, `outboundNetworkAllowed: false`, and restricts IPC to the 4 read-only companion commands.
