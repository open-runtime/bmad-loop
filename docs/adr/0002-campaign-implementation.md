# ADR 0002 implementation contract

- **Status:** Implemented; release-specific qualification and target-project delivery remain gated
- **Date:** 2026-11-05
- **Parent:** [Native SDLC campaign](0002-native-sdlc-campaign.md)
- **Delivery:** Initial campaign PR and reviewed follow-up releases preserve the same delivery contract

This contract resolves implementation choices in ADR 0002. The experimental
campaign implements these boundaries above the existing serial engines; integrated
qualification must establish that its runtime and recovery paths hold. The
[campaign guide](../campaign-guide.md) describes its current commands. Repository
policy, migration and installation qualification remain activation prerequisites.
The accepted [reasoning-job and handoff contract](0003-bounded-reasoning-and-handoff.md)
extends this work without weakening its acceptance or publication gates.

Final handoff views use the ADR 0003 closed selection contract: the model orders
immutable source references, and Python renders attributed historical claims.
Exact owner requirements and controller-observed holds remain separate mandatory
context. Changed controls invalidate continuation before writer admission; each
new final-selection or final-audit contract requires its own native qualification.

## Implementation goals and traceability

The initial implementation and released follow-ups established the historical
G0–G8 foundation below. The delivery goals distinguish established qualification
from checks that each changed release and target-project pilot must still satisfy.
The owner's administrative-publication decision supersedes this ADR's strict-only
REM activation requirement; strict and queue modes retain their existing contracts.

| Delivery goal                                | Implementation and acceptance                                                                                                                                                                                              | Status                                                                                                                                                                      |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1 — Hold and route admission                | Headless hold before preparation/launch; inherited developer; shared frozen launcher/permission readiness; standalone writer capabilities; no attempt charge while unavailable; old snapshot compatibility                 | Implemented; focused regressions and ablations passed                                                                                                                       |
| D2 — Owner-authorized publication            | Repository-bound opt-in; ordinary exact-head GitHub merge with authorized administrative fallback; no independent GitHub approver requirement; durable uncertain execution and landing verification/repair before tracking | Implemented and regression tested; real target delivery requires separate activation evidence                                                                               |
| D3 — Native routes and writable continuation | Qualify enabled Claude/Codex/Grok/AGY/Muse model/effort/permission recipes; real writable Claude↔Codex continuation; installation-bound proofs                                                                             | Nine installed tool grants and retained no-tools semantic routes qualified; both native writable directions demonstrated; changed installation-bound grants require renewal |
| D4 — Managed communication                   | Own actor per unit/job; factual bridge status; durable safe-boundary steering; accepting/closing/closed generations; no identity borrowing or reopened writers; Paradev bindings                                           | Implemented; actual isolated hcom and scripted managed-load qualification passed                                                                                            |
| D5 — Final candidate qualification           | Full suite, type/lint/package checks, 48 stories/24 epics/eight verification slots with managed communication, two independent candidate-bound reviews                                                                     | Prior suite, load, package and review checkpoints passed; every follow-up candidate requires applicable affected checks and independent review                              |
| D6 — Deployment and REM delivery             | History-preserving PR merge, exact fork installation and canonical base, refreshed fork setup skill, controlled two-epic real delivery, verified automation responsibility and concurrency configuration                   | Merged installation and generic onboarding demonstrated; each release and target require applicable renewal and selected delivery evidence                                  |

The completion contract includes
accepted reviews, synchronization with main, history-preserving merge, exact-commit
installation, canonical-base update and qualified REM activation. Do not mark the
ultimate objective complete at the review-ready boundary.
Renew only proofs invalidated by an installed path, dependency or contract change;
validate unchanged retained semantic evidence without repeating its inference.
Installed startup readiness does not establish actual parallel target delivery,
identity reflection, outside-peer communication or live-worker drain/resume.

### Portable operations extension — accepted 2026-11-11

The next follow-up implements the following additional goals. Earlier release
qualification does not establish these new behaviors. Preserve the existing REM
pilot's evidence and selected branches; it remains stopped until affected startup
and route qualifications pass. Its selected work is issues #1582 and #529.
The owner-authorized November 10 cleanup archived its three workspaces and retained
the selected branches in preservation draft PRs #2331 and #2332. Recovery must use
verified adoption of those branches, not recreate deleted native identities or
blindly resume a snapshot whose workspaces no longer exist. Those drafts contain
specifications, not delivered implementations. Active unrelated REM sessions remain
outside this recovery operation.

| Goal                         | Contract and completion evidence                                                                                                                                                                                                                                            |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P1 — Entity reflection       | Initiative-prefixed epic folders, stable story/worktree labels, phase tabs, actual session actors and explicitly labeled controller endpoints. Stopped reflection creates no workers or workspaces; preserve manual overrides and unrelated tags.                           |
| P2 — Portable continuation   | Stage-specific ordered summary pools, stronger final synthesis, independent audit-family reservation, exact qualified fallbacks and incremental managed-history capture. Normal-runtime interruption recovery retains the same worktree/PR and every unresolved obligation. |
| P3 — Repository organization | Separate audit/reconcile authority; complete issue/PR/milestone/ref/worktree inventory; automatic evidence-backed links, allowed labels, consolidation and duplicate/superseded closures. No branch, worktree or history deletion.                                          |
| P4 — Startup configuration   | Explicit revision-checked route/grouping changes before unit work; immutable prior specifications, templates and capabilities; retain ended optional jobs and confirmed allocation/tracking history. Preview and apply never resume the campaign.                           |
| P5 — Deployment and delivery | Deploy reviewed TeamRouter quota repair through maintained native setup, merge and install the reviewed BMAD follow-up, renew affected route proofs, migrate the stopped pilot, and deliver both selected REM stories through review, merge and tracking reconciliation.    |

Native subagents implement reflection, handoff/history and repository audit in
parallel; the primary owns shared configuration, integration, documentation and
publication. Regressions remain zero-token. Bounded native qualification is a
separate operation, with exact route/effort/permission and semantic evidence.
Record implementation, normal-runtime tests, native proof and target delivery
separately. Missing capacity or proof must remain visible rather than silently
changing the requested configuration or claiming the whole extension complete.

Portable continuation includes bounded recovery from a final summary rejected
by its independent audit. Preserve the rejection, request a new synthesis from
the same source and findings, and require another independent audit. Two repair
attempts per predecessor launch survive refresh and restart; they do not reset
native retry debt or grant authority to resolve an owner decision. See
[the handoff synthesis contract](0003-bounded-reasoning-and-handoff.md) for source,
quiescence and evidence requirements. Qualify the normal rejection, correction,
audit and packet path separately from a model's successful process exit.

Repository audit is read-only remotely and may persist local evidence. Reconcile
uses a frozen standing Organize-and-close policy, fresh object/ownership checks,
independent semantic review and durable uncertain-write reconciliation. Preserve
original issue text and review history. Consolidation must account for every
requirement; PR closure must account for unique contributions and prove writer
release. Milestone closure needs complete membership. Scope/source/code changes
go through delivered planning or implementation PRs. Defaults bound one cycle to
100 mutations and 20 closures, checkpointing excess work. Do not infer completion
from age, inactivity or semantic similarity.

Catalog completeness is separate from action-specific ownership-observer
coverage. A truncated frozen inventory remains incomplete across audit, resume,
reconciliation and status, including older ledgers that incorrectly recorded
completion. It cannot admit new reasoning or remote mutations; obtaining complete
coverage requires a new audit with sufficient bounds. Already issued native jobs
and uncertain GitHub mutations retain read-only receipt/readback settlement and
owned-controller closure, including while stopped. Preserve their original
attempts, counters, errors and immutable intents; observation never replays a
write or makes the incomplete audit complete. Missing ownership evidence still
constrains only actions requiring that evidence when the catalog itself is complete.

New campaigns use epic grouping; legacy snapshots with omitted grouping retain
story grouping. Initiative grouping is also supported by this extension; sprint
grouping requires a future explicit source identity and implies no calendar or
capacity policy. Folder display names use native settings separately from stable
membership tags. Only recorded BMAD-owned tags may change during regrouping.
Session identity never becomes workspace identity. No cleanup of the existing
pilot is authorized merely by its stale pending labels.

Phase and actor identity belong to each terminal/session view. Preserve existing
tab/pane layout and operator tab labels; a shared tab cannot represent a single
actor when it contains multiple sessions. Historical phase views reattach exact
retained terminals and never create a substitute live session.

REM's committed 2026-11-10 owner policy removes mandatory cross-family publication
review while retaining its native review cycle. The stopped pilot's revision 8
predates that direction. Support explicit startup adoption through
`--review-policy-source COMMIT:PATH`: bind a committed regular document in current
local ancestry, permit only `github.require_independent_review`, append
that exact decision source if absent, and archive the old/new value and source.
All unit/attempt, hold, check, native-review and budget protections still apply.
Ordinary route/group changes cannot change this policy or remove configured roles.

#### Transfer of an archived BMAD-owned branch

The supported operation provides startup-only transfer from an archived,
stopped native BMAD campaign to a named successor. No unit writer may have
attempted work; ended optional reasoning retains its independently validated
request, receipt, grant and process history. It does not generalize factory migration or
treat archived terminals, old PIDs, titles, disabled automations or a remote PR as
an ownership release. Ordinary native observation never grants release authority;
the bundled campaign observer reads the immutable predecessor retirement and
combines it with fresh scoped native observations. Keep the operation separate from harness continuation and from the
existing `release` command, which removes an operator hold.

Sandbox qualification exercises ordinary preparation, archived native observations,
SQLite transaction faults, older-reader refusal, named-successor adoption and
ordinary scripted delivery. Native and GitHub services and coding sessions are
scripted; these checks do not claim an operational campaign transfer. Each live
operation must earn its own fresh preview, release and adoption evidence.

Create the successor through an ordinary non-launching preparation path: parse,
qualify and freeze its specification, then persist it stopped and unallocated.
Do not create an active campaign and race a stop request. Transfer preview reads
current authority and returns exact proposed coordinates without saving state,
registering workspaces, submitting effects or calling a model. Applying the
explicit transfer does not resume either campaign.

The release record binds one predecessor campaign/unit and one successor
campaign/unit, repository/common directory, selected source identities, preserved
branch/head, positive PR number/node ID, target/base, native archive identity and
preservation evidence. Preserve the original unit bindings, attempt counts,
configuration, failed jobs, histories and source preimages. A preservation PR may
have been created after the predecessor stopped: qualify its actual repository,
branch and head rather than inventing a prior PR binding. PR-less transfer is
outside this narrow contract. Neither transferred nor unfinished work becomes
delivered or canceled merely to suppress its old writer.

Fresh release qualification requires a stopped predecessor with no unit writer
attempts, complete hot and cold job ownership, and no unresolved mutating effect.
The first release permanently retires the whole predecessor, including unmapped
work listed explicitly in preview; additional mappings can name only the same
successor. Issued uncertainty follows its existing observation/reconciliation path; it is
never relabeled never-issued. Pending reads retain their identities and history.
Existing dirty or unpublished work must be preserved before release. An absent
archived checkout needs its retained preservation evidence and current immutable
branch identity; absence alone never proves a clean retirement. Inspect exact
native project/workspace/terminal identities and other campaign claims covering
the selected branch, checkout or ownership scope. Unrelated live REM sessions do
not require a global drain, and no transfer command stops or moves them.

The predecessor is the single release authority. Collect bounded observations
before the write transaction, then recheck revisions and local bindings under the
existing repository coordinator lock and store CAS. Commit an immutable release
record, whole-campaign admission retirement and event naming the sole successor.
A lost reply is resolved by reading that same record; repeating the operation cannot choose another recipient. A
crash before successor adoption leaves the predecessor released, never silently
reopened. Ordinary resume, operator-hold release and configuration changes cannot
remove this exclusion. Current unit/session admission and mutating-effect guards
must refuse the retired predecessor globally; ordinary saves cannot change its
unit, configuration, job or effect authority. Read-only observations and retained
history remain available.

Storage compatibility is conditional, not a migration of every campaign. Keep
ordinary creation and untouched stores at SQLite schema 3. A predecessor earning
release authority commits its record, event and `PRAGMA user_version=4` in the
same transaction, then publishes the schema-4 SQLite marker. Existing readers
reject database version 4 even if a crash leaves marker 3. Updated readers accept
that recoverable pair and complete marker publication; later commits and marker
updates never downgrade version 4. Marker-only fencing is insufficient because
inventory and history also open the database directly. All store, inventory,
event, job and action-history paths must understand the conditional format.
Legacy JSON authority uses the existing supported quiescent migration before
transfer, rather than adding a release field that an old reader would ignore.

A bundled campaign observer converts only these persisted releases and fresh
scoped observations into the existing authenticated `ServiceInventory` contract.
Its actor identifies the named successor unit and reports admissions closed,
zero active scoped workers, preserved source/branch identity and released
ownership only when proved. Fence and binding revisions derive from the immutable
release, not unrelated event clocks. Existing adoption still checks current
GitHub repository/PR/head/base, source authority, native ownership and preserved
ancestry before each mutation or actor admission. It retains all acceptance,
review, quota and publication gates. Caller-authored JSON cannot supply proof.

When the old native workspace is archived, adoption allocates a new registered
checkout on the preserved branch through the normal durable allocation action.
The stock local workspace command supports an existing branch; use the exact
branch without a namespace rewrite or an agent-launch flag. Reuse the allocation
intent marker and exact UUID readback for lost acknowledgements. Confirm native
project, checkout path, common directory, branch and head before source setup.
Keep the predecessor UUID/path as historical coordinates; do not revive or rebind
them. A current conflicting checkout or ambiguous allocation remains a visible
refusal. Continue through the existing adopted-PR preparation and delivery flow.

| Boundary            | Required zero-token acceptance                                                                                                                                                 |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Normal transfer     | Real fixture Git history and ordinary prepare/stop/release/adopt flow deliver through the same preservation PR without duplicate intake or implementation.                     |
| Release authority   | Wrong repository/source/PR/head/revision, another recipient, dirty preservation and scoped live/unknown ownership refuse; unrelated live peers remain eligible.                |
| Permanent exclusion | Predecessor resume, hold release, reconfigure and queued writer/publication effects cannot reclaim a released unit; failed attempts and original bindings remain unchanged.    |
| Compatibility/crash | Old readers reject only upgraded campaigns; transaction rollback, database-4/marker-3 interruption, marker repair and subsequent saves preserve the release without downgrade. |
| Native allocation   | Existing-branch creation and lost acknowledgements reconcile one new workspace UUID, preserve old history and never reuse or close another actor's session.                    |
| Adoption freshness  | Moving source, PR, ref or ownership evidence invalidates the observation before mutation; provider output alone cannot waive ordinary adoption checks.                         |

Use guard-removal ablations for every refusal. The planned core is a cohesive
transfer module plus conditional store compatibility; shared runtime/CLI wiring
owns permanent admission/effect exclusion and non-launching creation. Adoption
and Paradev collaborators own the verified observer and allocation integration.
Scripted qualification, installed-provider qualification and actual REM transfer
are separate results; this contract authorizes implementation, not a claim that
the preserved pilot has already transferred.

### Accepted completion extension — 2026-11-08

Reconcile every earlier requirement against its ordinary CLI/runtime path, tests,
native capability and deployment evidence. Earlier candidate results are historical
evidence, not qualification of later changes. Keep coherent commits in this PR and
preserve their ancestry through merge.

REM's current primary-session publication decision retires both custom merge
scripts. Do not finish or restore the obsolete REM-specific executor or case-file
integration. Use ordinary GitHub publication with an exact-head condition and an
explicit repository-bound administrative authorization. Preserve generic trusted
project-command support for other repositories. An uncertain merge is reconciled
before replay. Holds, selected reviews, applicable acceptance and mandatory checks
remain required; no additional coordinator approval or ruleset change is added.

Post-merge acceptance must make progress: verify/review the actual landing when
its tree and ancestry are not already covered, then accept it or create one linked
repair unit. The repair depends on the confirmed merge event rather than original
delivery. Keep original completion and parent tracking pending until repair and
acceptance succeed. Do not chase unrelated later main revisions indefinitely.

Managed communication supplies a distinct addressable binding for every coding
session and reasoning job. Native delivery requires qualified closing behavior;
otherwise a controller bridge gives factual status and durable safe-boundary
steering. Acknowledgement distinguishes transport submission, BMAD acceptance,
model delivery and application. Never reopen a writer for late mail. An hcom PR
is not a prerequisite for this design. TeamRouter is required where the selected
route supports it; other routes explicitly report their native routing backend.
Steering selects the exact unit/job before applying page and byte bounds, so
another job's backlog cannot hide the target's accepted message. Prepared context
remains immutable; later mail waits for the next boundary.

Qualify actual native review invocation separately from prompted model review.
REM's selected base comparison and complementary native review retain exact
candidate/base and complete findings. Configure the optional `native_review_pair`
role to make these two actual native reviews a required durable stage before
independent publication review. The schema-mapping turn is a third turn, never a
substitute for either review. Retain both delegate artifacts and their combined
findings. The mapping turn receives the immutable original task and input as well
as the findings; delegated review context is not assumed to exist in the parent
conversation. The pair consumes ordinary review/shared capacity, binds the exact
candidate, base, graph and configuration, and is invalidated with candidate drift.
Missing or single-review capabilities cannot satisfy the pair. Pair findings
enter the existing same-PR repair path; its receipt cannot waive independent
review after mixed-family authorship. Omitted roles retain legacy behavior and
configuration identities. Complete writable Claude-to-Codex and
Codex-to-Claude continuation through the normal runtime, with predecessor
quiescence, current constraints, preserved dirty work and same-PR delivery.

Ordinary publication reviews and native review pairs share freshness checks for
candidate, integrated base, graph, source and frozen configuration. Live intake can
invalidate an approved review even when its candidate commit has not changed.
Before submission, that condition returns the unit to verification and review;
it cannot become a permanent remote wait. Already submitted or uncertain merge
writes retain their receipts until reconciliation, and delivered history remains
intact. Regression coverage injects a new issue through the running coordinator
after approval and requires fresh review followed by merge and tracking closure.

Bulk reasoning must partition oversized inventories with bounded source-addressable
reduction rather than requiring operators to split campaigns. Final scale proof
uses 48 stories, 24 execution workers/active epics, eight review slots, eight
verification slots, four reasoning jobs and **32 shared sessions**. Exercise
managed messages, recovery and eventual tracking closure under this exact budget.

After final checks and independent reviews, merge, install the exact merged fork,
update the canonical base and revalidate installation-bound routes. Demonstrate
two unclaimed disjoint REM stories from different epics and configuration-driven
onboarding in a non-REM repository. Retain active owners and unrelated automations;
never revive retired factory infrastructure. The initial native writer deployment
is qualified on macOS; unrun host/platform capabilities remain explicit.

The initial 32-session legacy load run reached delivery, and the qualified
headless three-story campaign reached delivery with all managed endpoints closed.
Real hcom qualification covers an outside peer's status request, durable steering
delivered through a normal reasoning child, and closed-generation late mail.
The larger qualified-native load exposed a separate Git visibility defect: a
shared branch ref could advance before its quarantined objects were promoted.
Private writer refs/index and seal-before-CAS publication now resolve that defect.
The subsequent zero-token managed load delivered all 48 stories, reconciled parent
tracking and closed all 347 public bindings without bridge errors. It reached
24 execution workers/active epics, eight verification slots, eight review slots,
four reasoning jobs and 31 of 32 shared sessions. All 47 injected messages were
applied. Stop acknowledgement took 0.429 seconds; the complete test took 1,384.99
seconds. The controller, scheduler, Git publication and bridge sources were stable
through this run. Four native-protocol leaves changed during the run but are not
executed by its scripted Claude transport; this result is not a whole-tree frozen
qualification or a real-provider throughput benchmark. Final candidate regression
and independent reviews remain required after those native changes settle.

Scoped command lifecycle evidence covers owned sessions and observed descendants.
An actual detached-and-reparented child can evade periodic observation. Keep that
limitation explicit: filesystem confinement is not universal daemon containment,
and no qualification may turn a finite foreground check into such a claim.

The matrix records the earlier candidate's implementation responsibilities, production entry points
and acceptance evidence. Paths identify the implemented runtime integration seams.
Pending means the named qualification remains to be demonstrated. Update each
row from concrete evidence, keeping implementation, runtime-tested, native-qualified,
review-ready and activated states distinct. Qualification artifacts remain outside
tracked documentation; durable behavior and test references belong here.

| Goal / responsibility                   | Production path                                                                                  | Required regression or qualification                                                                                                                                   | Native qualification                                                       | Status                                                                                                                                      |
| --------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| G0 — Upstream integration and contracts | Sanitizer, adapter/probe/mux seams; ADR 0002/0003                                                | Incoming security and reliability changes preserve fork behavior; all requirements have code/test ownership                                                            | Recheck affected installed transport behavior                              | Upstream integrated; static/package/regression checks passed at the named checkpoints below                                                 |
| G1 — Autonomous progress                | `campaign_runtime`, `campaign_remote_feedback`, `campaign_runtime_decisions`, scheduler/capacity | Temporary wait clears and reaches merge; new owner evidence re-arms resolution; mixed-model receipts pass; allocation, fairness and expiry use real runtime state      | Served-model and structured quota receipts                                 | Runtime progress repairs, integrated regressions and fresh final-candidate load/recovery qualification passed                               |
| G2 — Acceptance and publication         | Planning/source flows, `campaign_publication`, queue runtime and merge adapter                   | Criterion proof/amendments; reviewer recruitment; H/G checks; actual executor submission; uncertain writes and tracking recovery                                       | Eligible GitHub identity, required App identity and project executor       | Ledger/publication integration fixtures implemented; target-project identity and executor qualification remain activation gates             |
| G3 — Durable reasoning jobs             | Store/supervisor/worker seams and new job model                                                  | Parallel job claims, asynchronous lifecycle, typed outcomes, bounded limits, restart and drain/terminate                                                               | Qualified context delivery and completion for each advertised route        | Durable jobs, SQLite and runtime integration passed integrated checks; exact Grok/Muse routes qualified                                     |
| G4 — Harnesses and execution routing    | Profile registry, adapters and campaign worker dispatch                                          | Claude/Codex/Grok Build/AGY/Muse capability contracts; approved development pools; selected/served/auxiliary attribution                                               | Launch, context, native identity, effort, usage, stop and supported resume | Five writer routes, read-only routes and native review pair earned; changed transports and installed locations require renewal              |
| G5 — Portable handoff                   | New history readers, source indexes, reducer and handoff flow                                    | Coverage/citations, incremental caching, destination budget, worker fencing, dirty-work and pending-effect preservation                                                | Controlled corpus and sealed full-conversation trial                       | Runtime, full-corpus coverage, independent owner audit, exact retrieval and both native continuation directions passed                      |
| G6 — Semantic decisions                 | Intake/planning/decision flows plus reasoning jobs                                               | Event-triggered routing, triage, semantic sync, priority, graph review and reporting; stale/unauthorized proposals rejected                                            | Approved Astra/Fable roles; Astra-only conflict repair                     | Runtime hooks and semantic fixtures pass; approved Astra/Fable source routes qualified; installed/project activation remains separate       |
| G7 — Operator integration               | CLI projections, `campaign_paradev`, `campaign_communication`, decision evidence provider        | Tasks, role attach, completed history, nested diagnostics, qualified grouping, bounded hcom cursors and normal-CLI transcript binding                                  | Sidebar/tab/session identity and supported wrapper enrollment              | Runtime/CLI fixtures implemented; native Grok terminal, identity, task and history projection qualified on macOS; other routes remain gated |
| G8 — Scale and review readiness         | Coordinator, discovery/store, packaging and operator surface                                     | At least 48 stories/24 active epics/eight verification slots plus reasoning jobs; responsive controls; bounded hot state/context; final checks and independent reviews | Actual harness operation; explicit unrun platform/activation gates         | Final frozen suite, package, both 48-story load cases and independent scoped reviews passed; installed activation remains separate          |

The nine earlier review repairs remain mandatory regressions, not newly missing
features: unknown provider concurrency uses configured limits; attribution excludes
unrelated review sessions; stop consumes terminate-workers; merge retry preserves
hold labels; configured checks and outstanding review requests block publication;
rename validation checks both paths; multiple ticket selectors preserve private
store identity; hierarchy completion waits for asynchronous effects. Preserve
checkpoint-safe append-only recovery, frozen-policy reload suppression and ordinary
macOS temporary-path socket coverage. An isolated rerun does not resolve an xdist
failure.

Before final review, reconcile every unresolved GitHub review thread and findings
present only in review summaries, including comments on older revisions. Bind
each disposition to the current production path and a regression that exercises
the reported condition. Outdated line anchors and a green aggregate suite do not
establish that a finding is fixed. Ownership validation must agree with admission
and checkpoint grants, source freshness must include selected native requirements,
and unavailable route or suppressed-action observations must not abort unrelated
work. Preserve explicit unknown contributor attribution and refused transition
journaling. Only resolve a review thread after its actual defect is addressed.

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
`test_campaign_paradev.py`, `test_campaign_communication.py`,
`test_campaign_history_warmup.py` and `test_campaign_runtime_history_warmup.py`.
These suites validate production dispatch and guard behavior; the native column
records separate qualification obligations and earned evidence, including final
publication executor, real installed harnesses, semantic fidelity and operator UI.
The [qualification boundary](0003-bounded-reasoning-and-handoff.md#qualification-achieved-and-remaining-gates)
records the completed history, integrated regression and fresh load checks, with
remaining route and activation gates. The frozen code candidate `3fa00c5d` passed
20,932 regression tests with 261 skips in 518.69 seconds; the dedicated managed
48-story load passed on `3efacd77`. Pyright, Trunk, package build, isolated wheel
checks and release checks passed. Independent reviews cleared the launch-lock
control race, history prompt/schema mismatch and late optional-warmup admission
repair. A subsequent audit of historical GitHub reviews reproduced unresolved
ownership, hold, source-freshness, attribution and observation defects despite
that green suite. Those repairs received independent scoped reviews and resolved
all twelve historical GitHub threads. On the subsequent frozen `75ed1b5d`
candidate, 21,023 regression tests passed with 261 skips and three failures:
inconsistent bundled skill sources and two direct-control fixtures that omitted
normal store initialization. Correct the metadata and fixtures without weakening
the missing-hold-namespace refusal, then repeat the affected and final checks.

The same frozen candidate passed the managed 48-story load in 1,063.12 seconds,
with all 632 source/test hashes unchanged. It delivered every unit and parent
tracking, applied or closed 46 messages, and closed all 256 managed endpoints.
It reached 24 execution workers/active epics, eight verification slots, eight
review slots and four reasoning jobs within 32 shared sessions. Stop acknowledgement
took 0.969 seconds. This is scripted integration evidence; it does not establish
real-provider throughput. Later changes require impact assessment and affected
qualification, and these checkpoints do not satisfy writable continuation,
installation-bound route proofs or REM delivery.

The frozen `e57061ea` regression passed 21,066 tests with 261 skips and three
failures. One fixture omitted the applied producer state and retained obsolete
artifact-size expectations. Two production races remained: a messaging observer
could open SQLite before schema initialization completed, and queue publication
could land between remote reads, leaving a prepared dequeue unable to reconcile.
The repairs serialize connection/schema setup with a bounded cross-process lock
that is released before ordinary SQL transactions, and route terminal queue reads
through canonical landing verification. A pending dequeue rechecks terminal or
removed membership before preparing another mutation. Exact PR identity, accepted
head and checkpoint ancestry remain required. The regressions exercise controlled
interleavings and complete production campaigns; final integrated checks remain
required for the repaired candidate.

The frozen `e7f409e8` candidate passed the managed 48-story load in 1,710.02
seconds with every tracked file unchanged. All stories and parent tracking
completed, 46 messages were handled and 365 endpoints closed. Peaks remained
24 execution workers/active epics, eight verification slots, eight review slots,
four reasoning jobs and 31 of 32 shared sessions; stop acknowledgement took
1.028 seconds. The full regression returned 21,086 passes, 261 skips and 20
failures: sixteen queue fixtures omitted native identity inputs, and four
process tests relied on fixed sleeps instead of observing harvest and return
ordering. Controlled delayed observations reproduced the latter failures.
Their repairs use real child identity and parent-exit barriers, retain actual
process teardown and fail when cleanup guards are removed. No production
lifecycle guarantee or native capability is widened by these test repairs.

The next frozen `d0f9cbaf` regression returned 21,100 passes, 261 skips and six
failures, all exceeding unchanged process-cleanup timing limits under concurrent
native load. Profiling identified repeated host-wide macOS parent scans as the
bottleneck. macOS descendant observation now uses kernel-filtered direct-child
queries with the existing birth identities, PPID and generation checks. Each
snapshot bounds buffer growth and total candidate observations, retaining already
authenticated descendants when another branch becomes unreadable. This remains a
best-effort snapshot, not proof that every possible detached process was observed.
Linux and Windows paths retain their existing behavior. Independent ABI, identity,
partial-error and bounded-work reviews and guard ablations pass; the repaired
candidate subsequently passed the full regression, affected native route renewal
and load qualification below. Failed runs remain evidence for their original candidates.

On frozen `8a7b4a9e`, the complete suite passed 21,122 tests with 261 skips in
2,623.73 seconds, including both 48-story load cases. All 743 tracked files
remained unchanged. Earlier regression batches ran the scale cases separately.
The managed case delivered every story and parent tracking in 1,052.18 seconds,
handled 46 messages and closed all 279 endpoints. Peaks reached 24 execution
workers/active epics, eight verification slots, eight review slots and four
reasoning jobs within 32 shared sessions; stop acknowledgement took 0.748 seconds.
These remain scripted-service measurements, not native-provider throughput.
Pyright, Trunk, release checks, isolated wheel/sdist checks and independent scoped
reviews passed. All eight affected native route recipes and both writable
Claude/Codex continuation directions passed fresh renewal without retries or
budget changes. The current full-history qualification also passed as described
in ADR 0003. No hosted Linux/Windows qualification is inferred from these checks.

Installation and target-project delivery remain distinct obligations. The exact
merged installation must retain its final interpreter/dependency paths, earn any
changed native grants and pass provenance, process-identity and generic onboarding
checks. Install with runtime constraints exported from that commit's frozen lock
and the tested interpreter; a Git source pin alone permits dependency drift.
Retain the export with installation evidence, and qualify dependency updates as
separate candidates. Preserve old live/paused environments. The REM pilot requires maintained
source configuration, actual full-graph dispatch validation, current ownership,
selected acceptance and CI, native/independent reviews, merge, landing and tracking.
Ticket-local dependency lists alone are insufficient: source import must include
inherited epic prerequisites. Disabled/archived automations and finite run logs do
not establish complete historical worker drain; scoped coexistence preserves peers.

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

Initialize hold-channel directories from committed campaign authority before
processing controls or admitting children. Read and write through confined
directory handles. An absent unit record inside an intact namespace means no
operator hold; a missing or redirected initialized namespace remains held.
Releasing one unit must never recreate that namespace and lose another unit's
hold. Upgrade legacy channel state before processing its queued controls.

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

Keep the exact child-process handles in a process-local registry across coordinator
instances. Reserve each launch before process creation; independent launches do
not wait on another unit's process constructor. Observe and reap only the handle
created by this interpreter for the matching launch claim, before OS liveness
checks, so an exited unreaped child cannot appear permanently active. A forked or
restarted coordinator must use the OS identity path for inherited or adopted
workers; persisted PIDs never authorize reaping. Release terminal handles while
retaining a lightweight uncertain-launch reservation when no startup or receipt
exists. A process exit alone never substitutes for the existing receipt, workspace
and Git checks, and an unacknowledged launch never authorizes a duplicate spawn.

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
Match observation to the actual provider: TeamRouter may expose an already-enabled
Codex credit route independently of ordinary subscription quota, while native
Grok/Muse require their own current model and allowance observations. Preserve
exact-model restrictions and custom observer overrides. The provider-specific
admission contract is detailed in [ADR 0003](0003-bounded-reasoning-and-handoff.md).

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

Explicit product paths can narrow declared file scopes, never widen them. A
resource-only declaration grants no product-file access; exact accepted source
artifacts retain their separate bookkeeping permission. Use the same effective
file authority in graph validation, checkpoint sealing, execution/recovery grants
and terminal validation. Only a genuinely undeclared footprint retains exclusive
repository-wide admission and its corresponding file permission.

Count planning, development, repair and final review against campaign budgets
and session capacity. Preserve existing account/router policies; a configured
adapter name is not proof of the provider actually used. Capacity refusals and
quota waits are resumable resource conditions, not product failures. Unknown
usage stays unknown. Bound repair/review/rebase attempts and surface exhaustion;
do not restart counters by creating another worker or PR.

Reasoning jobs consume the same global session budget as unit workers. Persist
their claims before computing ordinary-worker proposals and recheck capacity
when preparing every never-sent launch. A last-slot race defers the action
without a claim, attempt or operator hold. Verification keeps its separate limit.
Reuse exact capability-validation results only within one synchronous admission
pass; every actual native launch revalidates its executable and evidence files.

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

Preserved ref aliases must not exhaust a fixed branch-count allowance. Retain all
observed ref identities, share evidence only for identical immutable heads, and
scope patch/history expansion to declared paths before applying detail bounds.
Inspect every best merge base in criss-cross histories and both endpoints of
selected renames. Empty or repository-wide planning scopes retain full inspection;
genuinely oversized relevant evidence remains explicitly incomplete. GitHub may
paginate through numeric repository URLs: bind that ID to the selected repository
metadata and preserve the original endpoint and query scope on every page.

Repository metadata and selected detail have separate budgets. Preserve complete
issue and PR identities, bodies and public object shapes within the frozen
`github.inventory_metadata_bytes` allowance (64 MiB by default), counting the
combined issue/PR inventory and linked additions. Finite selected-path discovery
also counts complete patchless PR file metadata after detail enrichment here,
while selected patches, review bodies, threads and checks consume separate
per-PR and aggregate detail bounds. The frozen `github.inventory_pr_detail_bytes`
allowance defaults to 16 MiB for aggregate PR detail; it does not increase the
individual PR or branch bounds, or any coding-session budget. Older snapshots
without the field load this default. Broad or explicitly listed-PR discovery keeps
its complete expanded file facts in the detail budget.
Record observed bytes and the allowance. This is a finite observation budget, not a model context allowance or
a bound on process memory. Exceeding it remains an explicit incomplete observation;
do not silently clip bodies or report omitted rows as complete coverage.

For finite selected paths, classify open and recent PRs by exact changed paths,
including both sides of renames and conservative merge-base coverage. Retain
global metadata while expanding links and review/check details for selected
issues, mapped PRs, path-related PRs and links connecting that work. A title alone
cannot exclude a candidate. Broad planning retains broad inspection. The receipt
states which unrelated link closures were not expanded, so scoped completeness
does not imply an exhaustive recursive audit of every historical conversation.
Bare numbers such as `#1` inside fenced output are not inferred issue links;
explicit same-repository URLs and inline-backticked references remain discoverable.
Preserve the complete original body regardless of link classification.
Retain every detailed PR file identity, status and rename source, but expand patch
text only for the declared paths (including either endpoint of a rename). Record
the selected paths, omitted patch paths and bytes, and patches unavailable from
GitHub. Keep review bodies, threads and checks complete. This scoped inventory
supports admission and overlap reasoning; it is not a claim that the whole PR
received a code review. Required relevant evidence still obeys the detail bounds.

Distinguish a PR's path/link identity from its detailed observation interval.
Before collecting details, an updated timestamp alone can retain the already
classified paths and link closure when the native identity, head/base, body and
lifecycle facts still match. Use the fresh PR metadata and collect fresh review
and check evidence, accounting for its actual metadata size. Changes during that
detail interval, including timestamp changes, still invalidate the observation.
Structural movement remains incomplete and identifies the PR and changed fields;
never silently narrow scope or substitute stale details.
Charge fresh metadata enrichment to the aggregate metadata budget for both scoped
and broad discovery before retaining it; a separate detail budget cannot cover
unaccounted native metadata.

An inferred body mention such as `audit #22626` is not a native issue identity.
Record optional unresolved references with their source and target; a 404 means
not found or inaccessible, not proof of absence or authorization to discard an
obligation. Deduplicate these observations and count failed lookups toward the
request budget. Required selected/mapped objects, native hierarchy links and
authentication, transport or other observation failures retain their holds.
Bound commit-ID retrieval with an overflow sentinel and measure immutable object
sizes before loading formatted history. A limit on retained JSON alone does not
bound Git's captured output; oversized histories or individual commits must remain
explicitly incomplete.

Native campaign discovery reads an ownership projection in one SQLite transaction,
independently of growing cached inventories, job inputs and event history. Keep
unit/workspace bindings and every claimed reasoning job whose quiescence is not
established, including terminal jobs with uncertain owners. Bound the projection
itself; a corrupt, foreign or oversized ownership record remains unavailable.
Legacy JSON remains bounded. Exclude this campaign's own unit and reasoning claims
from external ownership while retaining other campaigns' claims. Expose discovery
waits and their causes through the ordinary status contract.

The internal intake planning unit's `resource:campaign-plan` scheduling claim
does not widen filesystem discovery. Retain the actual product units' scopes,
global GitHub intent and file identities, and selected/mapped/link closure. Real
planning with unknown product scope, an intake unit with explicit file paths,
or a campaign containing only coordination units still requires broader discovery.

An installed coordinator upgrade cannot silently reinterpret frozen native route
proofs against new worker modules. Permit explicit route requalification only for
a stopped campaign before its first unit worker, PR publication or earned acceptance.
Under the repository coordinator lock, compare the expected revision, reject
unsettled effects/actors, validate freshly earned equivalent route contracts and
persist a new configuration revision/template without reloading policy or profiles.
Retain old proofs and invalidate never-launched optional advice. Resume remains a
separate action. This limited startup recovery does not claim general migration
of campaigns already in delivery.
Deterministic workspace preparation alone does not count as a launched worker.
After the upgraded coordinator reconciles all allocation effects while stopped,
renewal may retain a draft unit with an exact confirmed preparation receipt,
unchanged clean bootstrap commit and matching workspace/Git identity. Preserve
its source bindings, never-started intake control and confirmed reflection of the
same draft workspace before any unit worker. Refuse unknown allocations,
PR publication, writing attempts, unrelated bindings and earned stage
evidence; renewing proofs never adopts arbitrary work or rewrites its branch.
Confirmed bootstrap hierarchy observations, milestone/issue creations and exact
native subissue attachments can also survive this renewal when their exact
intents, receipts and native parent/child bindings agree.
Preserve the creations and unfinished hierarchy work without replaying them.
Concurrent units may propose one identical shared create. Retain every proposing
unit, deduplicate by the exact tracking key and intent, and require the one
confirmed GitHub action to belong to that proposing set.
Unknown or rejected writes, unrelated GitHub mutations and PR history remain
outside this pre-first-worker recovery contract. Ordinary hierarchy reconciliation
still checks current remote state before admitting work after resume.
Optional portfolio reasoning attempts may be retained only when every historical
attempt is proven ended under its exact read-only or no-tools request, or has a
confirmed preparation failure without any launch intent. No applied semantic
decision, required job or unit work is admitted through this exception. Preserve
failed results, attempt counters, usage, claims and receipts unchanged. Never-sent
inventory/communication observations and exact quiescent reasoning-stale proposals
keep their original IDs and remain pending for normal reconciliation; uncertain
native effects and external writes still refuse renewal.
Semantic renewal compares the declared purposes and contracts in hash-bound proof
metadata, then validates the complete replacement proof for every purpose. Proof
coordinates may change; lost corpus or review evidence must not silently narrow
authority. Superseded contract alternatives remain historical evidence.

Planning writes an overlap disposition for each proposed unit: already delivered,
covered by active work, continue existing work, reuse partial work, new work, or
unresolved overlap. Cite exact source/PR/commit identities and acceptance coverage,
including remaining gaps and intentional exclusions. Same-file changes are a
conflict signal, not automatic duplication. Deterministic delivery reconciliation
must confirm an already-delivered claim; closed-unmerged PRs and patch similarity
alone cannot satisfy dependencies. Superseding abandoned work requires a recorded
reason so later planning does not repeatedly revive rejected approaches.

Bind selected native input authority separately from its private publication
copy. Clean tracked requirements follow the fetched main candidate and artifact
review; ignored or shared requirements retain their exact canonical preimage.
Check staged and working overlays of the selected tracked input without rejecting
unrelated entries in shared planning files. Apply this same binding when preparing
new work, adopting an existing PR and refreshing a reviewed amendment. A private
worker's lifecycle edits must neither replace canonical owner intent nor cause a
false canonical-drift hold.

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

Commits pushed by a bot or collaborator to an already-owned PR are new candidate
inputs, not permission to overwrite either history. Bind the observation to the
exact repository, PR/node identity, branch, authenticated remote checkpoint,
local/remote heads and graph/config/source revisions. Reconcile pending push
effects and wait for the affected writer to settle before scoped integration.
Preserve local, remote and target heads; validate newly imported paths, including both sides of
renames, before treating remote history as an allowed conflict-resolution source.
Use append-only integration and the configured Astra conflict-repair route. A
changed combined candidate owes fresh verification, native reviews when enabled,
independent publication review and server checks. Another remote change makes
that candidate evidence stale; unrelated units may continue.
Stage boundaries perform the same owned-head observation before allowing another
writing stage. A quiescent planning or admission stage resumes its unfinished work
after scoped integration and backup; integration alone does not finish that work.

Keep this debt distinct from an uncertain push. A proved no-submission remote
change can schedule reconciliation; a sent-unknown write must first reconcile
its original journal. Active rollback/recovery fences remain holds under their
original preimage authority; external changes do not authorize clearing them. Count distinct
remote heads durably against `github.max_remote_head_refreshes` (default eight),
deduplicate repeat observations and expose an operator hold on exhaustion.
Unapproved paths or protected source/config changes remain ownership failures,
not permission to widen a repair. Releasing a hold never restores stale reviews.

Keep external commit provenance separate from campaign session attribution.
Record exact imported commit/parent IDs, byte-preserved author and committer
headers, content digests and the observation's coordinates. Enumeration includes
remote history absent from both the first observation's local candidate and accepted target, with
explicit refusal above 4,096 commits or 8 MiB of cumulative raw commit objects.
Do not truncate a provenance ledger or infer models from Git names, signatures
or commit messages. External model attribution remains `unobserved_external`;
Git authorship does not prove human-only authorship either.
Preserve that first-observation checkpoint when a live writer incorporates the
remote commits itself; an eventual no-op integration still owes their provenance.

The family-independence gate retains every authenticated campaign contributor
family and any family established by trusted imported session evidence. Missing
campaign-writer telemetry remains blocking debt; integration, later known writers,
handoff and existing-source verification cannot clear it. A repair adds only its
own observed writer family. Full-diff review includes the imported code, while
independence from an unobserved external model remains explicitly unproven. This
does not create a generic `verify-existing` exemption: its built/done-source and
no-development-session proof requirements remain unchanged.

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

Before a selected story's first writer, compare its initial bound or exactly
mirrored issue version with its committed specification through a bounded triage
job in that story's workspace. Identity markers establish the match, not semantic
coverage. With qualified triage reasoning enabled, only a current represented
verdict with no missing requirements settles that version without an intake unit.
Bind the comparison to issue version, source, head, graph and frozen configuration.
Comments, later edits, ambiguous matches and additional requirements retain normal
triage. Missing qualification, exhausted attempts or a bounded never-launched
admission wait also fall back to normal triage. Live or uncertain jobs retain their
claims. A pending comparison is visible and does not reserve an execution slot
against unrelated stories; owner holds still prevent launch.

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
For imported ticket units without a tracker binding, recognize a unique standalone
`github-mirror:issue:<initiative>/<selector>` comment in the owning repository.
Re-read the native issue identity and its existing lineage before any hierarchy
create. Conflicting markers, closed issues, foreign identity or prior unresolved
publication remain visible reconciliation waits; title similarity and issue-list
order never establish identity. Intake timing cannot substitute for this check.
Capture a default initiative in frozen source observations without changing unit
IDs or namespaces. Older prepared units may use their matching frozen effective
ticket-source selector; conflicting or absent identities cannot authorize a new
hierarchy. A confirmed story-create receipt must match the discovered mirror and
still pass fresh lineage checks before binding the story. For split stories,
prove each existing parent against an explicit identity or its exact source
mirror; missing or mismatched ancestry never authorizes inserting new parents
above an existing human issue or epic.
All supplied hierarchy, unit and source tracker identities must agree with that
parent; one matching binding cannot mask another conflicting binding.
Before creating shared parents, reconcile imported members of the same initiative
namespace, including other epics, so story ordering cannot create duplicate
parents. Collect milestone facts from the native story and its proven ancestors;
an absent milestone contributes no identity, while conflicting facts require
reconciliation. Prior publication receipts remain authoritative debt to reconcile.
Apply the existing reviewed-extension requirement to closed native parent scope
before creating a new epic or story beneath it.
Validate the terminal epic's owning repository as well. New mirror adoption
reports unsupported cross-repository ancestry instead of treating a foreign
issue number as local.
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

Posting a successful G check can release the merge queue, so its send path
rechecks the current candidate, group, rules, local acceptance and remote
holds/reviews. A proven-unsent veto clears the submitted flag and requests
dequeue; removing the blocking condition permits fresh admission and proof.
Operator-suppressed check or dequeue intents cannot leave a submitted flag that
prevents the next authorized attempt. Uncertain submissions remain reconciled
against the remote identity before any replacement write.

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

Remove temporary allocation tags only after the workspace UUID and Git identity
are durable. Preserve remaining operator tags; use the host's explicit
`--clear-tags` command when no tags remain, rather than an empty `--tag` value.
Read back the resulting set after success or an uncertain acknowledgement, and
recover through the pinned workspace identity without creating a replacement.

One actual worker PTY is attached once. Persist workspace/terminal/pane/tab and
native provider session mappings independently; hook read-back, not a terminal
title, proves native agent attribution. Pending/unknown attribution is visible.
Managed workspace, task and terminal titles put observed hcom names, epic, issue
and PR first, followed by description, phase or job identity. Unknown bindings
remain explicit; a provider or model name is never an hcom identity. Bound each
navigation field before shortening descriptive text. Group labels aggregate their
actual members and retain a stable identity suffix; group identity itself remains
independent of labels. Refresh labels after actor reassignment and GitHub discovery,
including quiescent sibling workspaces. Use a separate display-title method so
renaming cannot break window identity, and persist title-write intent before
native mutation so lost acknowledgements cannot become operator suffixes.
Before hierarchy reconciliation finishes, use an exact confirmed campaign story
issue binding for navigation when the unit has no local issue reference. This
display fallback does not mark hierarchy reconciliation complete or authorize
work. Newly confirmed shared bindings also invalidate the reflection projection.
Keep genuine operator terminal text after the navigation prefix. Normalize
managed title text consistently with the native host's removal of
replacement characters and Braille spinner glyphs before verifying readback.
Preserve ordinary Unicode text, focus
and user-customized layout; manually named tabs can still override their terminal's
automatic tab label. Reflection tasks remain optional, preserve operator descriptions
and labels, and update their native linked-PR field when its binding is available.

Headless jobs freeze the terminal's registered worktree separately from external
job files and native tool permissions. Unit jobs use their own workspace;
portfolio reasoning waits visibly for an allocated campaign workspace. After
account isolation, restore only the terminal context whose host row, launch record
and process ancestry match this worker. Native harness hooks then register the
actual session; no model output or display title manufactures that identity.
Headless titles show their own hcom binding as pending until observed; they never
borrow a parent coding worker's name. Their ephemeral display links can refresh
without changing frozen job input, workspace binding or permissions.
Managed tags use the host's lowercase representation while retaining full group
identity in the campaign record. Fit the complete managed tag, including its
ownership suffix, within 64 UTF-16 units after normalization. Reserve all four
navigation fields before shortening their values; compact tags use `?` for an
unknown binding and an ellipsis for a shortened value. Full workspace, task and
terminal titles retain the longer navigation labels. Preserve other tags and
report an occupied 32-tag limit without dropping operator metadata.

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
Headless completion additionally disposes only its exact owned native terminal
after worker and descendant quiescence, matching receipt and containment evidence.
Require daemon absence after disposal before acknowledging completion. Missing or
replaced identities, uncertain acknowledgements and lingering terminals remain
observable and recoverable; never close the workspace or unrelated panes. Native
transcripts, output and completed-session identities survive terminal disposal.

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

The accepted `communication.mode = "managed"` extension provides public
controller-owned bridge addresses without enabling native automatic inbox
delivery. Each launch generation has its own address, registered before the
native session starts. Claude/Codex wrappers retain TeamRouter and mandatory hcom
enrollment in private per-job rooms. The public room exposes only bridge actors;
their replies identify factual controller status, never a purported model reply.
An outside agent can request status or submit a versioned, exactly addressed
steering envelope. BMAD persists advisory mail before acknowledging it, freezes
it at the next eligible stage boundary and records application only after owned
stage completion. Unit mail targets coding stages; explicit job mail targets that
reasoning job. A queued message cannot grant scope, bypass a hold or reopen work.

Managed actor registration contention is retryable only at the evidenced boundary
before a worker request exists. The controller seals campaign, unit, launch,
generation, attempt, configuration revision and payload digest, reports a
pre-worker wait, and reuses the existing actor and launch reservation. Reconciliation
checks current ownership and controls before retrying. The proof is consumed before
request preparation can dispatch; request, startup, receipt or launch-intent evidence
preserves the existing unknown-launch containment. A failed proof write supplies no
retry authority. Scripted regressions hold the real registration lock, exercise
supervisor harvest/reconciliation, and cover lost replies, concurrent controls and
uncertain dispatch without provider calls.
Native reasoning jobs use the same typed wait without failing their attempt. Their
existing immutable request/launch-intent journal supplies dispatch containment; a
busy bridge never resets that journal or replaces the job claim.
Indexing a registration intent never replaces an existing endpoint identity. An
observer can encounter a newly indexed intent before its actor identity exists;
it reports incomplete registration and preserves that room's mail and cursor until
the identity is available. Existing actor records remain the recovery authority.

The endpoint ledger, bounded shared-room poller and durable message ledger survive
coordinator restart. They preserve uncertain replies, detect database replacement,
close only owned bridge registrations and return explicit closed-target receipts
for late mail. Unknown/live prior owners cannot be adopted. Completion and drain
wait for bridge closure. CLI `endpoints`, `messages` and `steer` expose address,
acceptance and application separately. Titles put the public hcom name, epic,
issue and PR first; private native enrollment is a separate identity.

## REM activation and delivery of this PR

Ship the entire framework in PR 18; each implementation stage is a commit/check,
not a separate capability PR. Do not restore exploratory stash files wholesale:
compare them against this contract and retain only compatible implementation.
Keep REM-specific logic out of portable orchestration; support trusted project
policy/evidence and merge-command adapters with exact-head receipts.

REM's 2026-11-07 primary-session decision retired its custom merge scripts and
authorizes ordinary GitHub publication with administrative fallback. Use the
repository-bound `direct-authorized` mode under that decision. Retain signed
commits, applicable verification, explicit owner holds, selected native reviews,
high-risk cross-family review and shared project verification locks. Do not
restore a retired executor or add approval by a separate coordinator.

The repository's non-product workflow emits its applicable required check names;
mixed changes can produce more than one result for a name. Every applicable
matching result must pass. Never collapse a failed duplicate behind a green one
or substitute a successful local command for missing hosted evidence.

Strict/queue contracts remain available for other repositories. Native authorized
publication deliberately has a different freshness guarantee: GitHub accepts an
exact-head condition but offers no atomic base comparison. Integrate and review
against current main before submission, reconcile ambiguous writes, then validate
the actual landed commit. Failed landing acceptance opens a bounded repair unit
depending on the confirmed merge rather than the original unit's delivery; the
original unit and its parents remain tracking-pending until repair succeeds.

Do not change GitHub rulesets. Fresh preflight must inspect the actual actor,
rules, holds and chosen merge mode before activation. Installation alone supplies
no publication authorization and proves no target-repository readiness.

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

Preserved REM worktrees can still call the external BMAD compatibility relay now
maintained in Paradev tooling. Preserve worker `BMAD_LOOP_RUN_DIR`,
`BMAD_LOOP_TASK_ID`, `engine.pid` and available process-birth identity so hooks
resolve their owning engine before the relay's configured/global fallback. The
relay forwards arguments and stdin; it does not start an engine. Current REM's
absence of registrations or an idle engine observation does not establish that
preserved worktree callers are retired. Relay removal requires explicit migration
or retirement of those callers, outside this campaign implementation milestone.

Review-ready is an intermediate boundary before those deployment actions:
it means committed and pushed code/docs, normal-path qualification and
two fresh independent reviews against the same candidate. One review covers
completion/authority/publication; the other covers runtime wiring, harness/handoff
and throughput. The accepted completion extension includes deployment and the
controlled target delivery after that boundary. Substantive later changes invalidate affected evidence. Report
disabled hosted Actions and unrun platform gates accurately; do not enable
repository settings or claim CI success implicitly.

The operator follow-up must distinguish route availability from messageability.
TeamRouter capacity observations do not establish execution through its wrapper;
optional hcom observation does not establish enrollment or a receive loop. Before
claiming every worker and reasoning job can receive outside-agent check-ins,
qualify each selected route's enrollment, exact job/session identity, delivery,
reply and closing/drain behavior. The managed bridge implements this contract
for headless workers and reasoning jobs; direct native messaging remains a
separate capability. The configured room and managed binding travel in immutable
headless launch requests without inheriting a parent actor's identity; unbound
identities are explicit. Include an actual delegated check-in in the
controlled target pilot alongside draft PR creation, main synchronization,
review/fix/merge, tracking reconciliation and interruption recovery.

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
