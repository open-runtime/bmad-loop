# ADR 0002 implementation contract

- **Status:** Reviewed implementation design; runtime qualification required
- **Date:** 2026-11-05
- **Parent:** [Native SDLC campaign](0002-native-sdlc-campaign.md)
- **Delivery:** One bmad-loop PR, including code, tests, documentation and local deployment tooling

This contract resolves implementation choices in ADR 0002. It describes proposed
behavior, not commands available in the current release. Preserve existing run
formats and serial execution; the campaign is a new supervisor above them.

## Command and configuration contract

Add the following namespace, with injected engine, transport and clock seams:

```text
bmad-loop campaign validate SPEC --json
bmad-loop campaign preflight SPEC --json
bmad-loop campaign run SPEC [--paradev-reflect] [--paradev-project ID] [--json]
bmad-loop campaign list --json
bmad-loop campaign status ID --json
bmad-loop campaign resume ID [--json]
bmad-loop campaign stop ID [--terminate-workers] --json
bmad-loop campaign hold ID --unit UNIT --reason TEXT --json
bmad-loop campaign release ID --unit UNIT --json
bmad-loop campaign attach ID --unit UNIT [--role ROLE]
bmad-loop campaign reconcile ID --json
```

Run/resume supervise in the foreground. JSON mode emits one terminal result;
progress goes to stderr and durable events. Operators use status in another
terminal for live observations. Stop defaults to draining: it closes admissions
while existing workers finish their current bounded stage. Terminate-workers
uses owned process/session controls and retains uncertain claims. Exit codes
remain 0, 1, 2 and 130; blocked/held details are fields, not new exit codes.
Follow machine.py for failures: a verdict command can emit a failing verdict;
an operational error outside that contract leaves stdout empty and uses stderr.

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

## State, ownership and module boundaries

Implement by responsibility, with the following public contracts agreed before
parallel editing. Names below are module boundaries, not required class names.

| Module                    | Contract                                                                                       |
| ------------------------- | ---------------------------------------------------------------------------------------------- |
| campaign_model            | Frozen specifications, unit graph, typed states and evidence coordinates.                      |
| statemachine              | Legal campaign transitions alongside, without changing, existing engine/plan transitions.      |
| campaign_store            | Atomic snapshot, coordinator lock, revision checks, outbox and command inbox.                  |
| campaign_sources          | Import/refresh sprint, stories and ticket sources; private-store preparation and publication.  |
| campaign_intake           | Durable remote observations, triage sessions, dispositions and bounded graph-change proposals. |
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

## Intake and triage

Always refresh mapped issues/PRs in the selected repository. New unmapped issues
enter through explicit selectors or configured intake labels; do not import the
repository's entire backlog by default. Existing BMAD tracker bindings take
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

Freeze the unit's writer, confirm teardown and require a clean tracked tree
before publication. Fetch canonical target B; merge B into unit head H without
rewriting published history. If conflicted, run a bounded repair session in the
same workspace and invalidate old evidence. Every candidate-changing action,
including spec/status synchronization, precedes final verification and review.

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

Throughput regression tests must demonstrate overlap, not just eventual success:
park one fake review/CI response and prove another independent unit starts and
advances; hold a tracking retry and prove another ready PR can publish; fill
verification slots and prove coding capacity still makes progress. Instrument
admission, stage and resource-wait durations so a serialized I/O path or idle
capacity is visible during REM qualification.

## Fact-check references

- [GitHub pull and asynchronous merge APIs](https://docs.github.com/en/rest/pulls/pulls)
- [Effective branch rules](https://docs.github.com/en/rest/repos/rules)
- [Merge queues and merge-group checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [Subissue identifiers and operations](https://docs.github.com/en/rest/issues/sub-issues)
- [Issue observations and update filtering](https://docs.github.com/en/rest/issues/issues)
- [Milestone membership and progress](https://docs.github.com/en/issues/using-labels-and-milestones-to-track-work/about-milestones)
- [Check-run observations](https://docs.github.com/en/rest/checks/runs)
- [REST retries and pagination](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)

Repository sources inspected include runsetup.compose_run, TicketsEngine's
selection/store/closure paths, CodingCLIAdapter.run, multiplexer selection,
the pinned Paradev transport and REM's current agreement/policy/merge route.
External capability observations must be repeated during activation.
