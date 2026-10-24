# Voluntary beta evidence register

plan_ref: RARVIS-MASTER-PLAN-v5.1 rev6 §7 (GCL / Lighthouse), §1 P4
(evidence dimensions); approved plan sha256
`4fe79ea73934a2ddb30ba64f68e2b334bbf853e0da321b481ae4c9a5cf60b005`.
Scope: F-05 / F-20, Astra C-06 / C-10 / C-11.

reuse_check: Extend `ClaudeBackground` launch/resume metadata and the existing
supervisor successor path for permission continuity; reuse `LocalStore` missions,
rollovers and events through SQLite SELECT aggregates and the existing `rlh` CLI
for reporting. No new service or telemetry store. Executable reuse evidence lives
in `tests/test_rollover_permission_continuity.py` (launch argv and persisted
restart boundary) and `tests/test_beta_summary_export.py` (populated SQLite and
CLI export).

## Voluntary offline export

Run against your existing local state directory:

```sh
rlh --home "$RLH_HOME" --json beta-summary --support-minutes 15 --false-positives 1 --key-takeaway-present
```

Or call `gcl.observe.export_beta_summary(store, support_minutes=0,
false_positives=0, key_takeaway_present=False)` with a `LocalStore`.
The command prints JSON for local review; sharing it is voluntary and separate.
It makes zero network calls and does not create or migrate a ledger. The ledger
must already exist. No upload or automatic enrollment occurs.

Schema `gcl.beta_summary.v1` exports only mission/completion/rollover counts,
distinct UTC activity-day count, repeat-use qualification, support minutes,
false positives, and a boolean takeaway marker. Activity means a ledger event
belonging to an existing mission; completion means a `mission.completed` or
`mission.succeeded` event, counted once per mission. Rollover counts include
requested/aborted rollovers. Repeat use qualifies with at least two activity days
or at least two missions. Support minutes and false positives must be
non-negative integers. Record whether a concrete takeaway changed the next
action using the boolean; the takeaway text stays local.

`privacy_mode=STATS_ONLY_NO_CONTENT_NO_PATHS`: no mission/session identifiers,
paths, prompts, credentials, event bodies, exact dates, or takeaway text are
exported. Counts can still reveal usage volume; review before voluntarily sharing.

## G-L2 acceptance register

Keep one participant row per consenting real target user; avoid personal names.
Do not infer distinct users from distinct missions or exports.

| Participant alias | Target user confirmed | Repeat use qualified | Support minutes | False positives | Concrete takeaway changed next action |
| --- | --- | --- | --- | --- | --- |
| user-01 | pending | pending | 0 | 0 | false |

G-L2 requires **>=3 target users** and **>=2 repeat users**, with support minutes
and false positives recorded, plus evidence of concrete takeaways that changed
next actions. Retain consent and evidence pointers under the beta owner's
control. Repeated exports from one person are not additional users.

## C5 graduation boundary

Unit tests and export tooling do not substitute for real-user G-L2 evidence
or the D4 Mac live rollover acceptance sign-off. Passing offline fixtures proves
only the exercised code paths. This implementation does not grant graduation,
production rollout, or live Mac acceptance; those require their own evidence
and explicit sign-off.
