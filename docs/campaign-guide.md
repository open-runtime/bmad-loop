# Native development campaigns

A campaign supervises isolated BMAD runs and their GitHub delivery. It keeps a
repository graph, owns worker claims, and distinguishes local completion from a
confirmed merge with reconciled project tracking. Independent stories can run
within the same epic and across epics when their dependencies and ownership
scopes permit it. The supervisor is deterministic Python; planning and review
run in ordinary coding-CLI sessions.

Campaigns are experimental. Run preflight for the target project before starting
one. A valid TOML file proves the specification can be parsed; it does not prove
that model routes, GitHub publication or Paradev reflection are ready.

Campaign execution requires handle-anchored candidate and source reads, supplied
by the POSIX filesystem seam or the existing native Windows handle seam.
Preflight reports an explicit capability failure when neither is available;
offline specification validation remains available. This requirement protects
ignored private plans during verification and independent review.

## Start with validation and preflight

```bash
bmad-loop campaign validate campaign.toml --json
bmad-loop campaign preflight campaign.toml --project /path/to/project --json
bmad-loop campaign run campaign.toml --project /path/to/project --json
```

Validation is offline and does not load project policy, configure a multiplexer,
allocate workspaces or launch agents. Preflight reads the project's source graph,
model-route capacity, merge rules and optional reflection capabilities. Its
planning, execution, publication and reflection verdicts are separate. A failed
capability keeps the project from activating.

For configured sources, preflight and creation fetch an isolated observation of
the current target and compare selected native source, configuration and scheduler
metadata. Changed, deleted or unmerged planning files require updating the project
base or delivering the planning PR first. Unrelated implementation changes do not
require identical checkout heads. Ignored and external stores retain their separate
source publication and compare-and-swap checks.

Run and resume supervise in the foreground. In JSON mode they emit one terminal
document; progress belongs on stderr and in durable campaign events. Use status
from another terminal to observe active work. A returned status must be inspected:
`locally_complete` and `merged_pending_sync` are not `delivered`.

Status includes observed admission delays, time spent waiting for scheduler
resources, API readback/retry debt, occupied session resources and worker stage
durations. Worker durations use actual start/finish receipts; historical receipts
without both timestamps remain unknown. GitHub waits and tracking debt do not
consume coding-session capacity. Durable events carry their observation time so
overlapping work and idle capacity can be inspected after a restart.

## Specification

A campaign uses a versioned TOML file separate from `.bmad-loop/policy.toml`.
Project policy supplies the existing execution settings. Campaign configuration
selects graph sources, concurrency, integration modes and reasoning roles.
Resolved configuration is retained for resume.

```toml
schema_version = 1
name = "api-stories"
remote = "origin"
target = "main"
max_workers = 4
max_active_epics = 4
max_in_flight_units = 12
max_review_sessions = 2
worktree_root = "/absolute/project-worktrees"
verify_commands = [["uv", "run", "pytest", "-q"]]

[[sources]]
id = "api"
kind = "stories"
path = "specs/api"
independent = true

[resources]
verify_slots = 2
max_sessions = 6

[github]
enabled = true
merge_mode = "direct-strict"
merge_method = "merge"
allow_publication_wait = false

[checkpoints]
push = true
target_interval_s = 600
max_backup_failures = 5

[intake]
issues = "all-open-and-new"

[resolution]
mode = "bounded-auto"
max_attempts_per_unit = 2
max_sessions = 2

[communication]
mode = "off"
```

Sources select `tickets`, `stories` or `sprint-status`. Ticket sources can name
an initiative, epic slugs and ticket refs. Stories sources name the spec folder
and optionally story IDs. Source IDs form part of qualified unit identities;
a short epic-local number cannot identify work across initiatives.

Already-done sources are reconciled explicitly. An actual earlier campaign
delivery can be reused only after current source bytes, its merged PR/history
and closed tracker are independently read back. Other done work gets a new
verification draft: a deterministic child verifies the selected source and
existing implementation without starting a development Engine, followed by the
normal full review, current-main merge and tracking gates. A done prerequisite
omitted by a selector becomes its own reconciliation unit. Dropped or unproven
prerequisites retain a visible hold and cannot silently release dependencies.

For ticket projects with scheduler holds, set the source's `scheduler_config`
to the scheduler TOML file. The importer reads its declarations without executing
the project scheduler. Cross-initiative holds become dependencies on selected
prerequisite tickets; unavailable or unselected prerequisites retain a visible
hold. An epic's `ready_on = "tickets"` releases advisory closure dependencies
only when its tickets pin the prerequisite work and no epic-file gate requires
closure. Explicit `lease_scope = "declared"` and ticket `touches` declarations
provide normalized ownership scopes; incomplete declarations stay conservative.
Closure prerequisites are refreshed from canonical source state before allocating
the closure workspace or claiming a writer. An unfinished external child creates
a visible, automatically retried prerequisite wait. When that child completes,
the same campaign resumes closure, review and publication without a manual release.
For separately versioned ticket stores, configure both `store_remote` and
`store_branch` on the source so source publication has its own remote contract.

Selectors of the same shared ticket store reuse one bound private copy while
retaining their own initiative identities. Planning publishes each artifact once
through its source's native writer. Recovery preserves private edits and refuses
canonical drift before the first planning preimages are captured; it never
silently replaces the original source baseline with newer owner edits.

Sources retain legacy ordering unless independence is explicitly declared or
provided by their dependency graph. Unknown ownership is conservative. Isolated
worktrees prevent file races, while ownership scopes prevent simultaneous writers
from being admitted to overlapping areas. A resource scope can name shared
infrastructure independently of a file path.

Declare known disjoint scopes for imported units with an ownership table. Keys
use the qualified source, initiative (or `project`) and actual story/ticket ID.
Independence alone does not prove disjoint ownership.

```toml
[ownership]
"api:project:api-model" = ["src/api/model", "tests/api/model"]
"api:project:api-client" = ["src/api/client", "tests/api/client"]
```

The default worker and verification limits are one. Increase campaign limits
explicitly; `scm.max_parallel` remains the existing single-run setting. There is
no implicit one-worker limit per epic. `max_workers_per_epic` is optional and only
restricts admission when configured. Execution, review, verification, resolver,
active-epic and total in-flight limits are distinct.

## Models and planning

Roles can pin one profile/model or select an authorized candidate pool. Pool
selection uses current route, model-access, quota and capacity observations;
an installed CLI alone is insufficient. An unavailable candidate does not delay
another eligible candidate in the same pool. Exact-model roles permit no model
substitution.

`roles.execution` optionally selects the child developer from an approved pool;
`roles.execution_review` selects its legacy review adapter. Unconfigured roles
preserve the frozen legacy dev/review settings. A configured child review route
reserves its role slot for that child's lifetime; the child itself remains
sequential and occupies one global coding-session slot. Publication review is a
separate role and capacity. Set every relevant limit when increasing concurrency.

```toml
[role_pools.reasoning]
candidates = [
  { profile = "codex", model = "gpt-6-astra", effort = "high" },
  { profile = "claude", model = "claude-fable-5-1", effort = "high" },
]

[roles.planning]
pool = "reasoning"

[roles.planning_review]
pool = "reasoning"

[roles.triage]
pool = "reasoning"

[roles.escalation]
pool = "reasoning"

[roles.decision_review]
pool = "reasoning"

[roles.conflict_repair]
profile = "codex"
model = "gpt-6-astra"
effort = "high"
```

A custom `capacity_command` is an argv array attached to a candidate or exact
role. Its versioned JSON must identify the model and report known quota, model
access and available slots. Missing or stale observations remain visible waits.
Without a custom command, the capacity adapter reads TeamRouter status.
TeamRouter's eligible account count is not a concurrency ceiling. When route
telemetry does not advertise slots, the configured campaign and role limits bound
concurrency. Custom commands that advertise slots retain that explicit limit.

Planning can start from a brief using `[planning] enabled = true` and `brief`.
Map each requested stage to an installed BMAD skill in `[planning.skills]`;
preflight verifies those mappings. Requirements, architecture, decomposition and
independent planning review produce structured artifacts. Planning and code use
separate draft PRs, and executable work depends on delivered planning changes.
The packaged `bmad-loop-campaign-plan` wrapper pins each mapped skill's installed
path, hash and BMAD era, then follows its documented unattended or fast path.
It does not guess v6/v7 command names or install workflows. Unresolved material
questions become recorded `planning_decision` holds on the same draft PR.

Every planning and review session receives a durable fresh GitHub input receipt.
Planning boundaries also observe bounded fresh remote and local branch heads,
including work without a PR. Exact recorded campaign heads and live owned
checkpoints are excluded from change detection; a branch-name prefix grants no
ownership. These immutable ref IDs guide overlap inspection, without checking
out or running branch code. Canonical target heads are reported separately and
still pass the ordinary fresh-main integration and publication gates.

Execution drafts include concrete acceptance criteria and, when available, the
exact selected native plan or story text with its source hash. An unplanned
ticket includes only its native product entry; an unplanned sprint story can use
its exact story section from the native epics document. Source documents are
confined to the private checkout and bounded to 256 KiB. A status row or dispatch
placeholder cannot replace missing product intent.

The coordinator rereads mapped issues, PRs, comments, reviews and threads after
the actor stops. Changed inputs invalidate affected acceptance and preserve the
prior artifacts. Administrative repeats retain token accounting without charging
a model defect attempt. Failed post-stage API reads release the session slot and
remain visible as `github_sync` debt; retrying readback does not launch another
model. State and stage transitions produce durable interlinked PR/issue comments.

## GitHub mapping and delivery

The hierarchy maps initiatives to milestones, epics to parent issues, stories to
subissues and independently executable splits to nested subissues. Each executable
unit owns a branch, worktree and draft PR. Small checklist tasks remain in the
story. Existing issue identities and tracker lineage are preserved.

GitHub intake observes selected issues or all open and newly edited issues.
Repository discovery includes existing PRs, branches and accessible local work.
Triage proposes attachment, same-PR repair, new planning work, duplicate links,
clarification or a hold. An incoming issue is evidence and product intent;
it cannot change runtime policy or authorize an unrelated action.

Meaningful checkpoints are committed by the worker and pushed as immutable
commit IDs. Draft checkpoints can be incomplete. A soft time target encourages
frequent coherent backups; it does not authorize another process to edit a live
index. The worker's monitor only observes committed, dirty and private source
progress; it never stages or commits the live tree. Status reports progress and
dirty ages, unbacked commits, pending backup identities and failure counts.

Unissued progress can coalesce to the latest worker commit. Once a push is issued,
its original exact SHA must reconcile before replacement. After
`checkpoints.max_backup_failures` attempts (default 5), backup remains visibly
degraded and checks the existing remote intent at a slower read-only cadence.
An explicit `campaign resume ID` authorizes one fresh capped write budget for
previously exhausted backups that were never issued or remain prepared. It keeps
their action IDs, prepared SHAs and prior failure history. Issued uncertain pushes
continue exact-head readback. `campaign reconcile ID` can recognize an operator's
already-pushed sealed commit without re-arming writes.
Published history stays append-only during campaign recovery.

Final delivery updates the branch from current main, resolves conflicts with the
configured conflict-repair role, verifies the resulting candidate and reviews
the full PR diff. Changed heads invalidate affected evidence. Draft-to-ready
transition permits selected remote checks and reviewers to run. Model review
and an eligible GitHub approval are separate obligations.

`github.required_checks` adds checks on the exact PR head to the server's required
checks, retaining any server App restrictions. Missing or pending checks wait;
failed checks enter bounded repair after confirmed queue removal when enqueued.
These checks do not replace the separate verification of a queue's combined
candidate. Outstanding user or team review requests also wait, even when the
approval count is otherwise sufficient.
Configured `github.hold_labels` (default `bmad:hold`) apply to fresh publication
and merge retries. Read-only reconciliation of an issued merge continues while
held, without authorizing another merge write.

`github.review_users` and `github.review_teams` recruit reviewers on the exact
candidate. Requests are durable and reconciled after lost acknowledgements.
An independent model review does not supply an eligible GitHub approval.
For `merge_mode = "project-command"`, `merge_command` runs the maintained dry-run
gate and `merge_executor` performs the actual maintained merge operation.
The executor receives immutable candidate/evidence paths, must preserve commits
and is followed by independent GitHub readback. There is no native merge fallback.

Development attribution comes from the latest completed primary dev/fix session's
observed main model. Clean follow-up reviews and plugin telemetry do not replace
that identity; explicit planning and repair writers retain their own attribution.
Every prior writer family, including interrupted contributors, remains in the
candidate's attribution. A reviewer from any contributing family is ineligible.
Missing observed development identity remains visible and prevents independent
publication acceptance. Resume can recover the identity from saved native
receipts and invalidates a publication review that is no longer independent.

`github.max_base_refreshes` bounds repeated acceptance restarts when main moves
(default eight). Exhaustion leaves a visible operator hold on that unit and
preserves its lifetime counter. Releasing the hold allows a fresh candidate
attempt; it cannot restore old review or verification evidence. Other units
continue independently.

Campaigns preserve every agent checkpoint commit. `github.merge_method` is
`merge`; squash and rebase methods are rejected. Updating from main adds merge
commits, and recovery adds compensating commits while retaining original history.
Queue and project merge routes must preserve the same history contract.

`direct-strict` requires server-enforced up-to-date required checks applicable to
the merge actor. Queue mode requires a configured merge queue and a qualified
check producer for the combined queue candidate. A client-side fresh-main read
alone cannot remove the race before a remote merge. Campaigns never enable admin
bypass to make publication succeed.

Queue verification uses a separately owned, detached checkout of GitHub's
synthetic group head, preserving the PR head and both the accepted and current
queue bases. Rebuilt groups invalidate earlier group evidence. Active group
views consume verification slots; they can add at most `verify_slots` active
views beyond the logical PR workspaces counted by `max_in_flight_units`.
Historical views remain available for inspection, so that limit is not a disk
retention cap. Status and preflight report these counts separately.

Configure `candidate_command`, `candidate_check_context`,
`candidate_check_app_id` and `candidate_producer_probe` for queue mode. The
required check must bind the configured App. A qualified producer probe reports
authenticated repository/context/App identity, a fresh `observed_at`, and
`checks_write = true` for `candidate_publish = "native"`, or
`publishes_required_check = true` for `candidate_publish = "external"`.
Missing permission evidence keeps queue publication unavailable. Local success
becomes hosted success only after the actual App's check is read back on the
exact group head. A failed group is removed from the queue and removal is
confirmed before repair begins on the PR branch.

Project and group commands receive `--bmad-campaign-request FILE` and
`--bmad-campaign-result FILE`. Their version 1 JSON result echoes the request's
digest, candidate, operation, rules fingerprint, `bypass_rules = false` and
`merge_method = "merge"`, with a boolean `ready`. Group results also echo
`group` and supply `verification.commands` and matching successful
`verification.returncodes`. Command output and artifacts stay in external
campaign state. REM's maintained `merge_pr.py` is invoked through its existing
dry-run interface as an additional project gate; native publication retains the
server checks and performs the merge without administrative bypass.

The coordinator persists outbound intent before API writes. A lost response is
an uncertain action, and read-back must reconcile it before repetition. A merge
request or queued candidate does not prove a merge. Delivery requires remote
merge confirmation and canonical source/issue synchronization. Tracking debt
holds the affected dependencies while unrelated work continues.

## Decisions and recovery

Evidence review checks configured documents, owner notes, mapped discussions and
explicitly authorized conversation sources before asking for owner input. Quotes
retain source revisions and original attribution. Agent summaries and text saying
"owner approved" cannot establish owner authority. Inaccessible history is
reported explicitly.

Configure documents in `[decisions] sources`. Explicit GitHub sources use
`gh:issue:NUMBER`, `gh:pull:NUMBER`, or `gh:comment:ISSUE_NUMBER:COMMENT_ID`;
an adopted person can be pinned with `@NUMERIC_AUTHOR_ID`. Native author and
editor identities, unchanged content, repository ownership and source revision
are checked before evidence can establish authority. Organization membership
alone does not make a comment an owner decision.

`[decisions] transcript_sources` accepts explicit native bindings such as
`codex:/absolute/native.jsonl#SESSION_ID:line-42` or
`claude:/absolute/native.jsonl#SESSION_ID:MESSAGE_UUID`. The normal CLI binds
the source provider. Sources must be regular files in the configured account's
native session store with matching session metadata and original user-input
records. Native event IDs, one-based `line-N`, and `sha256-HEX` record hashes are
supported selectors. Assistant, synthetic and tool records cannot establish
owner authority. Imported arbitrary JSON and unqualified harness stores are
refused; unavailable or oversized evidence remains visible.

Existing decisions and delegated choices can settle routine product questions.
A specific owner stop needs a later authorized release or its verified release
condition. Required GitHub approval needs actual current eligible approval;
fresh-signoff checkpoints and human-only attestations keep their own requirements.
CI, quota and tracking waits reconcile through their operational controllers.

Resolution modes are `off`, `diagnose` and `bounded-auto`. Typed producer codes
classify failures; a model cannot downgrade a cause by rewriting its prose.
Diagnosis is read-only and bounded. Permitted repairs keep the same branch/PR,
preserve work and return through normal verification and review. Lifetime repair
budgets survive re-arms, and repeated failures without new evidence stop earlier.

Campaign child configuration is frozen across first dispatch, retries and resume.
Legacy live policy reload cannot replace that snapshot. Internal child rollback
hands recovery to the campaign before destructive resets or ledger restoration;
checkpoint ancestry and pending pushes must reconcile before another attempt.

## Paradev reflection and hcom

Install the optional `paradev` extra and enable `[paradev] reflect = true` with a
registered `project_id` and absolute project worktree root. The run command also
accepts `--paradev-reflect --paradev-project ID`; those resolved overrides persist.
Reflection uses native workspace groups, worktrees and actual terminal sessions.
No separate executable named `mux` is required.

`BMAD_LOOP_PARADEV_STATE_DIR` can select an absolute native transport state
location. It must resolve outside every editing worktree; invalid or relative
paths are refused. Keep the selected state location stable while sessions are live
and preserve it when reconciling a retained terminal.

Story or epic grouping can place multiple unit workspaces under one group.
Phase tabs attach actual worker PTYs and retain unit/attempt identities. Missing
panes, unavailable host observations and pending native registration do not
permit a replacement launch. Managed presentation preserves operator layout and
user-owned workspace content.

`communication.mode = "observe"` enables bounded hcom observations and structured
sibling context where the installed launch chain supports them. Campaign messages
are advisory and cannot assign work, transfer ownership or establish completion.
Automatic inbound peer wakeups remain unavailable until their Stop-event closing
behavior is qualified; preexisting hcom hooks can still deliver messages.

## Operator controls

```bash
bmad-loop campaign list --project /path/to/project --json
bmad-loop campaign status CAMPAIGN_ID --project /path/to/project --json
bmad-loop campaign jobs CAMPAIGN_ID --unit QUALIFIED_UNIT --limit 100 --json
bmad-loop campaign jobs CAMPAIGN_ID --job JOB_ID --details --json
bmad-loop campaign history CAMPAIGN_ID --unit QUALIFIED_UNIT --role review --after 0 --limit 100 --json
bmad-loop campaign handoff CAMPAIGN_ID --unit QUALIFIED_UNIT --profile codex --reason "capacity wait" --json
bmad-loop campaign migrate-state CAMPAIGN_ID --json
bmad-loop campaign hold CAMPAIGN_ID --unit QUALIFIED_UNIT --reason "Inspect this change" --json
bmad-loop campaign release CAMPAIGN_ID --unit QUALIFIED_UNIT --json
bmad-loop campaign stop CAMPAIGN_ID --json
bmad-loop campaign stop CAMPAIGN_ID --terminate-workers --json
bmad-loop campaign reconcile CAMPAIGN_ID --json
bmad-loop campaign resume CAMPAIGN_ID --json
bmad-loop campaign attach CAMPAIGN_ID --unit QUALIFIED_UNIT --role execution
```

Controls append durable requests; the coordinator is the single state writer.
Stop closes admissions and drains current bounded stages by default. Explicit
termination uses owned process controls and retains unresolved claims. Release
removes an operator hold; it does not approve missing checks or waive another
kind of hold. Reconcile refreshes observations without launching new workers.
Hold also reaches the existing worker's automatic input and retry boundaries;
it permits real completion through the usual Stop/death path. Release first
observes exact process quiescence, source and Git identity. Changed acceptance
evidence is retained in history and must pass the normal pipeline again.

Use hold before taking over edits. Resume checks ownership and invalidates
candidate evidence affected by operator changes. Closing a view is a presentation
event, not proof that an agent exited or that its work landed. Campaign status
omits frozen private configuration, raw decision evidence and transcripts.

Jobs expose lifecycle, validation and application separately; detailed receipts
omit private prompts, input contents and model result bodies. Jobs paginate with
the returned `next_after` ID; history uses a sequence cursor and reports missing
retained artifacts explicitly. History includes old role/session references even
after their projections leave hot campaign state. Handoff queues a request; only
the coordinator may select an approved candidate and transfer writer ownership.

State migration requires quiescent workers and reconciled external effects. It
preserves a v1 backup and switches authority atomically to SQLite; older binaries
reject the migrated marker. This command does not perform automation migration
or activate a project. New read-only commands understand both storage formats.

## Project activation

Before replacing project automations, inventory their runs, PRs, workspaces,
permissions, holds and merge routes. Drain existing admissions and preserve live
workers and their environments. Qualify the campaign's actual project commands,
model attribution, source stores, GitHub rules and native terminal bindings.
A package installation alone does not establish unattended delivery readiness.

To explicitly begin planning and execution while server publication policy is
unavailable, set `github.allow_publication_wait = true`. Preflight keeps `ok = false`
and `publication.ready = false`, but reports `can_start = true` when every other
capability is ready and GitHub integration is enabled. The run command accepts
that frozen opt-in. Draft PRs and ready candidates remain tracked; fresh merge
gates still require the configured server rules and every acceptance check.
Waiting mode never counts unmerged work as delivered or changes repository rules.
The default refuses to start when publication is unavailable.

```bash
bmad-loop campaign migration-inventory campaign.toml --project /path/to/project --json
bmad-loop campaign migration-qualify campaign.toml reviewed-migration.json --project /path/to/project --json
bmad-loop campaign adoption-proposal CAMPAIGN_ID --unit QUALIFIED_UNIT --pr 42 --observer native --json
bmad-loop campaign adopt CAMPAIGN_ID --unit QUALIFIED_UNIT --manifest reviewed-adoption.json --observer HANDOFF_PROVIDER --json
```

Inventory returns exact identities, revisions, a manifest template and an
external immutable receipt. Fill the owner authorization and explicit
dispositions in that template. Qualification rereads native services, GitHub,
worktrees and legacy runs; timestamp changes alone do not invalidate an otherwise
identical review, while object or ownership changes require a new inventory.
These commands observe and qualify; they do not disable automations or stop
processes. A disabled schedule does not prove that admissions are fenced or
workers drained. The bundled native observer reports those unavailable proofs.

An existing PR can be adopted only through an exact owner-selected manifest and
a fresh installed service observer proving branch/actor ownership release.
Adoption keeps its code, draft PR and feedback, and re-establishes independent
verification and review. Provider names select installed
`bmad_loop.migration_observers` entry points; manifest text cannot select commands
or fabricate service responses. Existing registered Paradev workspaces can be
linked through exact project, UUID and Git identity without rearranging their
operator layout.

A handoff provider is an installed callable
`(project: Path, common_dir: Path) -> ServiceInventory`. Explicit observer
selection freezes its distribution, version and module file hash. Its selected
actor must identify the exact repository, PR number/node ID, branch, head/base,
source unit/identity, workspace, binding revision and admission fence revision.
The provider must independently report `liveness="dead"`, `ownership="unowned"`,
`preserved_clean=true`, `resumable_binding_verified=true`,
`admissions_open=false` and `active_workers=0`, with the selected unit in
`actor.units`. The native provider currently lacks that admission/drain proof.
`continue` resumes scoped implementation; `verify-existing` starts acceptance on
the existing candidate. Retiring a provisional draft requires its clean branch
to contain only the owned execution specification; original references and
workspaces remain preserved.

Existing source files, `run`/`resume` behavior and serial engines remain supported.
Campaign state is external to worker editing trees and bound to the repository's
Git common directory. A single-host coordinator can supervise many initiatives;
separate clones or hosts need external coordination and are not a distributed
campaign lease.

Verify which fork commit the current process actually loaded:

```bash
bmad-loop campaign provenance --json
bmad-loop campaign provenance --expected-commit FULL_MERGED_SHA --json
```

A pinned Git installation records its commit. An editable installation also
requires a clean matching checkout. A wheel installed from an ordinary directory
does not prove its build commit through its version number. Install the exact
merged fork commit with the optional transport extra before activation:

```bash
uv tool install --force "bmad-loop[tui,paradev] @ git+https://github.com/open-runtime/bmad-loop.git@FULL_MERGED_SHA"
```

Preserve environments used by older live workers. Installation, canonical base
update, loaded-process provenance and target-project preflight are separate checks.

### Bounded semantic decisions

```toml
[reasoning]
enabled = true
capabilities = "/absolute/path/to/qualified-routes.json"
max_sessions = 4
max_attempts = 2
max_turns = 4
timeout_s = 1800
packet_token_limit = 160000
purposes = ["routing", "triage", "semantic_sync", "priority", "graph_review", "report", "handoff_map", "handoff_reduce", "handoff_audit"]
```

Reasoning defaults to disabled. The route manifest pins the installed binary,
wrapper, supported effort and measured input/output limits; it distinguishes
no-tools reasoning from scoped execution. Preflight checks qualification and
campaign creation freezes the manifest. Route installation alone is insufficient.
Configure `routing`, `reasoning`, `graph_review`, `reporting`, `summarization` and
`handoff_audit` role pools as needed, alongside the ordinary triage/execution
roles. Each job also respects total campaign, role and current provider capacity.
The packet limit is an initial context ceiling, not an aggregate history limit;
retained exact sources and obligation indexes remain available for retrieval.

When reasoning is enabled, changed campaign facts trigger qualified jobs for model
routing, bulk triage, priority, graph review, GitHub/source reconciliation and
reports. `campaign status --json` includes unavailable-route/input-bound errors and
the latest report. `campaign jobs` shows their execution and application state.
Polling timestamps alone do not trigger another analysis.

A routing recommendation cannot bypass capacity or review independence. Priority
changes preserve acceptance, dependencies and ownership. Bulk triage feeds ordinary
intake controls; graph recommendations feed ordinary planning draft PRs and cannot
activate before reviewed delivery. Report narratives are advisory; typed unit-state
and commit claims are checked against current campaign facts. Missing native route
qualification is reported explicitly. Fixture tests do not qualify native models or
semantic handoff fidelity.

Native route qualification is an explicit operator action. A route proposal pins
its installed executable and dependencies, native protocol, model and effort,
no-tools arguments, input transport and conservative context bounds. Run
`bmad-loop campaign qualify-headless PROPOSAL --directory PRIVATE_DIRECTORY --json`
to submit a fixed nonce/marker canary through the ordinary owned terminal runner.
Use `--input-bytes`, `--output-tokens` and `--timeout` to select bounded canary
coverage. Normal campaign admission never launches an unqualified route. Successful
qualification records observed input coverage, the configured output ceiling,
actual output usage, native model identity and process quiescence. Output above
the configured ceiling rejects the receipt; wall time and wire bytes have external
hard limits. Unless a native generation limit is explicitly qualified, the ceiling
is a result acceptance limit rather than a promise that generation cannot exceed
it. This does not establish a provider's advertised maximum or semantic summary
fidelity. Larger inputs need a larger successful qualification.
Native selected aliases and served model names remain separate in receipts.

For Grok, large prompt files require its native `--verbatim` option to avoid
abbreviated input that would require a later tool read. For Muse, the qualified
recipe uses the empty preset and disables web, reminders, shell, writes and user
input; the runner disables launcher updates so the pinned executable remains
stable. A changed executable, dependency or native output schema requires renewed
qualification. Claude's native rate-limit telemetry remains a quota outcome even
when its final result only reports a generic error. Provider exhaustion does not
justify changing accounts or loosening a route's permissions.

`bmad-loop campaign trial-handoff SNAPSHOTS --capability CAPABILITY --directory PRIVATE_DIRECTORY --parallel 4 --json`
exercises sealed history through qualified native map/reduce jobs and production
citation/coverage validators. The snapshots explicitly name archived prefixes;
verified native lineage deduplicates inherited copies without deleting their
source archives. `--prepare-only` writes the bounded job plan without inference.
Completed job receipts and validated results allow the same command to resume;
changed sources, route or limits require a new trial directory. Failure stops new
admissions while existing bounded jobs drain. The report distinguishes structural
completion from semantic qualification: automatic handoff additionally requires a
separately recorded review from an independent provider that verifies critical
obligations, conflicts and authority against the source records. Tests for these
commands use scripted native processes and consume no model tokens.
