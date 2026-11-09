# ADR 0002: A deterministic SDLC campaign with GitHub delivery

- **Status:** Implemented; release-specific qualification and target-project delivery remain gated
- **Date:** 2026-11-05
- **Scope:** An opt-in campaign supervisor, isolated workers, planning, GitHub synchronization and native Paradev reflection

## Problem

The story engine implements, verifies, reviews and commits one unit at a time.
Its local `done` phase does not prove a GitHub pull request merged. Factory
planning, scheduling, issue mirroring and merge waiting currently require
project-specific helpers and independently scheduled processes.

A campaign must own these responsibilities through one resumable command. A unit
is delivered only after its complete PR has been reviewed and verified against
current main, GitHub confirms the merge, the merge is reachable from freshly
fetched main, and project tracking has been reconciled. An accepted merge request,
queued merge, local commit or successful worker exit is insufficient.

## Decision

The detailed [implementation contract](0002-campaign-implementation.md) resolves
the command surface, persistence, worker/source isolation, publication, terminal
retention and activation decisions below. Both documents define the contract for
the initial implementation and reviewed follow-up releases; current usage is in
the [campaign guide](../campaign-guide.md).
Capability and repository-policy prerequisites remain explicit; implementation
tests do not establish that a target project has been activated.

The accepted [bounded reasoning and handoff extension](0003-bounded-reasoning-and-handoff.md)
adds semantic routing, graph/priority review and portable cross-harness continuity.
It preserves this record's delivery contract and does not replace outstanding repairs.

### Implementation and delivery milestones

The initial campaign and reasoning/handoff implementation has been merged and
installed. Follow-up repairs retain the implementation contract's
[G0–G8 traceability matrix](0002-campaign-implementation.md#implementation-goals-and-traceability)
as the shared acceptance map. A helper test is not evidence that the normal
campaign path invokes that helper. Record implementation, runtime coverage,
native qualification, review readiness and activation separately.

Each changed release requires the integrated candidate, updated documentation,
affected production-path regression coverage and independent reviews. Renew native
proofs when their installed paths, dependencies or contracts change; retain valid
unchanged evidence. Merge, exact-commit installation and canonical-base update have
been demonstrated and remain requirements for later releases. The selected REM
pilot must prove real parallel delivery, operator reflection and communication,
and live-worker drain/resume. A green suite, disabled automations or a locally
completed story cannot substitute for those target-project gates.

The earlier `276daa62` implementation passed its integrated regression suite,
packaging and static checks, and two independent reviews found no open findings.
These results do not qualify the later delivery extension. Normal-runtime
coverage also demonstrates live history warmup, quota handoff, exact prefix reuse,
fresh owner-constraint audits and fenced successor delivery. The fresh scripted
48-story load and stop/resume qualification passed against the same frozen
candidate, including completed parent tracking.

The sealed-history fidelity assessment and native read-only successor retrieval
establish the bounded capabilities recorded in
[ADR 0003](0003-bounded-reasoning-and-handoff.md#qualification-achieved-and-remaining-gates).
They do not establish universal harness support, writable continuation of the
historical packet or target-project activation.

Optimize useful parallel work through dependencies, ownership and token/session
capacity. Preserve initiatives and sprints as existing source structure and GitHub
hierarchy, without adding calendar, date, velocity or sprint-capacity machinery.
The coordinator lock protects state; it must not serialize independent workers,
reasoning jobs, verification or candidate preparation.

Add a deterministic Python campaign supervisor above the existing serial story
engine. Planning, implementation, conflict repair and review run in coding-CLI
sessions. The supervisor never calls an LLM. Existing `run`, persisted run state
and its compatibility contracts retain their current behavior.

The campaign has a versioned input specification and a separate versioned state
document. It records immutable campaign/unit identities, dependencies, worker
claims, configuration fingerprints, source identities, external IDs, outbound
actions, review/verification evidence and confirmed publication receipts.

One coordinator owns campaign state and GitHub mutations. Each worker has its
own process, worktree, adapters, native sessions, journal and run state. Workers
never concurrently call methods on a shared `Engine` instance. A durable claim
precedes dispatch; uncertain launch ownership holds until reconciled.

### Completion contract

The publication boundary requires all of the following:

1. A genuine product plan/spec has been committed and pushed, and a draft PR
   exists before implementation starts. No empty or artificial dispatch commit is
   created merely to manufacture a PR diff.
2. For direct publication, the complete PR head includes the recorded current
   target head. Queue publication instead binds the combined merge-group
   candidate and its target. Synchronization preserves published history.
3. Local verification and independent final review identify the same full PR
   head, target head and trusted policy fingerprint, or the exact combined queue
   candidate when using a queue. A later change invalidates affected evidence.
4. Required GitHub checks and reviews are satisfied; unresolved review requests,
   conflicting changes, human holds and unknown observations prevent merging.
5. The merge uses the expected head identity and server-enforced up-to-date
   protection or a merge queue. A client-side mutex cannot protect against other
   publishers moving the target.
6. GitHub reports the PR merged. Its merge commit is reachable from a freshly
   fetched canonical target. A closed, unmerged PR remains unfinished.
7. Source records, linked issues and milestone/parent progress are reconciled.
   Only then does the campaign release dependent units and report delivery.

Completion scope is explicit per native ticket source. The compatible default,
`completion_scope = "epic"`, retains whole-epic child and retrospective closure.
An explicit ticket `units` selection may instead use `"selected-units"`. It owes
the same publication, actual-landing, dependency, required-repair and child-tracking
gates, but never creates new native parent closure work or treats delivery of a
selection as delivery of the epic/initiative. Parent issues and milestones remain
outside its closure authority, including prepared-write retries and sparse GitHub
projections. Status distinguishes selected work delivered with retained parent
scope. Unselected source siblings and parent lifecycle files remain unchanged.
Hard parent dependencies are preserved: only already-complete native closure with
its existing retrospective can enter verification reconciliation in this scope;
missing closure cannot silently become a selected worker's parent-writing task.
Persist the opt-in through restart while preserving legacy source/configuration
identity hashes for snapshots that omit the field.

Tracked BMAD plan status on an isolated branch is a proposed change until its PR
lands. Shared or ignored stores require private worker snapshots and publication
through the existing sole-writer APIs after verified landing. They must never
receive premature status updates from a worker.

### GitHub ownership and synchronization

BMAD owns the work hierarchy, planned intent, dependencies and acceptance
criteria. GitHub owns PR, review, checks and merge facts. Human-authored remote
discussion is preserved and imported as evidence or proposed work; it is not a
command channel or authority to change execution policy.

Map initiatives to milestones, epics to parent issues and units to subissues.
Store database/node IDs and numbers explicitly; do not derive identity from a
mutable title or overwrite an existing BMAD tracker lineage. PR bodies, issues
and structured comments link the corresponding plan, unit, PR and source SHA.

Reconcile before and after planning and review, before merge, and after confirmed
landing. Persist an outbound intent before network mutation. After a timeout or
crash, reconcile the remote marker/identity before retrying. Unknown outcomes
remain visible. Pagination failure is not an empty result. Preserve unmanaged
body sections and hold conflicting simultaneous edits.

Intake selects unmapped issues explicitly, by configured labels, or through the
rollout's all-open-and-new repository scope. A durable,
versioned inbox deduplicates remote edits and excludes the campaign's own
comments. Bounded triage sessions propose reuse, repair, new work, duplicates,
clarifications or holds; deterministic validation admits only authorized scope.
Required review findings repair the same PR. New scope becomes a reviewed
planning change before execution, without blocking unrelated units. Use ordinary
issue references in PRs and close tracking explicitly after confirmed merge and
source reconciliation; parent issues wait for all required children.

Planning also inventories existing PRs, relevant branches, local worktrees and
known runs. It records whether proposed work is already delivered, actively
covered, reusable, new or unresolved. Carry exact head/source identities and
remaining acceptance gaps into the plan. Observe external work without launching
a duplicate worker or taking over another author's branch. Adoption preserves
the existing PR/history when authorized and ownership can be reconciled.

### Parallelism and recovery

The scheduler admits only dependency-ready units, reserving worker and resource
slots before launch. Verification concurrency can be lower than worker
concurrency. Independent units may progress while another unit is blocked.
Campaign stop prevents new admissions and drains or stops owned workers through
their supported stop mechanisms.

Use a single integration queue. A main update invalidates stale publication
evidence and may require another sync, verification and final review. Persist
each boundary so a restart resumes the existing PR and worker rather than
creating replacements. The supervisor runs continuously; no scheduled Paradev
automation is needed for its routine decisions.

The queue serializes final merge decisions, not candidate preparation. Independent
workers, verification and reviews run concurrently under separate resource
limits. The coordinator must remain responsive while those operations run.
Tracking debt after a confirmed merge blocks only that unit's dependents;
unrelated work and merges continue. No implicit per-epic serial ceiling applies
to the campaign's independent stories.

Campaign workers retain their complete resolved configuration on first dispatch,
retry and resume; inherited story-boundary policy reload is disabled for these
workers. Ordinary runs retain their existing reload behavior. All campaign retry
and recovery paths preserve append-only branch history, including internal engine
rollbacks: a per-unit fence reconciles pending pushes, then a compensating commit
restores any required attempt state without resetting behind a published head.
Recovery and re-arm are durable stages with preserved work and lifetime budgets.

### Model roles and escalation resolution

The rollout configuration permits Astra or Fable, whichever has eligible capacity,
for planning, independent planning review, semantic triage, escalation diagnosis
and decision evidence review through qualified Codex/Claude/TeamRouter profiles.
Generic role settings remain configurable and
legacy execution defaults remain intact. Validate effective model/effort rather
than inheriting this chat's selection or selecting an unapproved model. Publication
review retains project-required provider-family independence.

Conflict repair specifically uses Astra. Commit and push substantive checkpoints
throughout planning, implementation and repair so draft PRs expose ongoing work.
The worker owns commits; the supervisor publishes exact immutable heads without
touching a live worker's index. Before publication, merge current main with both
pre-merge heads preserved, resolve conflicts against both sides' intended behavior,
and repeat affected verification plus independent final review. Main movement
invalidates stale acceptance; no force-push or blanket conflict-side selection
substitutes for reconciliation.

Before requesting owner input, a bounded evidence review searches authorized
owner notes, instructions, GitHub discussions and available bound conversations.
Apply a still-valid prior decision, exercise explicitly delegated routine judgment,
or recognize a satisfied approval condition with source attribution. Persist the
decision and its invalidation conditions. An explicit stop, genuinely new decision
or required fresh attestation stays outstanding until its actual condition clears;
model confidence cannot create authority. CI/resource waits are not approval holds.

An opt-in bounded resolution stage diagnoses technical escalations, proposes
typed actions and verifies any permitted repair before returning to normal
execution/review. Preserve partial work and lifetime retry budgets. Automatic
resolution cannot waive human-only decisions, operator holds, permissions,
required checks/reviews or product intent. Unknown ownership remains held.
Repeated failure produces a linked owner-action item; unrelated work continues.

### Native Paradev reflection

Include an optional reflection mode that uses Paradev's existing project sidebar,
workspace tag folders, branches, PR indicators and terminal tabs. Bundle the
transport in bmad-loop, adapting the existing paradev-tooling implementation and
its tests. The installed package must not depend on another repository checkout.
BMAD retains scheduling and delivery authority; Paradev supplies registered
workspaces, worker PTYs and their native presentation.

The entry point is a campaign run option, `--paradev-reflect`, with an
explicit Paradev project selector and equivalent policy settings. It selects
the Paradev workspace provider, native `paradev` mux and projection together.
The command contract is specified in the implementation document. A separate
reflection-only command must not suggest it can adopt an arbitrary live tmux
worker as a native Paradev agent.

#### Sidebar and execution mapping

| BMAD concept                                         | Paradev representation                                                                      |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Target repository                                    | Its registered Paradev project.                                                             |
| Story with independently planned child units         | One managed story tag folder containing the child workspaces.                               |
| Independently editable and mergeable unit            | One workspace, one worktree, one branch and one draft PR.                                   |
| Planning, implementation, review and repair sessions | Actual worker terminals/tabs within the owning workspace, named for their role and attempt. |
| Campaign/epic identity and dependencies              | Stable BMAD IDs and linked task metadata; the dependency graph remains in BMAD.             |
| Delivered unit                                       | Confirmed GitHub merge plus tracking reconciliation; terminal activity is advisory.         |

A story need not be split merely to fill a folder. An unsplit story has one unit
workspace. Splitting creates explicit child units with independent acceptance
criteria and dependencies; the parent story is delivered only when all required
children are delivered. Parallel editors never share an editing checkout. Phase
tabs do not imply simultaneous writers or create extra PRs for one unit.

Use stable, normalized managed tags and readable names carrying campaign, epic,
story and unit identity. Keep issue/PR links and full names in task metadata;
do not use mutable names as identity. Story grouping is the initial layout, with
an epic grouping option for larger campaigns. Both use the existing flat folder
model; no nested folder hierarchy or custom Paradev dashboard is required.

Paradev currently renders a workspace in exactly one tag folder, choosing by
folder order and then tag name. Multiple tags do not create multiple workspace
rows or nested groups. Preserve human and locally mandated tags, including
side-task tags where applicable. Detect competing grouping tags and report the
placement limitation visibly; names, links and CLI filtering remain available.
Do not reorder user folders or remove their tags to force placement.

#### Workspace and terminal ownership

The workspace provider creates or reconciles the registered project workspace
first, without `--agent`, then records its returned ID, actual branch and
worktree path. Verify the project repository and checkout identity before using
it. Run the unit engine in place in that worktree with an explicit workspace
selector; do not create another nested BMAD worktree. The existing native
transport requires matching Git top-level and per-worktree Git directory.

The mux launches the coding session once and attaches that same terminal to a
pane. Do not also call `agents create`, import its conversation or launch a
viewer agent. Persist campaign/unit, workspace, task, branch/PR, mux session,
terminal and native session identities with attempt/ownership generations.
After uncertain allocation or launch, reconcile those identities before retrying.
An unrelated existing workspace or unknown live terminal is not reusable.
Retain caller-minted launch IDs, tombstones, bootstrap identity checks,
sequence-addressed output with visible gap markers and disposal read-back.
The current CLI alone does not supply all these facts; keep a narrow,
schema-checked host RPC/WebSocket boundary for native terminal operations.

Make task projection optional within reflection mode. Discover status IDs and
use the explicit Paradev task tracker. Preserve unmanaged task text and labels;
use managed links for relationships absent from the flat task API. Project
blocked/held states visibly without pretending an agent's idle status proves
completion. Persist external mutation intents and reconcile uncertain outcomes
before repeating workspace or task creation.
Campaign state independently owns the complete hierarchy and external-ID map;
task metadata only mirrors them. Reflection with tasks disabled must retain
identity, links and discovery through campaign state and workspace metadata.

Viewing and basic terminal input use the real worker PTY. An explicit operator
hold/takeover must reserve ownership, suppress automatic input/retries and
record a durable event; resume rechecks identities and invalidates affected
review/verification evidence. A formal takeover UI and arbitrary bidirectional
plan editing are deferred. Raw input alone never grants policy changes or
creates another completion signal.

Pane disappearance is a presentation observation, not proof of worker death.
Closing a terminal can stop its worker; reconciliation must distinguish that
from closing only a view. Define retention for completed/failed sessions and
never clean up a live or uncertain worker to make the sidebar look finished.
Use supported lifecycle controls, respecting any project retirement guard.

#### Terminal tabs and operator layout

Persist pane/tab bindings separately from execution identities. Titles identify
the unit, phase, attempt and coding agent without changing the stable mux window
identity. Review and verification views identify the source head they cover.
Create only views for real sessions or clearly labelled deterministic output;
an unused phase must not appear to be a running agent.

After launch or restart, inspect the layout and attach the existing terminal
when needed. Reuse an existing view instead of adding duplicate tabs, preserve
operator names/layout where possible and avoid taking keyboard focus on each
phase change. Report an unverifiable or multiply attached view without closing
another pane. Qualify the displayed tab title, native agent binding and status
against the installed app, not merely an internal terminal record.
Read back workspace, terminal, native session and CLI session/attempt bindings;
missing native attribution stays visibly pending/unknown within a bounded
registration window. A visible terminal alone does not prove native agent
attribution, and a missing binding never authorizes a replacement launch.

Keep failures and recent completed output inspectable with bounded retention.
Durable logs and session accounting survive view removal. Removing a view must
use the keep-terminal operation; disposing an owned worker follows the declared
stop/retirement protocol. A closed tab never means the work was delivered.
Keep current adapter finalization, which always tears down its window. Retain
the latest completed/failed phase log and transcript and expose the existing
Paradev file/output view. Active session tabs remain real worker PTYs. Parked
terminal-history retention is deferred; never skip teardown to preserve a tab.

#### Packaging and portability

Port the transport, bootstrap/output reader, minimal authenticated host client
and relevant fake-host tests with source provenance and applicable notices.
Record the owner's authorization to incorporate the tooling and audit source
ownership; do not treat its vendored BMAD license as a license for the transport.
Replace checkout imports and hardcoded paths with package-local runtime and
CLI discovery. Route Git calls through `verify.py`, use the existing portable
locking seam, register core environment variables and policy defaults, and keep
POSIX shell construction inside the sanctioned transport boundary.
Provide a bundled context-verification entry point for account wrappers, with
no external script path. Preserve worker-owned hook/account attribution while
removing inherited parent-terminal identities.

Bundle `paradev` as an explicitly selected backend with lazy optional imports
and an optional dependency extra for its output stream support. It must not
change platform defaults or break standalone installs when Paradev is absent.
Register it through the builtin loader without an automatic platform match.
Detect a conflicting external `paradev` plugin and version existing transport
records; do not replace an active backend or silently adopt its live records.
There is no separate program named mux to install: mux is the transport seam.
Preflight checks the installed Paradev CLI/host schema, project registration,
checkout allocation, native hook/account context and terminal/pane support.
Qualify macOS first; advertise other platforms only after their transport gates.

Persist reflection and backend selection for later attach, stop and resume;
configuration is fixed for the lifetime of a run. Host unavailability,
schema mismatch or lost acknowledgements remain visible as blocked/unknown
observations. Do not fall back by launching a replacement worker in another
transport. A missing view alone does not stop the existing worker.

### Optional hcom awareness

Use hcom as an optional session communication integration. The existing fork
already forwards hook-reported hcom identities and can send recovery nudges to
a listening actor. Extend awareness around the campaign roster rather than
introducing a second scheduler. Standalone execution must not require hcom.

Supply each worker with relevant sibling units, roles, branch/PR links,
ownership and planned interface contracts. Where hcom is available, workers
can inspect bounded peer progress and transcripts, share findings and ask
targeted questions. Important interface or dependency decisions must also become
durable product artifacts; a message saying work is ready cannot release a
dependent unit before confirmed delivery.

Keep the configured project room and give each campaign unique routing tags
and threads. These are filters, not isolation boundaries. Persist room, campaign,
unit/attempt, hcom actor/process generation, native conversation and Paradev
terminal identities separately. Discover and validate an existing worker's
registration; do not wrap an already enrolled launch chain in another hcom
session or assume a reused name belongs to its former worker.

Address only exact, currently bound campaign recipients; avoid room broadcasts
and implicit thread membership for scheduler-originated messages. Bound message
volume, requests and context reads. Persist event cursors and handle database
reset explicitly. Expose unavailable or partial observations instead of treating
them as an empty, healthy roster. Reconcile uncertain sends without blindly
repeating requests or interpreting accepted delivery as processed input.

hcom text and activity remain advisory. They cannot assign work, transfer an
editing lease, authorize policy changes, establish completion or satisfy review
and merge gates. Only BMAD launches, resumes and stops owned coding sessions.
Peer sessions must not use hcom lifecycle commands to create replacement
executors or stop each other. Operator holds apply to hcom nudges as well as
terminal input. Autonomous peer delegation and message-driven scheduling are
outside this change.

#### Stop-hook compatibility gate

Automatic inbound peer delivery is disabled until its completion interaction is
qualified for the selected launch chain. In the source revision named by the
installed hcom image, Claude/Codex Stop handlers can mark an actor listening and
wake delivery asynchronously; Claude stdio Stop can also block for unread mail.
BMAD independently accepts a Stop with a result and can begin window teardown.
Those paths can race: a queued continuation may be cut off, or a new turn may
start after BMAD accepted the previous turn's result.

A listening status, send receipt, zero unread count or `stop_hook_active` field
alone does not resolve that race. Define and qualify admission/closing semantics
for queued, in-flight and late messages, preserving hook/window completion
authority and post-session verification. Do not solve it with sleeps or a new
prose/idle-based completion path. If the installed integration lacks the needed
capability, report that and retain the supported communication mode.

Observe-only operation also needs capability checks: a launch wrapper may have
already installed automatic hcom delivery hooks. Merely stopping campaign sends
does not make those hooks passive. Do not claim a passive mode unless incoming
wakeups are demonstrably excluded or the closing protocol handles them. Active
delivery can remain deferred while structured campaign context and supported
peer observations provide initial awareness.
The inspected hcom version has no supported per-actor mute/observe setting;
disabling automatic subscriptions does not disable ordinary inbound mail.
Observation means BMAD emits no messages/subscriptions, while preexisting actor
delivery can remain active. A stronger no-wakeup guarantee requires a qualified
unenrolled launch or a new delivery capability, not an inferred configuration.

### Planning

Run requirements, architecture, decomposition/refinement and planning review as
explicit coding-CLI stages with structured result artifacts. After hook/window
completion, independently validate stage/input identity, confined artifact paths,
nonempty acceptance criteria, dependency acyclicity and preservation of landed
work. Commit durable product artifacts and synchronize the resulting hierarchy.

Existing ticket, stories and sprint sources remain usable. The campaign can
import their work into a dependency graph. Sources without explicit independent
dependencies retain their existing serial ordering unless the campaign declares
independence.

## Implementation plan

| Stage | Deliverable                                                     | Acceptance                                                                                                                                          |
| ----- | --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | Versioned campaign specifications, state, transitions and store | Invalid/cyclic inputs refuse before effects; persistence is atomic; duplicate coordinators refuse.                                                  |
| 2     | GitHub gateway, outbox and publication controller               | Early drafts, pagination, exact-head/base gates, unknown-write reconciliation and merge read-back work through fake transport.                      |
| 3     | Issue hierarchy, managed content, intake and triage             | Stable IDs, native subissues, bounded new work and linked dispositions survive retry without duplicates, lost human edits or premature closure.     |
| 4     | Repository discovery, planning sessions and source import       | Existing work informs scoped plans without duplicate ownership; available Astra/Fable roles and structured results are validated.                   |
| 5     | Isolated workers, workspace providers and dependency scheduler  | Multiple units execute concurrently without shared engine state; provider allocation precedes launch; dependencies release only after delivery.     |
| 6     | CLI and complete delivery/recovery loop                         | One command plans, dispatches, resolves permitted failures, reviews and delivers; bounded recovery preserves identities, work and human-only gates. |
| 7     | Bundled Paradev transport and native workspace reflection       | Story/epic folders, unit workspaces/PRs and actual worker tabs reconcile without duplicate launches or lost operator holds.                         |
| 8     | Optional hcom awareness and compatibility checks                | Exact actor bindings, scoped observations and operator holds work; automatic inbound delivery remains unavailable until Stop interleavings qualify. |
| 9     | Qualification and migration                                     | Zero-token end-to-end tests cover crashes, moving heads and native reflection; project preflight proves readiness before replacing its automations. |

Implementation ownership follows these responsibilities: neutral model/store and
CLI; GitHub transport/publication; planning/project synchronization; and worker
supervision. Public interfaces are shared before parallel editing begins.

## Validation

Use sandbox project fixtures, scripted coding CLIs and fake GitHub responses.
Live/E2E tests consume zero model tokens. Test loss of a response after a
successful remote mutation, restart at every outbound state, concurrent
admission, stale workers, changed policies/heads/bases, held reviews, failed or
missing checks, planning changes and a closed unmerged PR. Ablate refusal gates
to prove their tests detect a removed guard.

The complete fixture must show brief to plan/spec commit, draft PR, linked issue
hierarchy, concurrent workers, exact-source review/verification, confirmed merge,
tracking reconciliation and dependent dispatch. No unit may become delivered
before the remote merge and fresh canonical ancestry observation.

Reflection qualification uses fake host/CLI responses plus scripted zero-token
PTY sessions. Cover workspace creation with a lost response, branch/path
rewrites, foreign checkouts, simultaneous child units, competing tags, task
update conflicts, host loss/restart, missing panes, output gaps, native hook
identity, worker exit, operator holds and retention. Prove a pane attaches the
existing worker terminal and cannot cause a second launch. Standalone Linux and
Windows installation/import tests must pass without reflection dependencies.

Communication qualification uses fake hcom responses and scripted hook/PTY
events with zero model tokens. Cover both hook orderings, a result followed by
unread delivery, zero unread with delivery in flight, late mail, provider errors
without Stop, stale generations/name reuse, parent identity/room leakage,
database reset, stop/recovery and operator takeover. Existing mocked nudge tests
do not establish compatibility with automatic peer delivery.

### Architecture review decisions

The implementation contract specifies native attribution read-back, task-free
reflection, bundled-backend migration and output history after teardown.
Optional hcom observation is included; new automatic peer wakeups remain deferred
until their closing behavior qualifies. GitHub's asynchronous merge API and queue
candidate semantics are explicit. REM activation must resolve its non-strict
required checks through its explicitly authorized native publication mode, without
claiming strict server-enforced base freshness. Its primary-session decision
retires the custom merge scripts; actual landing acceptance and bounded repair
remain required. Managed controller bridges provide addressable sessions and
safe-boundary steering while mandatory wrapper enrollment uses isolated native
rooms. No companion hcom release or GitHub ruleset change is required.

## Alternatives

- Removing the existing `max_parallel` clamp does not create a scheduler; the
  engine still dispatches synchronously.
- Threads sharing the existing engine would share its workspace and journal
  context, and full-state writes do not merge concurrent ownership changes.
- Moving the current advisory post-story publisher into the package unchanged
  would retain premature local completion and external merge ownership.
- Blindly importing repository-specific factory scripts would retain hardcoded
  paths, policies and automation dependencies. Reuse their tested contracts
  through the native adapter, process, Git and persistence seams instead.

## Research references

- [Pull request creation, update and merge APIs](https://docs.github.com/en/rest/pulls/pulls)
- [Pull request review APIs](https://docs.github.com/en/rest/pulls/reviews)
- [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Merge queues and current-base CI](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- [Milestone APIs](https://docs.github.com/en/rest/issues/milestones)
- [Native subissue APIs](https://docs.github.com/en/rest/issues/sub-issues)
- [API pagination, serialization and rate-limit practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
- [Paradev workspace, task and terminal CLI](https://docs.paradev.com/cli/getting-started)
- [Paradev workspace and agent model](https://docs.paradev.com/superset-model)
- [Existing native transport source, port baseline](https://github.com/open-runtime/paradev-tooling/blob/a5d76d363da3cca58db5e110942c1490a49f8fae/superset_tooling/native_transport.py)
- [Paradev fork folder membership and ordering, inspected source](https://github.com/tsavo-at-pieces/Paradev/blob/f8d0a3131def87cfd525ece3ace6867540011610/apps/desktop/src/renderer/routes/_authenticated/utils/workspaceTagFolders/workspaceTagFolders.ts)
- [hcom Claude Stop delivery, installed-image source revision](https://github.com/tsavo-at-pieces/hcom/blob/c578fe92ccb714572801e60fa29555c0b6a1929c/src/hooks/claude.rs)
- [Existing BMAD hcom recovery nudges](../loop-recovery-guide.md#nudges-over-hcom)

The folder constraints above were inspected in the local Paradev fork source;
CLI capabilities were checked against the installed command schema. Qualify the
installed desktop/host against those expectations rather than assuming a source
checkout proves the running app's version or behavior.
The hcom source check establishes the interaction to qualify; it is not a live
installed-binary reproduction or proof of an unmodified build.
