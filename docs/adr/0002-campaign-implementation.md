# ADR 0002 implementation contract

- **Status:** Partially implemented in the development PR; audited runtime gaps and project activation remain
- **Date:** 2026-11-05
- **Parent:** [Native SDLC campaign](0002-native-sdlc-campaign.md)
- **Delivery:** One bmad-loop PR, including code, tests, documentation and local deployment tooling

This contract resolves implementation choices in ADR 0002. The experimental
campaign targets these boundaries above the existing serial engines; audit found
runtime integration and recovery gaps that must be repaired before activation. The
[campaign guide](../campaign-guide.md) describes its current commands. Repository
policy, migration and installation qualification remain activation prerequisites.
The accepted [reasoning-job and handoff contract](0003-bounded-reasoning-and-handoff.md)
extends this work without weakening its acceptance or publication gates.

## Implementation goals and traceability

The next milestone is review-ready PR 18. Ultimate delivery additionally requires
accepted reviews, synchronization with main, history-preserving merge, exact-commit
installation, canonical-base update and qualified REM activation. Do not mark the
ultimate objective complete at the review-ready boundary.

The matrix identifies implementation responsibilities, production entry points
and acceptance evidence. Paths are existing integration seams unless marked new;
they are not claims that the extension is wired. Pending means the complete
normal-runtime scenario or qualification remains to be demonstrated. Update each
row from concrete evidence, keeping implementation, runtime-tested, native-qualified,
review-ready and activated states distinct. Qualification artifacts remain outside
tracked documentation; durable behavior and test references belong here.

| Goal / responsibility                   | Production path                                                                                  | Required regression or qualification                                                                                                                                   | Native qualification                                                       | Status                                                                                                                  |
| --------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| G0 — Upstream integration and contracts | Sanitizer, adapter/probe/mux seams; ADR 0002/0003                                                | Incoming security and reliability changes preserve fork behavior; all requirements have code/test ownership                                                            | Recheck affected installed transport behavior                              | Upstream integrated; affected regressions covered; final checks pending                                                 |
| G1 — Autonomous progress                | `campaign_runtime`, `campaign_remote_feedback`, `campaign_runtime_decisions`, scheduler/capacity | Temporary wait clears and reaches merge; new owner evidence re-arms resolution; mixed-model receipts pass; allocation, fairness and expiry use real runtime state      | Served-model and structured quota receipts                                 | Runtime progress repairs and regressions implemented; final load qualification pending                                  |
| G2 — Acceptance and publication         | Planning/source flows, `campaign_publication`, queue runtime and merge adapter                   | Criterion proof/amendments; reviewer recruitment; H/G checks; actual executor submission; uncertain writes and tracking recovery                                       | Eligible GitHub identity, required App identity and project executor       | Ledger/publication integration fixtures implemented; native qualification pending                                       |
| G3 — Durable reasoning jobs             | Store/supervisor/worker seams and new job model                                                  | Parallel job claims, asynchronous lifecycle, typed outcomes, bounded limits, restart and drain/terminate                                                               | Qualified context delivery and completion for each advertised route        | Durable jobs, SQLite and normal runtime integration implemented; final qualification pending                            |
| G4 — Harnesses and execution routing    | Profile registry, adapters and campaign worker dispatch                                          | Claude/Codex/Grok Build/AGY/Muse capability contracts; approved development pools; selected/served/auxiliary attribution                                               | Launch, context, native identity, effort, usage, stop and supported resume | Profiles, execution pools and confined headless transport implemented; route qualification incomplete                   |
| G5 — Portable handoff                   | New history readers, source indexes, reducer and handoff flow                                    | Coverage/citations, incremental caching, destination budget, worker fencing, dirty-work and pending-effect preservation                                                | Controlled corpus and sealed full-conversation trial                       | History, runtime replacement and preserved-review continuation implemented; full-history/fidelity qualification pending |
| G6 — Semantic decisions                 | Intake/planning/decision flows plus reasoning jobs                                               | Event-triggered routing, triage, semantic sync, priority, graph review and reporting; stale/unauthorized proposals rejected                                            | Approved Astra/Fable roles; Astra-only conflict repair                     | Runtime hooks and semantic fixtures implemented; native qualification pending                                           |
| G7 — Operator integration               | CLI projections, `campaign_paradev`, `campaign_communication`, decision evidence provider        | Tasks, role attach, completed history, nested diagnostics, qualified grouping, bounded hcom cursors and normal-CLI transcript binding                                  | Sidebar/tab/session identity and supported wrapper enrollment              | Runtime/CLI fixture coverage implemented; native UI qualification pending                                               |
| G8 — Scale and review readiness         | Coordinator, discovery/store, packaging and operator surface                                     | At least 48 stories/24 active epics/eight verification slots plus reasoning jobs; responsive controls; bounded hot state/context; final checks and independent reviews | Actual harness operation; explicit unrun platform/activation gates         | Load and independent review in progress; deployment gates remain separate                                               |

The nine earlier review repairs remain mandatory regressions, not newly missing
features: unknown provider concurrency uses configured limits; attribution excludes
unrelated review sessions; stop consumes terminate-workers; merge retry preserves
hold labels; configured checks and outstanding review requests block publication;
rename validation checks both paths; multiple ticket selectors preserve private
store identity; hierarchy completion waits for asynchronous effects. Preserve
checkpoint-safe append-only recovery, frozen-policy reload suppression and ordinary
macOS temporary-path socket coverage. An isolated rerun does not resolve an xdist
failure.

Use root integration plus three concurrent native workers, rotating specialists
across waves: contracts/upstream, runtime/GitHub/operator repairs, harness/history/
handoff, semantic integration/qualification/docs, then independent reviews.
Assign shared runtime/model/store/CLI edits explicitly and give workers disjoint
leaf-module/test ownership. Native subagents stay in this workspace; this does
not create Paradev side tasks. Push coherent commits to the same PR without
squashing history. Documentation status follows evidence, not agent completion.

The implemented evidence seams have focused zero-token coverage in
`test_campaign_acceptance.py`, `test_campaign_publication_extensions.py`,
`test_campaign_runtime_queue.py`, `test_campaign_handoff_context.py`,
`test_campaign_reasoning_semantics.py`, `test_campaign_operator.py`,
`test_campaign_cli.py`, `test_campaign_reflection_flow.py`,
`test_campaign_paradev.py` and `test_campaign_communication.py`.
These suites validate production dispatch and guard behavior; the native column
remains a separate qualification obligation, including final publication executor,
real installed harnesses, complete-history semantic fidelity and operator UI.

## Command and configuration contract

Add the following namespace, with injected engine, transport and clock seams:

```text
bmad-loop campaign validate SPEC --json
bmad-loop campaign preflight SPEC --json
bmad-loop campaign run SPEC [--paradev-reflect] [--paradev-project ID] [--json]
bmad-loop campaign list --json
bmad-loop campaign status ID --json
bmad-loop campaign jobs ID [--unit UNIT] [--job JOB] [--json]
bmad-loop campaign history ID [--unit UNIT] [--role ROLE] [--json]
bmad-loop campaign handoff ID --unit UNIT [--json]
bmad-loop campaign migrate-state ID --json
bmad-loop campaign resume ID [--json]
bmad-loop campaign stop ID [--terminate-workers] --json
bmad-loop campaign hold ID --unit UNIT --reason TEXT --json
bmad-loop campaign release ID --unit UNIT --json
bmad-loop campaign attach ID --unit UNIT [--role ROLE]
bmad-loop campaign reconcile ID --json
bmad-loop campaign migration-inventory SPEC --json
bmad-loop campaign migration-qualify SPEC MANIFEST --json
bmad-loop campaign adoption-proposal ID --unit UNIT --pr NUMBER [--observer NAME] --json
bmad-loop campaign adopt ID --unit UNIT --manifest FILE [--observer NAME] --json
bmad-loop campaign provenance [--expected-commit SHA] --json
```

Run/resume supervise in the foreground. JSON mode emits one terminal result;
progress goes to stderr and durable events. Operators use status in another
terminal for live observations. Stop defaults to draining: it closes admissions
while existing workers finish their current bounded stage. Terminate-workers
uses owned process/session controls and retains uncertain claims. Exit codes
remain 0, 1, 2 and 130; blocked/held details are fields, not new exit codes.
Follow machine.py for failures: a verdict command can emit a failing verdict;
an operational error outside that contract leaves stdout empty and uses stderr.

The jobs/history/handoff/migrate-state commands are required extension interfaces,
implemented with bounded operator projections. Jobs and history paginate, returning bounded receipts and
source references rather than entire transcripts. Handoff queues an idempotent
request for the coordinator using approved candidates; the CLI never launches a
replacement directly. Status exposes effective capacity, jobs, handoff coverage,
wait reason and next automatic action. Attach honors role selection; quiescent
sessions expose retained history. State migration is distinct from the existing
automation migration-inventory and migration-qualify commands.

Validate is offline and mutation-free. Preflight reads capabilities, reports
planning/execution/publication/reflection readiness separately, and never starts
an agent, creates a workspace or alters repository rules. Attach is interactive
and has no JSON promise. Release removes an explicit operator hold; it never
approves missing verification, changes a policy or adopts an uncertain process.

Use a versioned TOML specification. Version 1 has repository remote/target,
one or more source selectors, optional brief/planning stages, worker and resource
limits, ownership scopes, GitHub mode, Paradev configuration and communication
mode. Sources identify source kind plus initiative, epic and unit selectors.
Qualified unit IDs include source/initiative identity; an epic-local number alone
is not globally unique. The campaign ID is minted once and saved on creation.

Example shape, subject to parser naming consistency within the implementation:

```toml
schema_version = 1
name = "product-campaign"
remote = "origin"
target = "main"
max_workers = 4
max_active_epics = 4
max_in_flight_units = 12
max_review_sessions = 2

[[sources]]
kind = "tickets"
initiative = "initiative-example"
epics = ["epic-api", "epic-cli"]

[resources]
verify_slots = 2

[github]
enabled = true
merge_mode = "direct-strict"

[role_pools.reasoning]
candidates = [
  { profile = "codex", model = "gpt-6-astra", effort = "high" },
  { profile = "claude", model = "claude-fable-5-1", effort = "high" },
]

[roles.planning]
pool = "reasoning"

[roles.triage]
pool = "reasoning"

[roles.planning_review]
pool = "reasoning"

[roles.escalation]
pool = "reasoning"

[roles.decision_review]
pool = "reasoning"

[roles.conflict_repair]
profile = "codex"
model = "gpt-6-astra"
effort = "high"

[checkpoints]
push = true
target_interval_s = 600

[decisions]
mode = "evidence-first"

[resolution]
mode = "bounded-auto"
max_attempts_per_unit = 2
max_sessions = 2

[intake]
issues = "all-open-and-new"

[paradev]
reflect = true
project_id = "configured-project-id"
group_by = "story"
tasks = false

[communication]
mode = "off"
```

Generic defaults are conservative: one worker and one verify slot. Explicit
campaign limits enable parallelism; the legacy scm.max_parallel clamp remains.
There is no per-epic serial limit in a parallel campaign: an optional
max_workers_per_epic only constrains admission when explicitly configured.
Add every new policy default to core.toml and its reference generator. CLI
overrides are resolved once and persisted. Resume reports incompatible changes
instead of silently applying another launch environment. Continue using existing
configuration trust/digest machinery; do not invent document-hash ceremonies.

### Campaign model roles

Campaign roles are planning, planning_review, triage, escalation, decision_review,
conflict_repair and publication_review, with independently resolved profiles and budgets.
Legacy execution retains the existing dev/review stage settings; the extension
adds approved execution/recovery pools without changing that default. Generic defaults
inherit project settings; the rollout preset permits Astra through Codex or Fable
through Claude for planning, planning review, semantic triage, escalation diagnosis
and decision evidence review. The example's high effort is configurable. A role
selects either a named pool or one exact profile/model, never ambiguous mixtures.
This chat's selected model is never runtime configuration. Planning review uses a fresh session;
publication review additionally honors project family-independence rules. Astra
and Sol are both OpenAI models and cannot satisfy a cross-provider-family rule
merely by being different model IDs.

Build these roles through the existing profile/adapter factory with immutable
role snapshots, without changing legacy role inheritance. Preflight reports the
resolved launch chains, advertised models and eligible pools for each role.
Choose an available authorized candidate using account/router capacity and fair
admission, with stable configured order as the tie-breaker. Do not wait for Astra
while an eligible Fable slot is available. Availability includes route readiness,
model access, quota and campaign budget; a binary on PATH alone is insufficient.
No eligible candidate produces a visible resource wait, not permission to use an
unlisted model or account. An exact-model role still refuses substitution.
Record selected and observed model/provider separately and validate membership.
Missing or mismatching required attribution does not satisfy the role contract.
Keep a launched attempt on its selected model. A provider change after failure
starts a fresh session with saved artifacts/context and the same lifetime budget;
do not pass a Codex conversation ID to Claude or pretend it is the same session.

Conflict repair is explicitly pinned to Astra for this rollout. If Astra is
unavailable, retain that candidate's repair wait and continue unrelated work;
the planning pool's Fable alternative does not override this exact-model role.

The generic mux adapter already renders SessionSpec.effort additively for Codex
through model_reasoning_effort and for Claude through --effort. Characterize that
behavior and qualify additional native CLI/profile arguments while preserving
permission, hook and resume arguments.
Do not use extra_args in a way that replaces the profile's permission flags.
Honor client-advertised effort levels rather than assuming API and CLI catalogs
are identical. Test fresh and resumed argv and effective configuration without
spending model tokens. Project wrappers must accept the new campaign skills and
preserve explicit role selection instead of applying a dev-only retry fallback.

## State, ownership and module boundaries

Implement by responsibility, with the following public contracts agreed before
parallel editing. Names below are module boundaries, not required class names.

| Module                    | Contract                                                                                       |
| ------------------------- | ---------------------------------------------------------------------------------------------- |
| campaign_model            | Frozen specifications, unit graph, typed states and evidence coordinates.                      |
| statemachine              | Legal campaign transitions alongside, without changing, existing engine/plan transitions.      |
| campaign_store            | Atomic snapshot, coordinator lock, revision checks, outbox and command inbox.                  |
| campaign_sources          | Import/refresh sprint, stories and ticket sources; private-store preparation and publication.  |
| campaign_discovery        | Repository work inventory, coverage receipts and planning overlap evidence.                    |
| campaign_intake           | Durable remote observations, triage sessions, dispositions and bounded graph-change proposals. |
| campaign_resolution       | Typed escalation diagnosis, bounded repair actions and verified re-arm decisions.              |
| campaign_decisions        | Authorized context retrieval, decision ledger and evidence-backed hold satisfaction.           |
| campaign_planning         | Session stages, artifact validators, planning PR and graph revisions.                          |
| campaign_workers          | Spawn/reconcile isolated single-unit engines and collect final worker receipts.                |
| campaign_scheduler        | Pure admission decisions from dependencies, claims, scopes, budgets and resources.             |
| campaign_github           | Typed GitHub observations and mutations behind an injectable transport.                        |
| campaign_publication      | Sync, review/verification evidence, ready/merge/reconcile transitions.                         |
| campaign_paradev          | Registered workspace provider, managed metadata and pane projection.                           |
| adapters/paradev\_\*      | Bundled native PTY backend, host client, bootstrap and output reader.                          |
| campaign_communication    | Optional roster observations and scoped communication compatibility.                           |
| CLI / machine / documents | Commands, versioned JSON envelopes and read-only projections.                                  |

The supervisor never parses prose to decide completion. Session stages use the
existing CodingCLIAdapter contract, followed by deterministic artifact checks.
Do not run Engine instances on coordinator threads or share their Journal,
workspace, adapters or RunState.

Campaign snapshots live beneath a canonical repository-associated state root,
outside worker editing trees. Reuse existing external run-state conventions and
record the resolved root. A nonblocking OS-held coordinator lock is keyed to
the Git common directory, not the workspace path; its owner records the target.
Initially permit
one active coordinator per repository; that campaign can include many epics and
initiatives. This also serializes issue publication and cross-initiative leases.
Version 1 is a single-host supervisor bound to the canonical Git common directory.
It does not provide a distributed lease across independent clones or hosts.
Preflight/migration must account for existing external workers; remote branch
protection covers merge races, not duplicate ownership of an unfinished unit.

State contains a monotonically increasing revision, graph revision, coordinator
generation, immutable repository identity, policy snapshot, unit records,
worker claims, resource reservations, external bindings, holds and outbox rows.
Persist a transition and its outbound intents atomically before side effects.
Audit events carry revision/sequence IDs and are recoverably emitted from the
snapshot; a crash between snapshot and journal append must not lose authority.

The reasoning extension migrates this snapshot/journal authority behind the same
store boundary to transactional SQLite, moving events and job records out of the
hot snapshot. Its [migration contract](0003-bounded-reasoning-and-handoff.md#storage-and-compatibility)
preserves outbox semantics, old-format read access, identity protections and crash
recovery. No live or uncertain claim may be migrated, and an older binary must
reject migrated state rather than resume a stale snapshot.

The coordinator lock is a singleton ownership guard, not a semaphore acquired
by workers. Its event loop must not call a blocking engine, coding session,
verification command or long network retry. Run stages in owned child processes
and bounded I/O executors; consume their typed receipts in the single state
writer. Schedule independent ready work after each observation, not after an
entire epic or publication pipeline completes.

Suggested unit progression:

```text
pending -> planning -> draft -> ready -> claimed -> running -> locally_complete
        -> syncing -> verifying -> reviewing -> ready_to_publish
        -> merge_requested -> merged_pending_sync -> delivered
```

Blocked, operator-held and retry-wait are explicit conditions with reason and
resume stage; canceled is terminal and never satisfies a dependency. Local
completion is not delivery. Record the legacy run's actual outcome separately.
An externally merged PR can enter merged_pending_sync only after read-back;
missing acceptance evidence remains a reconciliation hold, never fabricated.

Worker claims record unit/attempt, coordinator generation, launch request ID,
process PID/start identity, run directory, workspace/branch and session IDs.
Persist claim before spawning. The child writes its startup binding and final
receipt to its own atomic files; only the coordinator writes campaign state.
Worker exit or exit code alone is insufficient: inspect persisted run state,
commits, holds and source artifacts. PID age/heartbeat expiry never proves death.

An orphaned worker may finish its current unit, but cannot start another unit or
publish. Restart reconciles process identity, run state and native terminals
before resuming or replacing it. Unknown launch ownership blocks that unit and
keeps its resources reserved. Multiple CLI controls append idempotent requests
to the coordinator inbox under a short lock; they do not edit its state directly.

Worker receipts are versioned and name campaign/unit/attempt/generation, run ID,
legacy outcome, base/head commits, source-change paths and native binding.
Planning results name stage, graph revision, input target and artifact paths.
Publication evidence names candidate coordinates, verification commands/results,
review verdict/findings/dispositions and actual provider-family attribution.
Cross-check each receipt against files, processes and Git; worker-written fields
are claims, not proof that a required operation succeeded.

Keep local revision locks short; never hold them across network calls or agent
sessions. Keep the OS coordinator lock for its lifetime. A durable action ID and
coordinator generation guard every effect; compare the current generation again
before publication. Reconcile adoption requires matching target/source IDs;
uncertain creates without a unique remote match remain held for explicit repair.

## Scheduling and the existing engines

Import the complete selected graph, including explicit after edges, cross-epic
edges, held work and parent closure requirements. Do not infer independence from
adjacent numbers. Missing dependency IDs and cycles fail validation. Preserve
legacy serial ordering when independence was not declared. Landed immutable
units cannot be removed or redefined by a later planning pass.

Use the ticket API's structured distinction between advisory epic declarations
and enforced gated_by edges. REM's ready_on=tickets releases a declared epic
dependency when its pinned prerequisite tickets land; an explicit epic-file gate
or an unpinned declared dependency still waits for closure. Import cross-initiative
scheduler holds and normalized ownership paths. Treating every advisory epic
line as an unconditional closure dependency would erase intended parallelism.

Admit ready units deterministically with fair epic rotation and stable source
order within equal priority, subject to global,
epic, verification and ownership limits. Multiple independent stories from one
epic are allowed and always use different branches/worktrees. A parent completes
only after its children and any required closure/retrospective unit are delivered.
Represent closure work explicitly so several children cannot each run the same
epic retrospective or shared completion writer.

Observe external closure prerequisites before allocating a broad workspace or
claiming its writer. Preserve an unfinished child as a typed, retried prerequisite
wait and refresh canonical source state until it clears. Recheck immediately
before launch; a newly discovered prerequisite produces a confirmed no-launch
receipt and releases the unused claim. An external completion must resume the
same campaign through review, merge and tracking without an operator release.

Select ownership-compatible ready work before reserving scarce workspaces, and
persist the fairness cursor with admission. An overlapping sibling cannot occupy
the final slot while disjoint work waits. Preserve source hierarchy and GitHub
mapping; add no calendar/date or sprint-capacity subsystem. Expire observations
at admission for both native and custom capacity observers. Unknown concurrency
uses configured limits; stale positive capacity is not fresh authorization.

Separate active execution workers, review sessions, verification slots, active
epics and total in-flight units/workspaces. CI waits, merge waits and tracking
retries consume no execution/review/verification slot. Keep a bounded in-flight
limit to prevent unlimited queued worktrees. A blocked unit holds only the
ownership/resources its unresolved actor still requires; unrelated epics and
stories remain admissible. Missing resource telemetry is reported explicitly.

An ownership scope names a file area, crate, proto package or explicitly shared
resource. Overlapping exclusive scopes cannot run together. Unknown scope is
conservative; read-only review is distinct from write ownership. Claims remain
until the actor is confirmed quiescent. Escaping the declared scope produces a
visible conflict and integration hold; worktrees alone prevent file races, not
semantic conflicts between branches. Verification slots are released only after
the owned verification process exits. Existing project-wide verify locks remain
authoritative where other non-campaign workers also use the machine.

Count planning, development, repair and final review against campaign budgets
and session capacity. Preserve existing account/router policies; a configured
adapter name is not proof of the provider actually used. Capacity refusals and
quota waits are resumable resource conditions, not product failures. Unknown
usage stays unknown. Bound repair/review/rebase attempts and surface exhaustion;
do not restart counters by creating another worker or PR.

Create a worker entry point internal to the package. It calls compose_run with
the exact source selectors and max_stories=1, isolation=none, an explicit run ID,
resolved profiles and frozen plugin grants. Tickets use the ticket epic slug and
ticket ref; stories mode uses spec folder and story ID; sprint uses its selector.
Characterize all three before adding the wrapper. Explicitly suppress child
epic-wide closure; the campaign's closure unit owns it.

Use a campaign-worker execution option rather than rewriting user policy files.
It forbids legacy publication/scheduler hooks and mutable shared-store writes,
but retains verification and applicable local plugin behavior. Detect conflicting
push-merge/sync-with-main ownership during preflight; do not merely rely on a
post_story hook being advisory. Disable automatic cleanup of unresolved runs.

### Frozen worker configuration

Persist the complete resolved worker policy, profiles, role selection, plugin
grants and campaign execution options outside the editing tree, identified by
the campaign configuration revision. Construct the engine, flows and adapters
from those same values. Force recovery.reload_policy=off in the effective child
policy and make Engine.\_maybe_reload_policy return before reading project files
when the trusted campaign-worker option is present. This applies before the first
dispatch, every internal retry boundary and resumed worker execution; max_stories=1
does not avoid the initial reload site. No worker may replace its model, budgets,
gates, review policy or campaign recovery rules from its editable checkout.

The existing config_digest pins the host-execution surface, not the entire
operational configuration: model, limits, gates and review edits can deliberately
pass that check. Preserve its legacy semantics and retain its trust checks;
do not use a matching digest as proof of frozen campaign settings. The trusted
campaign binding and persisted full snapshot supply that separate guarantee.
Journal the effective configuration revision and suppressed reload mode.

Every restart, resume and re-arm reconstructs this campaign context before any
engine recovery or dispatch. Direct legacy resume/resolve of a campaign child
must route through the campaign or refuse with the supported campaign command;
it must not silently reload/bless the checkout and lose its execution options.
Missing or mismatched snapshots hold recovery. Checkout configuration drift is
reported to the coordinator, never adopted by a child. An intentional policy
revision requires coordinator validation at a quiescent boundary, persisted new
bindings and invalidation of affected acceptance evidence. Ordinary non-campaign
runs keep their existing story-boundary reload and explicit resume behavior.

### Recovery without rewriting checkpoint history

Campaign branches are append-only from creation, including commits not yet
pushed. Internal child retries, defers, stops and startup/resume recovery must
not reset their branch to an earlier attempt baseline or discard/recreate the
worktree. This rule overrides inherited scm.rollback_on_failure for campaign
execution. Merely setting that flag false is insufficient: resolved re-drives,
mounted worktrees and some normalization paths select automatic rollback anyway.
Preservation refs keep objects but do not make a reset branch fast-forwardable.

Intercept recovery before destructive effects in RecoveryFlow.rollback_or_pause
and the related engine paths, including \_roll_back_rejected_attempt and
\_finish_inflight. Return a typed campaign recovery request and park the child
instead of following the legacy reset and immediate retry. Prevent the rejected
attempt's finally-block ledger restoration/cleanup from mutating source artifacts
after that handoff; include the required restoration in the recovery transaction.
Keep a defensive campaign guard at the destructive-reset seam so an overlooked
caller refuses instead of rewinding the branch. Legacy recovery stays unchanged.

The coordinator records a per-unit recovery fence and suspends new dispatch and
checkpoint push submissions for that unit. Confirm the child and coding sessions
are quiescent, then reconcile its final head, source bindings and checkpoint
receipts. Settle already-issued pushes or retain their unknown status explicitly;
never infer that an unacknowledged push did not reach GitHub. Capture prepared,
in-flight and confirmed checkpoint heads with the recovery request. Remote or
pending heads that are not ancestors of the owned current head require a hold.
No repository-wide lock is held while waiting; unrelated workers and pushes run.

Before recovery effects, persist the recovery action ID, expected parent/head,
source preimages and intended correction. Bind any recovery commit to that action;
after a crash, verify its parent and resulting tree before adopting it or retrying.
An exclusively owned recovery worker preserves the current committed head and
tracked/untracked attempt residue, then creates a compensating/revert commit on
top of that head where recovery needs a clean retry. Compute changes from the
recorded attempt baseline and owned-path/source preimages, preserving authorized
spec corrections, earlier accepted work and pre-existing files. Use source
sole-writer APIs for lifecycle/ledger restoration. Do not restore the whole old
repository tree over later work or delete unknown files. Ambiguous ownership or
an invalid preimage holds; a conflicting repair uses the Astra conflict contract.
If no content correction is needed, verify that fact without an empty commit.

The recovered head must descend from the pre-recovery head and every checkpoint
that was published or could still be published. Persist its recovery receipt and
source changes before lifting the fence. Unsent old push intents may be explicitly
superseded by that descendant; in-flight/unknown intents retain reconciliation
history. Only this transaction's authorized recovery push may pass the fence.
Confirm the recovered remote head and source state, then lift the fence before
admitting the next attempt.
Re-arm against that head with a new attempt/session generation and the same frozen
configuration, preserving prior baselines and lifetime budgets in history. A crash
at any point resumes this transaction; it cannot repeat a revert or run the legacy
rollback. Recovery failure retains the branch/PR and unresolved evidence intact.

Each worker gets a private source view. In-tree tracked plans stay on its branch.
Shared/ignored/external ticket stores are copied into an isolated store with
explicit configuration redirection; setting mark_done=never alone is insufficient
because the engine can still commit its store. Validate that ticket-tree writes
and StoreCommitter resolve only into that private view. Use existing sole-writer
APIs for final canonical publication after remote merge.

Publication applies only the unit's owned semantic changes, never a whole stale
board snapshot. Re-read canonical state under its store lock, verify the expected
source identity and merge changes to distinct units. A conflicting same-unit
change holds reconciliation. Separate versioned stores retain their own commit
and remote-publication requirements. Failure after code merge remains
merged_pending_sync; dependents do not run until that debt is settled.

## Repository discovery and avoiding duplicate work

Before initial planning, and incrementally before a planning revision or new
unit admission, assemble a repository-scoped work inventory. Intake selection
controls which issues become work, not which existing work can inform planning.
Read all open/draft PR and open issue metadata for the repository, plus mapped
work and a configured recent window of merged and closed-unmerged
PRs. Expand beyond that window for explicit links and relevant commit history.
Record each PR's repository/head/base identities, draft/merge state, linked
issues, changed paths, checks and unresolved review findings. Fetch relevant
diffs/commits after deterministic candidate selection; a title match alone is
not evidence that two units implement the same acceptance criteria.

List remote branch tips and local refs/worktrees in the selected Git common
directory. Inspect relevant unmerged diffs, merge bases, committed artifacts and
known BMAD runs/Paradev bindings; record whether another actor owns the work.
Unpublished local changes are only visible in explicitly accessible local
worktrees. Archive roots are opt-in and read-only; do not scan arbitrary disks,
revive archived workspaces, run code from an inspected branch or claim visibility
into another machine's unpushed work. Fetch into isolated observation refs through
the Git chokepoint, without checking out or modifying another worker's branch.
Do not treat an old branch or an idle terminal as proof of abandonment.

Produce a versioned inventory receipt with query scope, pagination completeness,
observed SHAs, timestamps, unavailable sources and bounded/truncated details.
Use cached unchanged objects and targeted refreshes; do not repeat a full diff
audit for every worker. A required inventory failure holds the affected planning
or admission decision. Explicitly report optional missing context; never label
a partial inventory as proof that no competing work exists.

Planning writes an overlap disposition for each proposed unit: already delivered,
covered by active work, continue existing work, reuse partial work, new work, or
unresolved overlap. Cite exact source/PR/commit identities and acceptance coverage,
including remaining gaps and intentional exclusions. Same-file changes are a
conflict signal, not automatic duplication. Deterministic delivery reconciliation
must confirm an already-delivered claim; closed-unmerged PRs and patch similarity
alone cannot satisfy dependencies. Superseding abandoned work requires a recorded
reason so later planning does not repeatedly revive rejected approaches.

An active external PR becomes an observed dependency or overlap hold, with no
second worker assigned to its covered scope. Continue the same PR automatically
only for an already-owned campaign binding or an explicitly authorized adoption
whose process/worktree ownership, clean preserved state and review obligations
reconcile. Other work gets an adoption proposal; observation is not authority to
push to a collaborator's branch. Preserve existing commits and feedback. If the
original branch cannot be safely adopted, an authorized replacement PR links the
predecessor and explains which parts it retains. No stale state files are imported
as execution authority. Revalidate ownership/head identities immediately before
claiming a unit, and invalidate affected planning evidence when they change.
External publishers do not participate in local claims; a new competing PR can
appear after discovery. Periodic reconciliation catches that overlap and holds
affected publication. Do not promise global duplicate exclusion across humans,
other hosts and independent schedulers.

## Intake and triage

Always refresh mapped issues/PRs in the selected repository. Generic intake uses
explicit selectors or configured labels. The rollout preset selects
all-open-and-new: inventory open issues at activation and observe new/edited
issues thereafter, including issues created outside BMAD. Deduplicate existing
bindings before bounded triage batches; discovery does not automatically admit
the entire backlog for implementation. Existing BMAD tracker bindings take
precedence over title matching. Distinguish issue records from PRs returned by
the issues API. Generic sources without initiatives use an explicitly configured
campaign milestone; they do not require rewriting the source hierarchy.

Poll while active and reconcile before/after planning and review, immediately
before merge and after landing. Use endpoint-specific updated-time/cursor reads
with an overlap window and complete pagination. Advance a cursor only after its
observations are durable. Recheck mapped active objects periodically: one issue
timestamp is not a complete cursor for reviews, threads, check runs or rules.
Persist repository/object IDs, source revision or updated time plus payload
digest, author, origin URL and observed content. Deduplicate identical versions;
edits create new input. Suppress feedback from the campaign's own marked comments.
Persist these inbox records separately from outbound action receipts.

Deterministic matching handles known bindings, duplicates and lifecycle facts.
Semantic triage runs as a bounded coding-CLI session, using the normal completion
contract and a validated result artifact. It proposes one of: attach to existing
work, repair the current PR, propose a new story/epic, duplicate/link, needs
information, or hold/out of scope. The result cites input IDs and versions,
existing/new unit identity, rationale, acceptance criteria and proposed
dependencies. The supervisor validates scope, policy, budget and current graph
before accepting it. No LLM call enters the supervisor control loop.

Reuse an incoming issue as the unit's GitHub binding when appropriate; do not
create a second issue merely because BMAD imported it. Missing actionable
criteria hold that item and publish a deduplicated clarification request.
Bound intake, triage attempts and newly admitted work independently of execution
capacity. A campaign has an approved scope: automatic intake within it may
proceed, while scope expansion or a project-required checkpoint becomes a visible
decision. Comment text is evidence, never executable instructions or permission
to change policy. Honor authenticated operator controls through their separately
defined trusted command path.

A required review finding repairs the same PR and retains its history; creating
a follow-up issue cannot silently satisfy that review. Nonblocking follow-ups
are allowed only under trusted project policy with an explicit disposition and
backlink. Human holds and unresolved required requests still prevent publication.
Changes to active acceptance criteria produce a versioned plan proposal, hold
affected units at a safe stage boundary and invalidate their downstream evidence.
Do not mutate a live worker's requirements or suspend unrelated units.

Accepted new intent is written to a private planning source through the existing
source-specific APIs, committed to a planning draft PR and validated as a graph
revision. Its hierarchy and backlinks synchronize to GitHub. Only after that
planning change is delivered can dependent implementation be admitted. Keep
proposed, accepted and delivered work distinct in comments and machine status.
Shared/external source publication follows the same sole-writer contract as
implementation delivery; a triage session never patches the canonical board.

## Evidence review before requesting owner input

The autonomous preset does not introduce an owner approval at every phase. Where
project policy permits, use gates.mode=none for routine child-engine checkpoints
and retain automated planning/publication review. Explicit source HITL records,
owner holds and project-required fresh approvals remain individually represented.
Do not silently turn an intentional project gate into a routine checkpoint.

Before notifying the owner about a decision or approval hold, first check whether
it is already answered, satisfied or delegated. The decision_review role uses
the available Astra/Fable pool. It receives the exact question, hold ID/type,
unit/source/head coordinates and relevant evidence, not a generic instruction
to find a reason to proceed. Deterministic reconciliation runs first for facts
such as already-recorded GitHub approval or a completed required check.

Retrieve from configured project instruction/decision documents and owner notes,
mapped GitHub issue/PR discussions and approved plans, plus explicitly bound
conversation sources. Prior chat can be supplied as owner-imported messages or
read from an available authorized transcript provider for that project/session.
Preserve original role/author IDs and message/commit/object IDs, timestamps and
source revisions. A provider unable to establish origin yields contextual evidence,
not owner authorization. Report inaccessible history; do not promise a search of
all past chats. Do not require the owner to restate an accessible decision in a
new template. Source adapters remain bounded and read-only; raw private chat stays
local, while GitHub receives the permitted decision summary and source references.

Distinguish authoritative owner directions and adopted policy from suggestions,
quoted statements, issue proposals and agent-authored summaries. Trust comes from
configured source identity/authority, not the words "owner approved" in a document.
Apply instruction precedence and scoped supersession; newer low-authority text
cannot override an owner hold. A broad instruction may delegate routine design
choices, but a specific later hold still governs its unit. Match evidence to the
actual action, constraints, revision and conditions; approval of an earlier plan
does not automatically cover a materially expanded plan or changed PR head.

Return a typed disposition: prior_decision_applies, delegated_choice,
already_satisfied, needs_new_owner_decision, conflicting_evidence, or external_wait.
Include the chosen answer/action, exact supporting passages and identities,
applicability rationale, contrary evidence, constraints and invalidation triggers.
The controller validates source authority, current bindings and the action's
policy envelope. Within an existing delegation the model may choose the routine
implementation detail; it does not need to find a verbatim prior answer.
Model confidence or agreement between two models cannot supply missing authority.
Use one reviewer normally; a second eligible family is optional for a genuinely
ambiguous interpretation, bounded by budget, never a mandatory serial debate.

Persist the accepted decision receipt and any permitted hold transition in the
same authoritative campaign transaction. The versioned repository decision ledger
is a recoverable index of those receipts; independent file writes are not an
atomic transaction. Reuse decisions across campaigns only with freshly validated
scope/authority bindings and while their
conditions remain true, and invalidate on relevant source edits, revocation,
scope/head changes or conflicting newer direction. Apply hold satisfaction only
through the typed state/confirmation APIs with actual attribution; the reviewer
cannot write state or issue a blanket confirm --yes. Preserve the original hold
and its disposition in the audit trail. This is evidence-backed satisfaction or
exercise of delegated authority, never fabrication of a new owner approval.

Distinguish these hold classes:

| Hold                             | How it can clear                                                                                                                                              |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Unanswered product/design choice | Apply an existing owner decision or choose within delegated scope; ask only for genuinely new authority or unresolved conflict.                               |
| Configured plan/spec checkpoint  | Accept prior approval only if the gate permits it and the approved scope/revision still matches; a fresh-signoff requirement remains explicit.                |
| Explicit owner stop/hold         | Match a later authorized release or the hold's stated release condition; earlier general autonomy never overrides a specific stop.                            |
| Required GitHub review           | Read actual current approval state, or have an authorized eligible reviewer perform and submit the required review; model inference is not a GitHub approval. |
| Human-only action/attestation    | Reconcile trustworthy evidence that the exact action already happened and any required attestation exists; otherwise keep the action outstanding.             |

CI, quota, model capacity, dependency and tracking waits are operational states,
not requests for owner approval. They reconcile automatically unless a real
unresolved owner action remains. Required reviewer identity, CODEOWNERS, last-push
and stale-review rules still apply; never impersonate a human or dismiss a blocking
review to convert a recommendation into approval. Automated review is usable only
where project policy and GitHub permit that actual reviewer identity.

If evidence cannot settle a decision, notify once with the smallest concrete
question, recommended answer, sources checked and why existing authority is
insufficient. Cache the request; re-evaluate on relevant new evidence instead
of repeatedly asking or spending a new model session every scheduler tick.
Unrelated units continue while that decision is pending.

## Bounded escalation diagnosis and resolution

Existing resolve is an interactive human workflow; CRITICAL pauses do not already
invoke Astra automatically. Preserve that behavior for legacy runs. Add an opt-in
campaign resolution controller with off, diagnose and bounded-auto modes. Generic
default is diagnose; the rollout preset selects bounded-auto within trusted
project policy. The original escalation, reason and attempts remain durable.
Resolution is a separate stage, never a model-issued completion verdict.

Classify causes with typed producer codes and evidence, not a regex over the
human-facing pause reason or a model's severity downgrade. Unknown legacy causes
can receive diagnosis but are not automatically cleared. Deterministic transient
conditions use existing quota/backoff/reconciliation paths first; a reasoning agent is useful
for ambiguous technical failures, conflicting evidence and proposing repairs.
Owner holds, explicit human-only tickets/checkpoints, credentials/permissions,
spend increases, policy changes, uncertain live ownership and unsatisfied required
approvals cannot be waived by the resolver. First apply the evidence-review
contract above to recognize an existing decision or a satisfied hold. Remaining
conflicting product intent goes back to a planning decision; it is not permission
to weaken acceptance criteria.

Before launching the resolver, confirm the failed worker is quiescent and acquire
its unit ownership. Build a bounded evidence packet from resolve.build_context,
journal and session results, current main/head, frozen intent, failed commands, preserved
work, prior diagnoses and GitHub review/check observations. Redact credentials.
Start a fresh Astra or Fable diagnosis through CodingCLIAdapter in its registered unit
workspace/tab, with observation-only instructions and permission scope where
supported. Diagnostics have no authority to mutate campaign state, canonical
sources, GitHub or project policy. Verify the candidate is unchanged afterward;
unexpected edits invalidate diagnosis and hold rather than silently becoming an
approved repair. Use normal Stop/window completion and validate a versioned
diagnosis artifact against the exact input coordinates.

The artifact records cause, cited evidence, proposed action, affected paths,
preserved work, expected verification and unresolved decisions. Allowed actions
are wait/reprobe, retry an interrupted stage, reverify preserved work, repair
within the accepted contract, propose a plan amendment, or require owner input.
Confidence or model prose alone cannot authorize an action. A deterministic
policy check chooses whether and how the controller may apply the proposal.
Repairs run as separately owned coding sessions in the same unit branch/PR;
they never introduce a second writer. Environment repairs are limited to
explicit project repair commands and require successful reprobes; the resolver
cannot restart shared services or alter host settings from a proposed command.

Reuse the existing transactional preservation and re-arm primitives only through
a campaign adapter that proves their supported preconditions. Generic resolve
can reset attempts and roll back work; it is not a safe blind retry primitive.
Preserve partial commits, recovery refs, native/session generations and source
identity. Do not automate --force, --adopt-branch, unsubstantiated operator
confirmation or a global acceptance reset. Campaign lifetime diagnosis/repair counters persist
across child re-arms and restarts, even where legacy attempt counters reset.
Changed code/spec invalidates affected verification and review; successful repair
returns to the normal delivery pipeline, never directly to delivered.

Limit resolver concurrency separately from coding/review/verification. The preset
allows two sessions and at most two automatic resolution attempts per unit;
repeating the same failure with unchanged evidence stops earlier. New evidence
can support another attempt only within the remaining lifetime budget. A failed
diagnosis or exhausted budget passes through decision evidence review, then
produces any remaining deduplicated owner-action item with
evidence, attempted repairs and the smallest unresolved decision, linked to the
existing issue/PR and visible in Paradev/status. Unrelated units keep running.
Publish resolution start, disposition and verified result through the existing
GitHub outbox; preserve human holds and every unresolved required review finding.

## Planning and draft PR lifecycle

A campaign starts from an existing graph or a brief. Planning uses a dedicated
registered planning workspace and branch. Commit the user's substantive brief
or imported product baseline and open its draft PR before coding-CLI planning.
If no substantive input exists, validation requests a brief instead of creating
an empty dispatch commit. All later planning work belongs to that draft.

Package a campaign-planning skill that adapts to installed BMAD capabilities;
do not assume v6 and v7 share command names. Stages are requirements, architecture,
decomposition and planning review. Each session receives stage/graph/input
identities and writes a versioned result listing the artifacts it produced.
Validate regular confined files, stage identity, nonempty acceptance criteria,
unit ownership/dependencies, graph consistency and preserved delivered work.

Resolve installed skills in preflight. REM currently exposes bmad-product-brief,
bmad-prd, bmad-architecture, bmad-build-auto and review skills; decomposition
needs a qualified stage mapping, not an invented universal command. The packaged
stage wrapper selects the authorized unattended/fast path and records unresolved
requirements as explicit decisions. It does not initiate installation or ask
routine coaching questions during each automatic stage.

Sync GitHub inputs before and after each planning/review stage. Remote discussion
is cited evidence, not executable policy. A scoped plan repair invalidates
downstream stage results. Planning review uses a separate session; project
checkpoints remain explicit holds. Publish the planning PR through the same
delivery controller before implementation depends on its artifacts.

For each unit, commit its substantive execution spec and create its draft PR
before implementation. If an existing spec yields no new diff, refine it with
real acceptance/implementation details in that unit's branch; do not manufacture
an empty commit. Workers update that same PR. All PRs target the configured main
branch in version 1; stacked PR planning and retargeting are outside this change.

## Frequent checkpoint commits and pushes

Publish useful progress throughout planning and implementation, not only when a
unit completes. Require substantive checkpoint commits after planning stages,
coherent implementation/test milestones, completed repairs and before planned
handoff or pause. The rollout targets ten minutes of active editing between
checkpoints where a coherent snapshot is available. This is a soft progress
target, not permission to create empty commits or interrupt an atomic edit.
An intermediate checkpoint may be incomplete or have failing tests if clearly
recorded; its PR remains draft and no acceptance gate is satisfied by the push.

The worker is the sole owner of staging/committing its work. Its session contract
requires checkpoints and a versioned receipt naming unit/attempt/generation,
branch/head and progress. Reconcile receipts against Git, including reachability
from the prior owned head and scoped tracked changes. Never git add, commit or
merge in a live worker's tree from the supervisor. Dirty work that has not reached
a checkpoint is explicitly unbacked-up progress, not a falsely healthy receipt.
Report checkpoint age and outstanding push debt; do not inject unsupported
mid-turn instructions or bypass the Stop/hcom closing contract for a timer.

Every child recovery path follows the append-only recovery contract above,
including automatic retries inside a running Engine. Checkpoint publication and
recovery share the per-unit fence and durable push history; an internal rollback
cannot race a pending push and silently invalidate branch ancestry.

The coordinator persists a push intent, then pushes the exact committed SHA to
the already-bound draft PR branch through the Git chokepoint. It can publish that
immutable commit while its worker continues editing; it never pushes the worker's
index or uncommitted files. Serialize pushes per branch, coalesce superseded
pending checkpoints to the newest descendant, and verify the remote ref afterward.
Use fast-forward updates only. Unexpected remote movement or a rejected push
becomes a reconciliation hold, never an automatic force-push over another writer.
An uncertain response is read back before retry; retries are bounded and retain
all local commits. Failed remote backup remains visible and blocks transitions
that depend on the published head. Cross-unit pushes need no global execution lock.

Keep source manifests, plans and progress changes in their existing ownership
paths; exclude run logs, credentials and private conversation evidence. Respect
signing/hooks and avoid blanket staging of unrelated files. Draft CI may observe
checkpoint heads, but only evidence for the final accepted candidate can authorize
merge. Batch progress comments and superseded CI work where project policy permits.

## GitHub transport and synchronization

Use an injectable gateway and the installed authenticated gh CLI as the initial
production transport, with argv arrays, bounded timeouts and JSON stdin.
Never invoke a shell or log credentials. Pin the supported API version and use
complete page/cursor iteration. Prefer explicit repository/node IDs; a missing
or forbidden response is not absence. Back off for Retry-After/rate limits and
persist next retry times instead of occupying a worker slot with long sleeps.

Map initiative to milestone, epic to parent issue, story to child issue and
split-story units to children beneath that story. Store both number and database
ID: sub_issue_id is the database ID. Respect API hierarchy limits and existing
parents; never silently reparent human work. Preserve existing tracker lineage.
For unsupported native hierarchy, expose the capability gap and link existing
objects without claiming a native parent relationship.

Checklist tasks remain in the story's BMAD artifact and issue unless explicitly
split into independently owned, reviewed and merged units. Each executable leaf
gets its own branch, workspace and draft PR; planning has its separate draft PR.
Link each issue to its source and parent, each PR to its exact unit and planning
PR, and transition comments to the relevant head SHA, evidence and related work.
Record dependencies separately from parentage: sibling issues may be independent,
and a subissue relationship alone does not declare an execution order.

Assign the initiative milestone to its mapped issues and PRs, preserving legacy
membership unless an explicit migration resolves a conflict. GitHub's milestone
percentage counts its open/closed issues and PRs; it is not weighted BMAD story
progress. Publish exact delivered/total unit counts separately in managed
transition comments. Parent closure waits for all required children and closure
work. Close a managed milestone only when its full required scope is delivered
and no unmanaged open items make automatic closure ambiguous.

Create issues/PRs with deterministic campaign/unit markers and source links.
Put ongoing progress/evidence in bot-owned, append-only transition comments.
Do not repeatedly overwrite human-editable bodies to maintain progress. Changes
to shared fields need a visible conflict policy; GitHub issue PATCH does not
provide a general transaction with human edits. Human closes, holds and requested
changes are inputs to reconcile, not fields to erase automatically.

Outbox states are prepared, sent_unknown, confirmed and rejected, with action
ID, exact target/input coordinates and remote receipt. After timeout/crash, query
the branch/PR relation or marker and compare the requested result before retry.
For create operations without server idempotency, an empty eventually consistent
search is not proof of failure: retain uncertainty rather than duplicating work.
Idempotent updates can retry only against revalidated coordinates. Reconcile
duplicate/conflicting matches visibly. Do not promise distributed exactly-once.

Fetch all PR reviews, review threads, check runs/statuses, requested reviewers,
hold labels and relevant comments. A latest dismissed review is not approval;
an old approval does not approve a changed head. Scope required statuses by
context and app identity; distinguish pending, missing, failed and acceptable
conclusions. Discovery of required checks combines effective rulesets with
classic branch protection. Local evidence never masquerades as a GitHub check.

## Sync, final review and merge

Acceptance evidence is criterion-level: stable ID, original source, required
proof, exact candidate, environment, reproducible preparation recipe and outcome.
Distinguish passed, failed, not run and stale. Existing text criteria migrate
without fabricated passes. Preserve authorized scope amendments with provenance;
creating a follow-up issue cannot discharge a required row or close its parent.
Record actual review receipts and invalidate affected proof when the candidate,
recipe, environment or applicable requirements change.

For final integration, freeze the unit's writer, confirm teardown and require a
clean tracked tree. Fetch canonical target B; merge B into unit head H without
rewriting published history. If conflicted, run a bounded Astra conflict-repair
session in the same workspace and invalidate old evidence. Every candidate-changing action,
including spec/status synchronization, precedes final verification and review.

Before attempting that merge, persist preservation refs for the pre-merge unit
head and fetched target, along with their merge base and source identities. There
must be no uncommitted worker edits to discard. The repair context includes both
branches' changes, acceptance intent, relevant tests and the conflicted index.
Require a resolution report for each conflict explaining how both sides' intended
behavior survives, or citing a valid superseding decision. Blanket ours/theirs
selection and deleting conflicting tests are not acceptable evidence of repair.
Semantic disagreement goes through decision evidence review before any owner ask.

Verify that the merge result retains the recorded histories, has no unmerged
index entries, and passes tests covering affected behavior from both sides.
Preserved commits make recovery possible, but ancestry alone cannot prove semantic
preservation. Independent publication review inspects the conflict resolutions
and the full final diff. Push the repaired head and rerun affected verification,
CI and review. A fresh main read immediately before submission must still match
the evidence; further target movement requires another sync/acceptance cycle.
The server current-base gate or qualified merge queue remains mandatory. Do not
hold the global publication slot while waiting for Astra, tests or repair review.

Evidence binds repository, unit, full PR head H, base B, source/graph revision,
effective trusted policy and actual dev/reviewer model families. Final review
covers the complete PR diff B...H, not the story engine's post-plan baseline.
No worker edits during review. A repair or formatter change creates a new head
and restarts affected verification/review. Retain only meaningful required checks;
apply project risk selection without dropping the campaign's delivery contract.

Add a campaign publication-review prompt/result contract through SessionSpec;
do not reuse Engine.\_review_prompt unchanged, which re-invokes the build skill
on a single spec. Supply the whole diff, acceptance clauses and test evidence,
with result artifacts outside tracked candidate files. Reject a review that
edits its candidate. Parse findings into required fixes, justified dispositions
and follow-up work under trusted project policy; transcript prose alone cannot
assert that verification ran or that another model family reviewed the head.

Transition the draft to ready after local acceptance; request required remote
review and wait for selected CI/bot feedback. This is necessary where bots skip
drafts. Peer model review is not automatically a GitHub approval: record the
reviewer identity/permissions and respect GitHub's last-push/self-review rules.
Return to draft during substantive repair where supported. Any human hold,
unresolved required review or unknown rule observation prevents publication.

Recruit configured users/teams through durable review-request intents and remote
readback. Preserve human-added requests; rerequest after a candidate change only
when policy requires it. Eligible approval submissions name GitHub review ID,
reviewer identity and exact reviewed head; credentials stay outside snapshots.
Missing eligible actors produce an external review wait while unrelated work
continues. Local cross-family review cannot manufacture GitHub approval.

Keep remote check/review/label/queue waits typed and automatically reobserved.
A queue removal cannot convert a temporary wait into an unreleasable generic
approval hold. Relevant new owner answers, review changes and document revisions
invalidate the corresponding decision context. Unchanged evidence does not
consume repeated reasoning attempts. Operator release still clears only explicit
operator holds, never missing evidence or required remote conditions.

Direct-strict requires server-enforced up-to-date protection that applies to the
actual merge actor, expected-head H, and a fresh read of B/rules/holds immediately
before submission. Do not use admin bypass or silently weaken this contract.
Main movement restarts affected candidate acceptance; bound retries and show
starvation instead of looping forever. Prepare, test and review independent
candidates concurrently against recorded bases. Acquire the publication slot
only for the final fresh read, merge submission and uncertain-result resolution;
never hold it while running a model review, test suite or waiting for CI.

In direct mode, a confirmed merge releases that slot as soon as its new target
is reconciled. Remaining source/issue synchronization is per-unit debt and blocks
that unit's dependents, not other publication candidates. Other candidates whose
base moved must re-establish the required evidence; do not reuse stale acceptance
merely to improve throughput. Queue mode delegates landing order to GitHub and
permits concurrent candidate checks under the configured queue policy.

GitHub's current asynchronous endpoint is PUT pulls/N/merge-async. Specify sha,
merge_action and bypass_rules=false; persist its UUID. 202 is acceptance, a 409
can identify an existing request, and enqueued is not merged. Poll the request
result and independently read PR.merged. The older synchronous merge API is
available for qualified direct merges but does not implement merge queues.

Queue mode uses an explicit merge_queue action and requires merge_group CI.
Its combined candidate C includes target B, H and potentially earlier queued
PRs; it is not equivalent to an updated PR head. Exact combined-candidate review
requires a configured, required evidence producer on C. Provide an invocation
contract/example for that producer and refuse queue activation without it.
Requeued candidates invalidate old evidence. Do not claim PR-head review alone
proves the contents GitHub's queue eventually merges.

Qualify and produce required evidence on both PR head H before enqueue and
synthetic merge-group head G after admission. Bind each receipt to candidate kind,
SHA/base, policy and source evidence. Read back the same required App/context
identity for both; a G-only producer cannot satisfy an H enqueue prerequisite.
Reconcile uncertain check writes before retrying, with no fabricated green check.

Keep the existing project gate-command contract and add a separate actual merge
executor. Its durable request binds repository, PR, H/B, rules fingerprint,
method and bypass policy. Submission may be pending or uncertain; reconcile the
remote state before retry. When configured, this executor performs publication,
with no independent native submission fallback. The campaign still verifies
GitHub merge, canonical ancestry and tracking. A successful dry-run gate is not
proof that the required project merge route executed.

On confirmed merge, fetch canonical target and prove the reported merged commit
is reachable. Squash/rebase need method-aware reconciliation; H need not itself
be an ancestor. Then publish BMAD source changes, issue closures, parent progress
and configured required projections. Use reference links rather than automatic
closing keywords for campaign-managed issues, so the merge itself cannot close
tracking before source reconciliation. Close a unit only after its source update
is confirmed, and its parents only after all required children/closure work.
A merge response followed by a tracking failure stays merged_pending_sync and
is resumable without another PR.

## Paradev transport and presentation decisions

Port the pinned transport/runtime and minimal host client into the package with
their tests. Keep explicit platform opt-in, optional websockets dependency,
portable locks, Git chokepoint and lazy imports. Register with an always-false
platform matcher: the current default, first-match and fallback paths all test
that matcher, while explicit environment/policy selection resolves by name.
Characterize all selection paths, including missing tmux, to preserve opt-in.
Detect an installed external backend of the same name and refuse
ambiguous migration; do not redirect live external records to the builtin.

The Paradev workspace provider allocates the correct project workspace before
engine creation, records returned path/branch and verifies Git common directory
plus per-worktree identity. It never uses --agent. Preserve required setup/seed
files, hooks and ignored configuration through existing supported setup seams.
Prove that private ticket/config redirection survives that setup.
Before allocation, validate the project's configured worktree base against the
owner's allowed root. The standalone provider also uses an explicit worktree
root; neither provider silently allocates beneath a run folder or temporary path.

One actual worker PTY is attached once. Persist workspace/terminal/pane/tab and
native provider session mappings independently; hook read-back, not a terminal
title, proves native agent attribution. Pending/unknown attribution is visible.
Titles name unit, phase, attempt and agent; use a separate display-title method
so renaming cannot break window identity. Preserve focus and user-customized
layout. Reflection tasks remain optional; the campaign owns all identities.

Wire task projection into normal runtime reconciliation when enabled, including
held/blocked/merged-tracking-pending states. Expose nested transport, reflection
and native-registration failures in public status. Grouping identities include
source and initiative as well as epic; equal slugs from different initiatives
must not share a managed group accidentally. Tests must observe actual runtime
calls rather than invoking projection helpers directly.

Keep existing unconditional adapter teardown. Once owned execution is confirmed
stopped, retain its durable terminal log/transcript and offer the native Paradev
file view for completed/failed output. Close only BMAD-owned views by policy;
do not create a coding agent to display history or keep one alive for its tab.
Parked terminal-history retention is an optional later capability, not required
for this PR. Record latest completed/failed phase per unit and preserve artifacts
until the campaign retention policy permits removal.

Hold/takeover first acknowledges a durable scheduler hold and suppresses all
automatic nudges/retries/publication. It can permit direct input to the same
owned worker while the hold remains. It is not proof that a running agent has
stopped writing. Direct file editing requires confirmed execution quiescence.
Release rechecks source, process and head identity and re-arms invalid evidence.
The typed lifecycle policy distinguishes ephemeral BMAD workers from protected
operator sessions; where a project requires superset-control, preflight requires
that supported route and never substitutes a raw close after refusal.

## Communication decision

This PR includes optional scoped hcom roster/events/transcript observations and
existing qualified nudge behavior, plus structured sibling context generated by
the campaign even when hcom is absent. It does not enable automatic new peer
wakeups by default. Persist room, actor/process generation and native binding;
use exact recipients, not a room broadcast. Names, tags and threads are not ACLs.

Bind the transcript-evidence provider in the normal CLI. Wire bounded event and
transcript observations, durable cursors and configured context limits separately
from Paradev reflection; disabling reflection does not disable requested hcom
observation. Scope sibling context to relevant active work and dependencies,
excluding unrelated or delivered history unless explicitly retrieved.

Observation mode sends no messages or subscriptions. Existing TeamRouter/hcom
enrollment can still receive mail; the installed hcom has no per-actor mute.
Preflight reports that distinction. Native teardown must be confirmed before
verification, source publication or reuse of the editing lease. Keep messages
advisory and durable material decisions in product artifacts.

Automatic peer delivery is deferred unless a deterministic closing protocol is
qualified for the actual launcher without requiring an unplanned hcom release.
The test gate covers both hook orderings and in-flight/late delivery, not just
an unread counter. No hcom actor is allowed to create a second executor or mark
another unit complete. This limitation does not prevent concurrent BMAD units.

## REM activation and delivery of this PR

Ship the entire framework in PR 18; each implementation stage is a commit/check,
not a separate capability PR. Do not restore exploratory stash files wholesale:
compare them against this contract and retain only compatible implementation.
Keep REM-specific logic out of portable orchestration; support trusted project
policy/evidence and merge-command adapters with exact-head receipts.

REM still mandates its maintained merge_pr.py route and risk-proportionate
evidence, signed commits, cross-family review for high-risk work, explicit owner
holds, and project-wide verification locks. A project merge-command adapter may
call that route synchronously and then independently reconcile GitHub. It must
not hand publication to a scheduled merge waiter and report delivery early.
Disabling automations does not imply removing required project tooling.
Strict mode also requires the command to honor the server's current-base gate;
an administrative fallback that bypasses it is incompatible. The current REM
route includes administrative exceptions, so activation must resolve that
contract explicitly rather than merely invoking the old command unchanged.

REM's working agreement also forbids changing GitHub rulesets. Implement BMAD's
executor contract, rejection fixtures and actionable preflight in this PR;
changing REM policy/tooling or enabling a different publication guarantee requires
its separate explicit activation decision. The generic integration cannot be
called REM-qualified merely because it invokes that script.

Live rules inspected on 2026-11-05 expose required checks with
strict_required_status_checks_policy=false and no merge_queue rule. Classic
protection reports absent while enterprise/organization/repository rulesets
still apply. Consequently strict unattended publication is not currently ready
on REM. The new preflight must report this accurately. Installing bmad-loop
cannot repair it, and this PR must not change organization settings implicitly.

Resolve that activation prerequisite through an explicitly authorized repository
policy change or a reviewed change to the requested guarantee. Preserve the
current strict contract meanwhile: allow planning/execution to proceed when
explicitly selected, retain ready PRs, and hold automatic merge. No code can
atomically compare the base through GitHub's head-only merge precondition.

REM's Studio configuration permits up to 24 active epics, 32 build/test threads
per command and eight nextest suite slots. These are separate limits; they do
not establish unlimited story workers. Import explicit max_workers and
max_workers_per_epic with ownership scopes; retain the eight-slot shared lock
while non-campaign jobs coexist. Do not use CPU load as an undeclared throttle.
Configure REM's 24 active epics and eight verification slots independently of
the execution-worker, review and in-flight workspace ceilings. Do not convert
the epic limit into a hidden story-worker limit or add a per-epic serial ceiling.
Choose explicit worker/review limits within the existing account and spend
policy, and report their effective values in preflight. Eligible account capacity,
budgets, actual dependency/scope conflicts and verification capacity constrain
admission; the coordinator does not. Measure throughput before claiming a maximum.

Migration inventories current processes, runs, workspaces, PRs, holds and
automation IDs. Adopt only units proven unowned or supported resumable bindings;
never start a second engine over an existing live run. Fence old scheduler and
publisher admissions before enabling the new coordinator. Drain or preserve
old in-flight runs and their launcher environments. Disable only the replaced
automations after their native replacement is qualified; retain unrelated jobs.

Stamp the version and release notes using repository scripts inside this same
PR before final review/CI. Then merge through this repository's policy, install
the exact merged fork SHA with uv and the Paradev extra, update the canonical
Paradev base and verify installed provenance.
Do not rewrite environments used by live old workers. Smoke-test package import,
CLI/JSON, profiles, native mux selection and scripted zero-token PTY lifecycle.
Only then perform REM preflight and the staged opt-in activation above.

The implementation handoff is complete only when the merged fork SHA is both
the canonical Paradev base and the verified global installation, the REM-specific
launch/source/merge contracts qualify, and preflight reports no unresolved gate
to the requested autonomous mode. Exercise the full campaign on scripted,
zero-token fixtures and the actual installed native transport. Record any real
REM canary against an explicitly selected unit with its own draft PR and normal
delivery evidence; do not use an LLM-spending run as an E2E test. Retire replaced
automations only after their responsibilities are covered and existing workers
are accounted for. Passing bmad-loop tests or installing the wheel alone does
not establish REM readiness; an unresolved activation prerequisite leaves the
overall outcome incomplete.

The present implementation milestone stops before those deployment actions:
review-ready means committed and pushed code/docs, normal-path qualification and
two fresh independent reviews against the same candidate. One review covers
completion/authority/publication; the other covers runtime wiring, harness/handoff
and throughput. Substantive later changes invalidate affected evidence. Report
disabled hosted Actions and unrun platform gates accurately; do not enable
repository settings or claim CI success implicitly.

## Required qualification matrix

Use existing sandbox fixtures and scripted coding sessions; no live/E2E gate
spends LLM tokens. Each negative gate gets its required ablation check.

| Layer              | Required coverage                                                                                                                                                          |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pure model/store   | Invalid selectors, duplicate IDs, cycles, graph revisions, legal transitions, atomic state/outbox, command idempotency, two-coordinator refusal.                           |
| Worker seam        | One selected unit in each source mode, no shared mutable source, child closure suppression, exact launch/resume identities, PID reuse, conflicting plugins.                |
| Scheduler          | Two epics and two independent sibling stories overlap; dependent units wait for delivery; scope collisions, fair resource limits and crash-held claims.                    |
| GitHub gateway     | All pages, auth/rate-limit faults, lost create/update/merge replies, conflicting markers, hierarchy IDs, preserved human text and event cursor restart.                    |
| Intake/triage      | Issue versus PR identity, edited/duplicate inputs, bot feedback, cursor crashes, existing issue reuse, scope limits, same-PR repair and safe graph revisions.              |
| Discovery          | Active/abandoned/merged PR overlap, squash history, branch-only partial work, external ownership, stale heads, partial coverage and no duplicate admission.                |
| Model/resolution   | Astra/Fable availability and attribution, exact-model refusal, fresh cross-provider attempts, effort propagation, verified repairs and persistent retry budgets.           |
| Decision evidence  | Prior/delegated decisions, authenticated sources, spoofed summaries, scope/head drift, revocation, conditional holds, actual GitHub approvals and deduplicated owner asks. |
| Checkpoints/repair | Push exact heads during live editing, coalesce retries, refuse foreign head movement, preserve both merge histories/behaviors, Astra-only repair and repeat acceptance.    |
| Publication        | Moving H/B, late review changes, draft/ready transitions, required app/status identity, strict-rule absence, queue candidate changes, squash/rebase, merged tracking debt. |
| Planning/source    | Brief to planning PR to graph, malformed artifacts, existing-plan import, private shared stores, three-way unit updates, multiple initiatives and closure units.           |
| Paradev            | Real PTY identity, same-terminal pane attach, title and native-agent read-back, missing/duplicate panes, user focus, output history after teardown, absent optional extra. |
| Communication      | Missing hcom, stale name/generation, room leakage, resets, automatic enrollment detection, both Stop orderings, late messages and operator hold.                           |
| End to end         | One resumable campaign reaches plans, drafts, parallel units, full-head acceptance, server merge proof and tracking completion using fake GitHub and scripted agents.      |
| Regression/release | Existing full suite, pyright, Trunk, policy/env/portability guards, wheel skills/data, version synchronization, Windows import without optional transports.                |

Inject failure before and after every remote mutation and worker launch boundary.
Assert restart continues the same PR/claim or reports unknown; never duplicate
an executor. Run only affected checks during iteration, then the required full
CI/platform matrix once the final implementation stabilizes. Report unavailable
live-host qualification explicitly instead of equating mocked tests with it.

Recovery qualification must run a real child retry with rollback_on_failure=true
after a checkpoint reaches a fixture remote, then prove its recovery and next
checkpoint remain descendants and push without force. Repeat with a push prepared,
in flight and remotely accepted but its reply lost; crash at fence, preservation,
compensating commit, receipt and re-arm boundaries. Cover normalization, resolved
re-drive, startup/resume and rejected-attempt ledger-finally paths. Verify accepted
earlier work, owned source corrections and pre-existing untracked files survive.
Restoring the legacy reset path must fail the ancestry regression; exercise the
defensive reset guard directly as a separate refusal/ablation test.

Configuration qualification must use inherited story-boundary reload plus a
checkout whose model, limits, gates and review values differ while config_digest
still matches. Prove first dispatch and resumed/re-armed workers use the frozen
snapshot and that their engine, flows and adapters agree. Test the effective
reload-off policy and the early campaign guard independently, so one cannot hide
removal of the other. A guard test supplies story-boundary mode with a trusted
campaign binding and must fail when the early guard is removed. Separately prove
legacy reloads still apply and direct child-resume cannot bypass the snapshot.

Throughput regression tests must demonstrate overlap, not just eventual success:
park one fake review/CI response and prove another independent unit starts and
advances; hold a tracking retry and prove another ready PR can publish; fill
verification slots and prove coding capacity still makes progress. Instrument
admission, stage and resource-wait durations so a serialized I/O path or idle
capacity is visible during REM qualification.

Scale qualification uses at least 48 stories across 24 active epics, eight
verification slots and concurrent reasoning jobs. Exercise ownership contention,
quota waits, parked reviews, moving main, tracking retries and restarts. Measure
eligible idle capacity, fairness, control responsiveness and confirmed delivery,
not merely started agents. Slow source/Git/inventory reads must leave controls
responsive; hot-state growth and prompt context must remain bounded. Retained
worktrees are accounted separately from active workers, without deleting dirty,
live, uncertain or required evidence-bearing work.

Run final exact-head pytest with the supported parallel runner and ordinary
temporary paths, pyright, Trunk, documentation/version checks and wheel/sdist
install smoke tests. Use targeted checks during iteration; do not replace final
integrated coverage with isolated passing reruns. Actual model/native-fidelity
qualification remains separate from zero-token repository tests and follows
[ADR 0003](0003-bounded-reasoning-and-handoff.md#implementation-sequence-and-qualification).

## Fact-check references

- [GitHub pull and asynchronous merge APIs](https://docs.github.com/en/rest/pulls/pulls)
- [Effective branch rules](https://docs.github.com/en/rest/repos/rules)
- [Merge queues and merge-group checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [Subissue identifiers and operations](https://docs.github.com/en/rest/issues/sub-issues)
- [Issue observations and update filtering](https://docs.github.com/en/rest/issues/issues)
- [Milestone membership and progress](https://docs.github.com/en/issues/using-labels-and-milestones-to-track-work/about-milestones)
- [Check-run observations](https://docs.github.com/en/rest/checks/runs)
- [REST retries and pagination](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
- [Branch inventory](https://docs.github.com/en/rest/branches/branches)
- [Astra model identity](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [Codex model and reasoning configuration](https://learn.chatgpt.com/docs/config-file/config-reference)

Repository sources inspected include runsetup.compose_run, TicketsEngine's
selection/store/closure paths, CodingCLIAdapter.run, multiplexer selection,
the pinned Paradev transport and REM's current agreement/policy/merge route.
External capability observations must be repeated during activation.
