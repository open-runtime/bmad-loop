# ADR 0003: Bounded reasoning jobs and portable session handoff

- **Status:** Delivery completion in progress; writable continuation, final reviews and activation pending
- **Date:** 2026-11-06
- **Extends:** [ADR 0002](0002-native-sdlc-campaign.md) and its [implementation contract](0002-campaign-implementation.md)
- **Delivery:** The same campaign implementation PR, before autonomous project activation

## Problem and decision

Deterministic state management does not remove the need for situational reasoning.
Routing, effort selection, semantic triage, priority changes, graph review and
reporting need more context than fixed scheduling rules can interpret. Long-lived
work must also survive quota exhaustion or a change of coding harness without
losing requirements, unfinished acceptance, local edits or conversation history.

Add bounded reasoning jobs executed through disposable coding-CLI sessions.
Python owns job admission, state transitions, validation, resource accounting and
external mutations. Models propose decisions and produce evidence-linked artifacts.
Valid proposals within existing authority apply automatically; this does not add
an owner-approval step to every decision. There are no direct model calls in the
coordinator loop and no model acting as the authoritative scheduler.

Build a Python handoff pipeline around immutable source records, structured
extraction, hierarchical summaries and a destination-sized working packet. Native
session import is optional enrichment. A translated transcript or summary never
replaces the accepted contract, actual repository state or publication receipts.
This decision extends the original campaign work; it does not defer its unresolved
correctness, GitHub, Paradev or activation requirements.

Implement this extension in PR 18 alongside the original audit repairs. Follow
the [G0–G8 traceability matrix](0002-campaign-implementation.md#implementation-goals-and-traceability)
and keep implementation, normal-runtime coverage, native qualification and
activation separate. The immediate milestone is readiness for the next reviews;
merge, exact installation and qualified REM delivery remain subsequent gates.

The accepted completion extension in ADR 0002 includes these subsequent gates in
the current work. Its native route proofs, managed hcom communication, writable
cross-harness continuation and 32-shared-session qualification are required before
claiming unattended readiness. Earlier candidate results below remain historical;
every affected route and final candidate must earn fresh evidence.

Oversized semantic inventories use a durable bounded map/reduce job graph. Maps
retain source identities and cross-partition dependency/ownership facts. Reducers
must account for complete input coverage before the final purpose-specific result
can change authority. Intermediate summaries never mutate priorities or plans;
stale, incomplete or oversized final decisions remain visible without partial
application. These jobs share the same capacity and safe-boundary message ledger
as other reasoning work, with distinct claims and public bridge identities.

Optimize sequencing and useful parallelism through dependencies, ownership and
token/session capacity. Preserve existing source initiatives/sprints and their
GitHub mapping; introduce no calendar, dates, velocity or sprint-capacity model.

## Reasoning responsibilities

| Purpose                           | Model proposal                                                                               | Deterministic application boundary                                                                                       |
| --------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Model and effort routing          | Rank qualified candidates for task difficulty, context size, urgency and observed outcomes   | Enforce current eligibility, role requirements, supported effort, budgets and review independence                        |
| Batch triage                      | Classify versioned issues and feedback; identify duplicates, dependencies and remaining work | Validate identities, scope, input coverage and existing bindings; preserve required findings                             |
| Semantic synchronization          | Explain conflicting intent or suggest a BMAD plan amendment from GitHub changes              | Keep GitHub facts and canonical source writers authoritative; apply reviewed revisions, not model-written tracking state |
| Prioritization                    | Propose order and rationale using owner goals, blockers, critical paths and current progress | Enforce scope, fair scheduling, dependency rules and safe boundaries for active work                                     |
| Execution graph review            | Suggest story splits, dependency changes, ownership scopes and independent work              | Validate graph revision, cycles, active claims, source authority and delivered dependencies                              |
| Reporting                         | Summarize progress, evidence, waits, decisions and delivery status                           | Supply canonical facts; validate references and explicit completion claims against those facts                           |
| Decision and technical resolution | Apply existing delegation or propose bounded repair                                          | Preserve typed holds, actual approvals, observed evidence and lifetime recovery budgets                                  |
| Handoff extraction and reduction  | Recover goals, decisions, chronology, nuances and remaining work from cited records          | Preserve exact inputs, coverage, provenance, contradictions and unresolved acceptance separately                         |

Run these jobs on relevant changes or explicit requests, with debounce and bounded
batches. Do not invoke a model on every scheduler tick. Unchanged evidence reuses
a current receipt. A new owner answer invalidates the applicable decision input.
Graph changes require the existing planning/review/delivery path; simple ordering
within approved scope can apply at the next scheduling boundary.

Routing has a deterministic bootstrap: choose from the configured qualified pool
using fresh availability and role constraints. An optional routing job can refine
future selections; it cannot recursively launch another router to select itself.
If no routing recommendation is available, use that policy fallback or report a
capacity wait. Do not fabricate availability or downgrade an explicit model rule.

### Approved route selection

The rollout planning, triage and decision pools retain Astra/Fable availability;
conflict repair remains Astra-only. These are independent of the summarization
pool and of this chat's selected model. Extend approved route pools to development
and recovery while preserving existing legacy dev/review settings by default.

Summarization prefers qualified Grok Build or AGY CLI routes; use Flash for maps
and Argon for reductions when qualified. Fall back to Opus 5.5, then Codex
Terra/Sol through existing CLI accounts. Resolve actual model IDs, effort support
and context/output limits from the qualified route snapshot, never a guessed
alias or advertised API limit. On fallback, replan unfinished chunks for the
destination's payload budget while retaining valid completed results.

The native capability manifest supports explicit `by_effort` variants under each
profile/model key. Medium map jobs and xhigh reducer jobs on the same model retain
separate native proofs and permission modes. Preflight, semantic planning, child
workers and handoff selection resolve the exact candidate effort; absence cannot
fall back to a differently qualified level. Legacy single-capability and
reasoning/execution mappings remain readable.

Preserve TeamRouter/account policy and use supported deployment routes. A consumer
CLI whose integration terms or protocol do not support the application is not
qualified merely because its binary exists. Use an eligible configured fallback
or expose a capacity/capability wait; this work does not authorize new paid API
accounts, subscriptions or undocumented authentication changes.

## Job identity, concurrency and persistence

The existing unit claim and pending-action deduplication by unit/kind cannot
represent many parallel read-only chunks for one story. Introduce job identities
and job-level claims under the owning campaign. A job can bind a unit, a batch of
units or a campaign snapshot; it is not a synthetic story, worktree or PR.

Each immutable request contains:

- schema, job and purpose IDs; parent/dependency job IDs and attempt generation;
- campaign/unit bindings, head/base, graph/config revisions and inventory digest;
- input manifest with exact source versions, hashes, event ranges and authority;
- prompt/template and output-schema versions;
- approved profile/model/effort candidates and the selected launch identity;
- tool grants, input/output/total-token bounds, agentic-turn cap, deadline and budget;
- permitted result type and invalidation conditions.

The receipt contains actual native identity and observed model/effort, lifecycle
evidence, usage, context-delivery observations, output hash and validation result.
Distinguish requested/served primary models from auxiliary calls and contributor
roles. A legitimate auxiliary model does not invalidate the primary-role receipt;
all actual writers remain part of review-independence attribution. Normalize
families for all supported harnesses instead of relying only on GPT/Claude names.
Keep proposed, running, awaiting reconciliation, validated, applied, stale,
failed and canceled states distinct. Persist intent before launch; reconcile a
lost acknowledgement before starting another attempt. Queue deduplication includes
the job ID, preserving existing unit mutation deduplication semantics.

Record terminal execution outcome separately from validation, application and
staleness. Emit typed quota, authentication, transport, cancellation and limit
outcomes even when no result JSON exists. A completed process with invalid output
is different from an uncertain live process; neither becomes a generic successful
empty result. These receipts feed the same bounded recovery and capacity paths.

Allow parallel snapshot readers without granting them the unit's write lease.
All reasoning jobs count toward campaign and provider session/token budgets.
Separate reasoning concurrency from development, publication review and verification
capacity, and reserve service for controls and publication. Launch and observation
are short effects; no model session occupies an I/O executor while waiting for
minutes. Store large inputs/results outside the hot snapshot by content hash.
Persist event history incrementally rather than expanding whole-history rewrites.

Parallel Git observations use action or launch-specific destination refs. Explicit
fetches disable configured ref mapping and `FETCH_HEAD` writes, so linked worktrees
cannot race on a campaign-wide target or `origin/main`. Each integration retains
the exact target it observed even when another worker fetches a newer main.

Extend stop/drain/terminate, observation, restart reconciliation and campaign
completion to job claims as well as unit claims. Draining closes new admissions
and accounts for already admitted dependencies; termination reconciles each owned
job through its transport. Required jobs gate their parent operation. Optional
background reports may be explicitly canceled and journaled, but a campaign cannot
report stopped or fully drained while a job remains live or ownership is unknown.
Parent cancellation cannot leave untracked orphan workers.

Stop cancels a durably prepared reasoning launch only when no native launch intent
exists. Sent or uncertain launches remain under observation. Operator holds block
new unit-scoped reasoning at admission and again at the last native launch
boundary, using current persisted authority and the scoped hold channel. A deferred
launch consumes no failure budget and can resume after release; existing native
receipts and monotonic attempt identities remain intact.

Serialize the final control read and a fresh native launch intent with control
acknowledgement using the existing campaign state lock. Capability validation,
launch-lock waits and multiplexer's empty-session preparation precede that short
critical section; terminal RPC follows it outside the lock. If control commits
first, refuse the new intent. If intent commits first, account for the admitted
work through its existing lifecycle. Direct headless child stages use the same
scoped authority boundary. Ordinary drain still permits an already admitted
bounded child stage; an operator hold or hard stop suppresses its next session.
Never turn an uncertain existing intent into permission to launch another worker.
Optional history warmup has a separate launch condition: recheck that its writer
is still eligible before a new native intent. A prefix can remain valid for
completion or reuse after its writer exits without authorizing a fresh optional
session. Keep observation of admitted work separate from new launch eligibility.

Use a dedicated reasoning-worker mode with immutable input manifests, not a
development stage that assumes its whole working tree stays unchanged. Concurrent
snapshot readers must tolerate legitimate unit edits elsewhere without accepting
changed input as their original context. Results apply only against matching
source/graph/configuration revisions and the required head/base coordinates.
Semantic jobs also bind their purpose-specific canonical context digest. Retire
obsolete nonlive jobs before retry delays or exhausted-attempt checks, preserving
their history as stale records. Current exhausted work remains observable; this
does not waive required handoff work or invalidate immutable map inputs merely
because unrelated campaign activity progressed.

## Storage and compatibility

Put one local SQLite database per campaign behind CampaignStore. Preserve the
repository coordinator lock, revision/generation comparison, immutable identity
validation and atomic intent-before-effect. Move append-only events and new
reasoning jobs out of the hot snapshot. Retain current outbox semantics and
introduce indexed historical access before pruning anything needed for adoption,
reconciliation or checkpoint proofs. Large artifacts remain content-addressed files.

Storage version 3 archives repeated terminal effect payloads transactionally.
Keep prepared/uncertain effects, current claim references and the latest terminal
record per unit/kind hot. Indexed historical reads preserve creation order,
exact native IDs and confirmation evidence, with visibility bounded by the
reader's saved revision. Own-comment filtering, metadata preimages, hierarchy
receipts, checkpoint provenance and deterministic action deduplication must use
these reads. Export and paginated action history retain all archived receipts.
Validate incoming IDs across both action and polling-observation archives before
either splitter removes rows; an archived write cannot reappear as an observation.
SQLite version 2 remains readable; its next save upgrades the database before
publishing the version 3 marker. Both the old-marker/new-database crash window
and old-reader refusal are tested. A newer marker over an older database is an
invalid authority binding.

Use short transactions and per-thread or short-lived connections; perform no Git,
network or model operation inside a transaction. Preserve permissions and symlink
protections. Use SQLite's consistent backup mechanism, not a bare database-file
copy while a WAL may contain authoritative writes.

Separate state-format versioning from configuration and machine JSON schemas.
Provide campaign migrate-state under the coordinator lock only when no live or
uncertain claims exist. Validate the new database before switching authority:
a pre-switch crash leaves v1 authoritative, a post-switch crash leaves SQLite
authoritative. Preserve the original under a backup name and replace its old
location with a versioned marker that old binaries reject. Never leave a readable
stale snapshot that an old binary could resume.

New read-only commands understand both formats. Update automation migration
inventory's direct snapshot readers so migrated active campaigns remain visible.
Migration is idempotent, with no live conversion or automatic downgrade. Commands
jobs/history paginate receipts and references; handoff queues an idempotent request
for the coordinator rather than spawning from the CLI. Existing status exposes
job/handoff summaries, effective resource limits, coverage and exact next actions.

## Headless transport contract

The conceptual interface is prompt + explicit context + bounded execution + a
schema-conforming result. An output-format flag alone may wrap free text in JSON;
require the provider's structured-result field when supported and validate it
locally. Validate field types, object identities, source citations, purpose-specific
semantics and current revisions, not only JSON syntax.

Reuse the adapter/profile seam and maintained TeamRouter launch path. Record
supported stdin/file attachment mechanisms, schemas, tools, native IDs, effort
levels, stop events, turn bounds, timeouts, token controls and actual context
limits per qualified harness version. Unsupported controls fail preflight or use
a documented equivalent; never silently ignore them. Hcom remains optional for
isolated read-only jobs, subject to the configured wrapper's registration contract.

Keep genuine Stop or managed-window death as the session completion boundary.
Final JSON, stdout EOF or a model's completion claim cannot create another path.
A headless CLI can run inside the existing managed worker/window. A new direct
stdio transport needs explicit lifecycle qualification; do not fabricate Stop
events from a successful-looking payload. Completion still precedes independent
result validation and does not imply unit delivery.

Submit large context explicitly using the qualified native input protocol. A
prompt naming a large file does not prove that the CLI read or delivered all of
it. Record input hashes, token accounting and truncation/compaction observations;
split or reject oversized chunks before launch. Never place large histories in
shell argv. Stream parsing is bounded and retains diagnostics separately from
the structured result. Missing output, limits reached or incomplete streams fail
the job; bounded repair may retry with smaller chunks.

Turns, time, output tokens, reasoning tokens and total spend are different limits.
Enforce a whole-job bound even if a CLI resets its turn limit on each queued input.
Do not enqueue unlimited follow-ups. Pure extraction gets no repository mutation
tools; validation failure cannot expand its permissions. No summarizer changes
source state, acceptance, GitHub or the active workspace.

## Canonical history and acceptance

Read supported Claude, Codex, Grok Build, Antigravity and Muse transcript formats
through separate versioned readers. Preserve immutable source bytes and normalize
events with source/session IDs, offsets, timestamps, actor/role, parent/tool IDs,
payload hashes and references to large attachments. Recover an append-only prefix
from an active log and record its boundary; an incomplete final record is explicit.
Final writer replacement seals the tail after proven quiescence.

Deduplicate repeated serialization of the same event, not distinct messages with
identical text. Record forks, imports and compaction ancestry without inventing
missing history. Cumulative billed input is not unique transcript size. Count
the actual normalized payload using the selected model's tokenizer or a qualified
conservative bound, including JSON, prompt, schema and harness overhead.

Maintain an exact source index for every owner message and a separate obligation
ledger: criterion ID, original source/quote, scope, required evidence, candidate
and environment, current proof state, and any authorized amendment or deferral.
Model-extracted obligations link to original sources; extracted summaries do not
become owner authority. Assistant claims, quoted instructions and tool text retain
their original provenance. Keep unresolved contradictions and explicit supersession
relations rather than choosing whichever statement sounds most recent.

Required rows distinguish passed, failed, not run, stale and authorized scope
change. A linked follow-up issue alone cannot clear a required row or close its
parent effort. Verification preparation is a reproducible part of the recipe.
Required reviews need actual receipts for the final candidate. This ledger remains
available independently of every lossy summary or native import.

## Hierarchical extraction and reduction

1. Snapshot, normalize and index the exact history and current work state.
2. Plan token-bounded chunks at event/tool-pair boundaries, with configurable
   overlap. Assign every event a primary chunk and distinguish contextual halo
   events so overlap does not create duplicate obligations.
3. Run parallel map jobs. Return structured goals, owner requests, decisions,
   progress, failed approaches, nuances, unresolved questions, files/PRs/worktrees,
   verification claims and next actions, all citing exact event IDs or ranges.
4. Validate citations and covered input ranges; merge repeated facts by identity.
   Preserve conflicting interpretations and source chronology. Never silently
   truncate required output to fit a cap; split work or report incomplete coverage.
5. Reduce by workstream and chronology. Each reducer receives relevant source
   references and ledger entries as well as child summaries. Retain a summary tree
   for targeted retrieval; do not discard intermediate artifacts after reduction.
   Packet indexes retain every validated summary node, its immutable input and
   semantic child identities. Retry dependencies remain separate provenance.
   Final summaries omitted while fitting initial or audited context stay retrievable;
   bounded omission indexes distinguish omitted finals from intermediate nodes.
6. Assemble a concise working brief, the exact active obligations and latest owner
   direction, current repository/PR state and a bounded detailed-history packet.
   Size it to the destination's effective context after instructions, tools,
   output/reasoning reserve and working space. A 160k-token packet is a configurable
   ceiling, not a universal target or half of every Codex context window.
7. Audit extraction coverage and critical obligations against exact sources.
   Structural checks prove references and inclusion, not semantic completeness.
   Preserve missing/ambiguous information visibly and offer source retrieval.

For T unique tokens, payload window W and overlap O, the window count is one for
T <= W; otherwise it is `1 + ceil((T - W) / (W - O))`, with `0 <= O < W`.
At T=20,000,000, W=1,000,000 and O=500,000, there are 39 jobs and 39,000,000
input tokens before overhead. Sixteen concurrent workers can process these in
waves; sixteen total windows cover only 8,500,000 unique tokens.

If each map emits 64,000 tokens, the first layer can total 2,496,000 tokens.
Windows of 500,000 with 250,000 overlap then require nine reducers; their
32,768-token output ceilings total 294,912. At 65,536 per map, ten reducers
permit 327,680. Reaching 160,000 needs smaller output targets, another reduction
or retrieval-based selection. These are illustrative maximums, not measured
throughput or promises that models produce useful text up to their limits.

Do not hardcode these windows or fanout. Derive payload space from qualified
route limits and enforce account-wide capacity. Output bounds can include thinking
and tool calls, leaving less space for useful summary text. Verify full context
delivery rather than inferring it from an advertised model window.

## Incremental handoff and worker replacement

Reasoning and replacement sessions share the campaign navigation-title contract:
their own observed hcom name, epic, issue and PR precede purpose, role, model and
descriptive text. Unbound portfolio work is explicit. A headless job never adopts
its parent worker's hcom identity; absent qualified enrollment remains pending.
Presentation can refresh GitHub links while frozen job inputs, permissions and
workspace identity remain unchanged. Campaign-created tabs inherit terminal
labels; manual tab titles and operator folder precedence remain visible limits.

Build summaries incrementally while work proceeds. Hash sealed source ranges,
prompt/schema versions, route identities and extraction settings for cache keys.
New input recomputes the tail and affected ancestors. New authoritative decisions
invalidate affected interpretations even when old raw chunks remain reusable.
An interrupted reduction resumes completed jobs instead of replaying the pipeline.

The runtime prepares optional extraction jobs from exact hook-bound native
transcript prefixes while the owned worker remains active. Observation starts
after a debounce interval, runs outside the coordinator tick, and seals only
immutable prefixes. Unchanged or small-growth histories reuse the private index;
map preparation is paged and admitted behind required reasoning work under the
same resource limits. The hot unit retains a bounded pending-job list and a
hash-bound index reference. Missing bindings, unavailable qualified routes and
coverage bounds appear in operator diagnostics without holding the coding unit.

This live precomputation applies to trusted native hook bindings, independently
of Paradev reflection. Active managed-headless streams do not yet have a qualified
live export; their receipt-bound history remains available after quiescence.
Native-store import is optional enrichment, and no native-store writer is claimed.
Final capture verifies each reused raw prefix and every input fragment against
current source records, including after transcript truncation or rewrite. It also
checks the original approved route and extraction contract, so provider capacity
loss does not discard valid completed maps. Invalid optional cache entries are
reported and re-extracted. Fresh owner-constraint audits and writer fencing remain
mandatory before continuation; summaries never settle acceptance or delivery.

Handoff correctness does not depend on the source provider answering one final
prompt. Provide a mechanical packet of current state, exact obligations, available
summaries and unsummarized tail when deeper reduction is unavailable. Expose that
coverage limitation; never label an incomplete digest complete.

Bound the fallback tail and initial obligation view to the destination budget;
retain omitted ranges and the complete source-addressable obligation index for
retrieval. When the successor cannot establish its current required constraints,
keep it in read-only recovery while retrieving or reducing the missing evidence.
Do not proceed with edits merely because an oversized fallback was truncated.

Keep routing within a live session, native same-harness resume and cross-harness
replacement distinct. First consume structured interruption causes, refresh
allowed target capacity and bound retry/cooldown. Confirm the old writer and its
owned subprocesses are quiescent. Reconcile uncertain pushes/API writes and
preserve committed, staged, unstaged and untracked work without resetting history.
Only then grant the replacement generation a write lease.

Quota waits do not consume semantic repair attempts or reset lifetime counters.
After Claude and Codex both write a candidate, neither family becomes independent
because the other was the latest writer. Select another qualified review family
or expose the review-capacity wait; never erase earlier contributions.

The successor retains campaign/unit/worktree/branch/PR and gets a new native
session identity with predecessor and handoff-digest links. Apply only current
configured permissions, not imported approvals. Preserve contribution lineage
when selecting independent reviewers. Attach hcom/Paradev to the new session and
retain old history; late predecessor events cannot act as current control input.
The successor re-observes work state before continuing the interrupted stage.
Transfer success never marks implementation, review or delivery complete.

The interrupted stage's frozen permission contract also survives replacement.
Read-only planning or publication review does not require a writer capability or
receive a writable packet. Legacy development reviews that are explicitly allowed
to repair retain that existing contract. The native review pair additionally
requires its own qualified two-review operation; generic review cannot replace it.
Initial fallback, route refresh and final successor admission use the same current
contributor-family exclusions for independent publication review. An explicit
author-family choice is refused with its reason. Replacement capacity or an
inadmissible saved route cannot prevent the owned predecessor from draining;
unknown claims and unrelated operator holds remain authoritative.

## Implementation sequence and qualification

Complete the earlier audit repairs alongside this addition: temporary-wait and
owner-answer liveness, actual-model attribution, allocation/fairness, capacity
freshness, GitHub reviewer/check producers, transcript evidence binding, Paradev
tasks/history/status, hcom observations and REM's maintained publication contract.
Integrate upstream security/reliability fixes before activation.

The work can be split into parallel implementation responsibilities after this
contract is reviewed:

1. Job model/claims, resource admission, asynchronous lifecycle and headless adapter
   capability tests; reuse campaign supervisor/worker seams without unit-claim abuse.
2. Transcript readers, immutable source/obligation indexes, chunk planning, map/reduce
   schemas, cache/retrieval and destination-packet assembly.
3. Execution route pools, typed quota receipts, fenced replacement, portable handoff
   adapters with optional native-import enrichment, attribution and hcom/Paradev lineage.
4. Integrate semantic routing, mass triage, graph/priority proposals and reports;
   retain existing graph publication and canonical-source write APIs.

Keep old campaign/legacy formats compatible through explicit versioned migration.
Register new policy defaults and environment variables through the existing core
contracts. No new exit codes; machine output remains schema-versioned JSON.

Zero-token fixtures must cover the normal CLI path, multiple chunks under one
unit, shared limits and fair progress of unrelated work, cancellation/restart,
lost launch/import acknowledgements, stale proposals, invalid JSON/citations,
missing ranges, source growth, compaction/forks, tool pairs across boundaries,
owner corrections, quoted prompt injection, output limits and oversized input.
Exercise quota during editing, push, review and merge reconciliation; assert one
writer and preserved work at every handoff boundary. Ablate refusal gates before
trusting their negative tests.

Qualify actual harness input/lifecycle behavior separately from fixture success.
For each advertised Claude, Codex, Grok Build, AGY and Muse capability, record the
qualified version and evidence for launch, explicit context delivery, native
identity, effort, usage, completion, stop and supported resume. Native-import
directions qualify separately; packet continuation does not promise a universal
native transcript converter. Unsupported interfaces fail visibly at preflight.

First evaluate a controlled corpus with designated critical requests, holds,
corrections and unfinished proof. Then run the full-history qualification on a
sealed snapshot of the owner-selected BMAD planning/implementation conversation,
including attributable child lineage. Snapshot at trial start without stopping,
moving or replacing the live source session. Record source boundary, byte/event
coverage, hashes and measured unique token size; do not substitute usage totals.
Source records and qualification artifacts remain outside tracked documentation.

The owner authorized completing that sealed full-history trial regardless of
size through existing CLI accounts, with no aggregate token ceiling. Enforce
per-job context/output/thinking/turn/time/retry/concurrency limits and report exact
observed usage, with unknown fields remaining unknown. Quota exhaustion preserves
completed chunks and permits an approved fallback or resumable capacity wait;
it does not silently truncate the corpus. No new paid API account is implied.

Report lost obligations, contradictions and critical-detail recall against exact
sources. Fake outputs do not prove semantic fidelity. Never add token-consuming
calls to the repository test suite. Enable automatic semantic handoff only after
qualification retains every designated critical obligation and owner hold,
preserves unresolved contradictions, and yields no invented approval or completion.
Passing this finite corpus does not promise lossless compression of arbitrary
histories. Measure input/output work, latency, failure recovery, delivered
throughput and working-context size before advertising scale. Original merge,
exact-install and selected REM-delivery gates still apply.

Parent tracking closes independent epics concurrently, with at most sixteen
pending parent scopes and the shared I/O bound. Each scope keeps its own durable
mutation intent and identity fence; initiatives wait for epic confirmations and
an aggregate pass still precedes campaign completion. Fresh graph, intake and
native-child checks apply before writes and retries. A temporary intake wait
retains its intent, while a changed graph invalidates the old scope authority.

Capability validation is memoized only within one synchronous admission pass,
using the complete capability, candidate, permission mode and validation mode.
Repeated routes share successful or failed observations in that pass. Preparation
and launch always revalidate executable and evidence bytes, and no cached result
survives the pass or substitutes filesystem timestamps for content hashes.

## Implemented semantic and operator boundaries

The campaign runtime now schedules `semantic-plan` effects from changed canonical
context and submits bounded jobs for routing, bulk triage, semantic synchronization,
priority, graph review and reporting. These jobs require enabled purposes and a
qualified frozen role route. Missing qualification or oversized context appears in
operator status; it never silently selects an unqualified model. Observation clocks
do not trigger duplicate analyses. Graph/configuration revisions and exact current
unit/source references are checked again before application.

Routing recommendations only reorder candidates that pass existing admission,
capacity, independence and budget gates. Priority changes only existing unit order,
preserves dependency/ownership/acceptance fields and advances the graph revision.
Bulk triage groups exact incoming versions into ordinary intake controls; their
existing triage stages validate disposition authority. Graph and synchronization
recommendations create ordinary planning controls with cited context. Requirements,
architecture, decomposition, independent review, PR delivery and canonical-source
reconciliation remain mandatory before a changed source graph activates. Report
claims reproduce exact unit state and commit coordinates; narrative is explicitly
context, not completion evidence.

The normal CLI binds bounded owner-evidence readers for explicitly selected native
Codex and Claude records. Other transcript readers do not yet grant owner authority.
Operator commands expose paginated jobs/history, queued handoff, role-specific
attachment, retained terminal history and nested diagnostics. Reflection invokes
native task projection; held tasks prefer an installed held/blocked status and
otherwise remain started while workspace tags and BMAD status expose their hold.
Hcom observations have bounded event/transcript cursors and operate independently
of Paradev reflection without peer wakeups.

Headless native transport has an immutable presentation workspace independent of
its job directory and permission grant. Preserve only verified new-terminal hook
context across environment isolation. The macOS Grok canary established full
input, native completion, exact host/CLI session identity, one native pane,
managed title, retained history and task projection. This evidence does not
qualify other harnesses, workspace-writing permissions or full-history fidelity.

Zero-token tests exercise production runtime effect dispatch, persistent job/result
application, ordinary intake control reservation and planning activation refusal,
plus operator task/history/communication paths. They establish wiring and deterministic
guards. Native route, sidebar/session and semantic-fidelity qualifications remain
separate gates; these fixture results do not establish autonomous REM readiness.

## Current storage, continuation and qualification boundaries

SQLite stores the durable job/outbox transaction and indexed event history.
Terminal reasoning jobs leave hot authority while remaining queryable. Coordinator
history also keeps repeated terminal effects outside hot authority, with indexed
snapshot-bound reads rather than discarding historical ownership evidence. Coordinator
interruption reloads committed authority before recording drain; an interrupted
save acknowledgement is never permission to replay an old revision. Observation
and termination have independently admitted executor slots, including when regular
I/O is saturated. Concurrent GitHub hierarchy observations apply field deltas with
preimages and identity/state fences rather than replacing a stale full projection.

Repeated semantic context receives a new job identity for each durable trigger;
replaying the same trigger preserves its request and creation time. A cleared
hold can therefore revisit earlier context without reactivating a completed job.
Native effects bind the exact job request and launch attempt. A late launch or
observation reply cannot overwrite a newer claim or a completed receipt. Replies
for archived terminal jobs settle the effect without modifying immutable history.

Execution pools select both development and ordinary child-review routes. A
cross-harness continuation preserves the same worktree, draft PR, partial product,
source obligations and all contributor families. Interrupted ordinary review uses
a fresh run with an exact predecessor permit, preserving the remaining review and
development counters without replaying completed development. Accepted-review and
stopped-native reservations remain fenced; they are not ordinary-review permits.

A native quota receipt invalidates older positive capacity observations for its
actual development or review route. Before replacing a writer, the coordinator
can choose another currently eligible, qualified member of the frozen role pool.
An explicit operator-selected route remains explicit. Rerouting reuses an audited
packet when its bytes fit the new destination; a smaller destination returns to
assembly and bounded reduction before any successor acquires the write claim.

History audit covers complete native-owner source spans, not just message IDs or
model-selected quotations. The controller partitions each message into contiguous
spans of at most 512 characters, binds offsets and UTF-8 hashes, and reconstructs
every exact quotation locally. Audit batches reserve input and mandatory output
coverage capacity. Each span occurs exactly once in its result; packet assembly
also checks coverage against the complete captured native-owner history. Compound
requests and requirements adopted from quoted plans therefore remain available
even when an interpretation summarizes only part of a message.

Both current and contextual spans remain in the successor's constraint view and
its independently audited compression tree. A status question need not create an
approval hold, but classifying it as context cannot erase its text. Historical
exclusion requires a separate authenticated, scope-specific supersession or
canonical completion proof for the affected spans. No current evidence producer
establishes that binding, so the shipped audit schema offers no historical
exclusion and requires an empty exclusion-evidence reference. Raw later messages,
assistant completion claims and model-selected quotations cannot manufacture this
proof. Semantic qualification includes compound/adopted requirements and omission
or unsupported-exclusion mutations. The audit prompt version is history-audit-v3.
The frozen continuation context binds this prompt version and schema digest.
Persisted captures or ready packets from an older contract cannot resume a writer;
the normal refresh path rebuilds their audit while retaining original artifacts.
Launch preparation and claim adoption independently enforce that freshness.

Current constraints that exceed the working packet budget use additional bounded
reduction jobs. Each reduction retains a source-linked tree and receives an
independent audit of its complete immediate input. Exact original quotations and
canonical acceptance stay in immutable indexes. Output bounds include source-ID
coverage overhead; every semantic failure retains its attempt accounting across
retries. The constraint-compression schemas require their own earned semantic
qualification binding. Completing the history-only corpus does not qualify this
additional contract automatically.

Missing native history first triggers a read-only evidence-recovery job over
canonical acceptance, explicitly adopted committed policy, configured documents
and provenance-checked mapped GitHub discussions. An independent audit must find
that authenticated current authority establishes the complete working contract.
Canonical acceptance or contextual discussion alone cannot replace unknown owner
instructions. Insufficient evidence preserves a precise hold after the attempt.
Successful recovery retains every missing-history marker, confers no approval or
scope waiver, and rechecks source revisions before releasing the successor.
Recovery has a separate schema-bound semantic qualification gate.

An evidence wait performs a read-only refresh every 30 seconds and restarts
reconstruction only when its bound sources change. New committed policy or owner
evidence therefore resumes the same handoff without an unrelated graph change.
Audited reconstructed constraints participate in the same bounded reduction tree
as native-history constraints; the exact evidence quotations remain indexed even
when their working packet representation is compressed. Additional contract
qualification requires the independent native receipt itself to identify the
reviewed schema digest, not merely a new digest in the qualification manifest.
Native contract schemas bind coverage and authority references to the exact
supplied IDs. Replanning a mixed quota/failure batch preserves semantic failure
counts by immutable source coverage; changing routes, job IDs or chunk geometry
cannot renew those attempts. Exhaustion produces an explicit continuation hold,
while quota waits themselves do not consume that budget.

Each failed attempt receives one persisted route cooldown, bound to its job,
launch, generation and attempt identity. A deferred replan stores that cooldown
and the lifetime failure ledger even when every route is unavailable. Polling or
coordinator restart cannot move its expiry; a later failed attempt receives its
own cooldown. Unknown native claims must drain before replanning, and replayed
observations cannot cancel a replacement from a newer plan epoch.

Campaign history reduction separates native output tokens from destination
context bytes. The qualified native allowance includes provider reasoning.
Fresh campaign reducers retain complete structurally validated artifacts using
the existing unallocated-artifact mode; the successor's initial context does not
limit stored provenance. Previously failed requests keep their immutable bounds
and failure debt. A single retained root needs no further reduction merely to
fit context. Hierarchy rounds retain their exact byte/node measurements. No
observed shrink or the eight-round bound produces explicit compaction-limited
evidence, followed by source audits and bounded retrieval selection; only a
complete applied frontier can take this path.

Packet assembly reserves exact serialized space for canonical obligations,
current state, independently audited owner constraints, uncertainty and indexes
before selecting historical facts. Complete canonical obligations must fit;
oversize owner constraints retain their separate audited reduction path.
Whole facts are selected by kind and recency, round-robin across final nodes,
without clipping text or granting authority. Every selected fact binds its
unchanged parent hash and fact identity. Partial views remain distinct from
complete summaries; hashed omission indexes retain every omitted fact and node.
If history contains facts but no useful whole fact fits, continuation requires
visible context recovery. Final exact packet bounds and launch-time provenance
and canonical-obligation checks remain mandatory. Versioned prompt templates and
schema definitions remain unchanged; fresh requests bind the selected artifact
mode and limits. Fresh native reduction and independent qualification still
precede use of a new route.

New durable packets also bind the complete mandatory context independently of
the optional fact view. Final consumption verifies every authority result,
immutable audit input and owner/obligation/constraint index used by assembly,
including compressed-constraint and recovered-authority evidence. Legacy ready
packets without this binding refresh through the ordinary drained handoff path;
the controller never manufactures a binding from their mutable contents. Live
successors continue under their existing claim and enter a fresh fenced handoff
only after normal observation establishes their interruption and quiescence.
Extraction and reduction prompts embed their exact job-bound native schema,
including eligible citations and quote IDs. The corrected `history-extract-v3`
and `history-reduce-v5` contracts require fresh extraction and reducer semantic
qualification. Earlier receipts remain historical evidence, not grants for the
new prompts; unchanged audit and finite-contract proofs retain their own validity.
This applies to queued jobs, applied summaries and already assembled packets.
Keep old requests and results immutable, observe any live or uncertain claim to
quiescence, then use the existing safe refresh path before releasing a successor.
Preserve lifetime retry debt and source artifacts; neither restart nor a contract
change grants a fresh failure budget.
The working brief retains current obligations and uncertainty while a cited index
points to historical technical details in the complete retained children. Known
historical limitations remain qualified facts; `gaps` identifies missing supplied
coverage or ambiguity that prevents faithful continuation. Neither index entries
nor gap classification may fabricate resolution or waive unfinished acceptance.

Semantic qualification uses schema-2 proofs for exact operation, effort and
earned transport-evidence hash. A capability may reference several independent
proofs for history extraction, reduction, audit and the separate constraint or
recovery operations. The native independent result must bind the complete
route-purpose list digest as well as the corpus and any additional contract
schemas. Finite constraint/recovery proofs bind their own used schemas; a change
to the unrelated history-audit schema does not relabel or invalidate those native
receipts. Editing a manifest cannot extend an existing receipt to another effort
or purpose. Older generic semantic proofs require requalification; verified
version-1 native transport canaries retain their separate supported read path.
The independent native result also carries `semantic_contract_digests`, keyed by
operation. Each digest binds the actual readers, summary schema and that
operation's prompt; history audit additionally binds its audit schema. Extraction
and reduction do not depend on the unused audit schema or each other's prompt.
Their schema binding covers the job-specialized template, including required
quote-ID fields, allowed properties and empty-catalog variants. Normalize dynamic
job IDs and source-catalog values for this reusable semantic grant; the immutable
native request digest independently binds those exact values for each execution.
The legacy unbound schema is not sufficient evidence for a specialized shape.
Editing only a manifest and its outer hash cannot requalify a native result for
changed code contracts. Older generic receipts without this attestation require
renewed independent qualification. Existing finite constraint/recovery receipts
retain their separately attested `contract_schema_digests` protocol and do not
inherit unused generic reader, summary or prompt requirements.

Generic history-auditor evidence also binds the audit prompt version in both the
proof and independent native result. Clear unfinished work, resolved historical
questions and rejected untrusted claims are not unresolved owner decisions;
missing current authority or conflicting current constraints still block continuation.
Automatic map/reduce admission applies these semantic gates before launching,
and completed result reuse retains its original source and route provenance.

Headless write-capable execution requires a qualified native route and an OS
confinement wrapper. The shipped macOS wrapper enforces frozen product/private-source,
owned Git branch and explicitly granted harness-runtime paths for descendants.
Read-only history cannot overlap writable grants. Other operating systems currently
refuse this execution wrapper; their existing legacy transports are unaffected.
No route becomes qualified merely because a profile or this wrapper exists.

`qualify-headless` records exact input-canary coverage, native outcome/identity,
usage and owned quiescence. `trial-handoff` operates on sealed explicit histories
outside the repository test suite. Transport qualification is distinct from the
independent semantic-fidelity receipt required for automatic summarized handoff.
Native account exhaustion is a retained failed probe or capacity wait, never a
reason to manufacture qualification or silently omit history. Earned full-history
fidelity and successor retrieval are bounded by the qualification below.

A delivered revised plan does not itself waive prior acceptance. The explicit
`authorize-scope` control binds the operator's reason to the exact ledger revision,
before/after scope and delivered planning revision; changed coordinates refuse it.
Reintroducing a waived criterion makes it required again. This operation never
clears another owner hold or supplies a GitHub review approval.

## Qualification achieved and remaining gates

The sealed whole-conversation trial processed 28 sources containing 191,223,107
captured bytes and 53,494 unique events through 809 maps and 69 reducers. An actual
independent Muse assessment found no missing critical owner obligations, invented
authority or lost unresolved conflicts; a separate Grok assessment qualified the
Muse history-audit interpretations. All 37 native owner records and their 178
complete source spans remain indexed. These receipts bind the exact source corpus,
route, effort and used reader/schema/prompt contracts. They establish critical-owner
fidelity for this corpus, not lossless compression of arbitrary tool history.

A native Muse successor retrieved omitted summaries through the retained tree,
verified exact source bytes and owner text, and respected the packet's read-only
boundary. The packet remains read-only because it lacks live canonical acceptance
and current repository reconciliation; opaque historical content remains visibly
opaque. This qualification does not establish writable continuation of that packet.
Scripted runtime coverage of quota replacement through delivery and finite native
writer canaries provide separate evidence. Failed qualification attempts remain
retained alongside successful results.

The normal supervisor path now demonstrates optional history extraction while a
real scripted predecessor remains live, followed by appended owner instructions,
quota interruption, reuse of completed prefix maps, a fresh full-history owner
audit and same-PR successor delivery. The successor starts only after predecessor
quiescence. Native handle observation supplies the exact hook binding with Paradev
reflection disabled; no completed jobs are manually seeded into this scenario.
Both Claude- and Codex-format predecessor histories exercise this zero-token path.

Focused regressions cover immutable-prefix and fragment validation, source rewrite,
corrupt optional artifacts, stale extraction contracts, unavailable bindings and
route changes. Exhausted maps advance optional pages without becoming semantic
coverage; mandatory final extraction still includes their failed source ranges.
Background jobs remain optional, resource-bound and lower priority than required
reasoning work. Only trusted hook-bound native prefixes qualify for live warmup;
active managed-headless streams retain their quiescent-export boundary. No
native-store import writer or universal live-history capability is claimed.

The frozen scripted load qualification on `3efacd77` delivered 48 stories across
24 active epics, reached eight verification slots, eight review slots and four
reasoning jobs within 32 shared sessions, and completed parent tracking after stop,
drain and resume. It applied 47 messages and closed 302 managed endpoints. Stop
acknowledgement was 0.554 seconds; the measured scenario took 1,265.90 seconds. This
demonstrates tested concurrency and responsiveness, not maximum throughput or a
REM/provider benchmark.

At that earlier milestone only exact Grok and Muse routes had applicable native
qualification. The delivery extension has since earned writer evidence for exact
Claude/Fable, Codex/Astra, Grok, AGY/Flash and Muse recipes, plus separate read-only
and single native-review recipes. These exercise roughly 128 KiB of input, actual
scoped edits/commits, protected paths, native identity, usage and lifecycle. Input
bytes are not tokens; the results do not qualify every model, effort, permission
mode or full-history continuation path. Changed transport dependencies require
fresh proof. The actual main/custom native review pair has since earned separate
proof: both native reviews precede a third schema-mapping turn, with retained
delegate artifacts and exact candidate/base identity. Writable continuation remains
a separate gate, followed by installation-bound revalidation.
The frozen code candidate `3fa00c5d` passed the integrated regression suite with
20,932 passes and 261 skips in 518.69 seconds; the dedicated scale case runs
separately. All 624 source/test hashes remained unchanged during that run.
Pyright, Trunk, package build, isolated wheel installation and release checks
passed. The launch-lock control race, embedded-versus-enforced history schema
mismatch and optional-warmup admission fixes passed scoped independent review.
Historical GitHub review findings subsequently reproduced missing ownership,
hold, source-freshness, attribution and observation protections. Their repairs
remain release work; the green suite alone does not establish readiness.

Five fresh extraction jobs earned current map-contract semantic evidence through
independent native audits. Current compact-reducer trials failed native output,
time or normalized-artifact bounds; no reducer grant or writable continuation is
inferred from them. Diagnose such non-convergence before spending another attempt.
A small measured writer-input allowance can make the destination packet much
smaller than its native advertised context. Qualifying a larger input requires a
new canary, immutable proof and actual delivery; an advertised window is not that
proof. Recompute new packet and artifact budgets only from earned capabilities,
then run new reduction and continuation jobs. Never reinterpret a failed receipt
under a larger later budget. Preserve the retained history and exact owner index.

A subsequent fresh stored-artifact reducer earned current `reduce-v5` semantic
qualification at Grok medium effort, independently audited by Muse. Its complete
retained result covers all five children and all 38 owner quote references, with
historical uncertainties explicit. The destination packet selects bounded,
source-addressable views from that artifact; its initial-context budget does not
discard the full summary tree. This new proof does not regrade the earlier failed
compact-artifact requests. The current full-corpus trial retains its completed
maps and immutable attempts across a drained source-repair checkpoint. Completion
of that trial, an independent full-source audit and writable continuation remain
separate requirements.

Those regression and load results qualify their named earlier candidates.
The delivery completion extension requires fresh final checks and
reviews after its changes. Hosted Actions remain disabled; unrun platform gates
remain explicit. Merge, exact-commit installation, canonical-base update and REM
activation are required completion work, not satisfied by earlier qualification.

## Capability evidence and limits

These references inform qualification; public API limits are not proof of the
effective installed CLI route or its automatic compaction boundary.

- [Claude headless output](https://code.claude.com/docs/en/headless) and
  [CLI bounds](https://code.claude.com/docs/en/cli-reference): structured output is
  separate from the JSON envelope; streamed queued inputs require a whole-job cap.
- [Antigravity headless mode](https://antigravity.google/docs/cli/headless/): stdin
  streaming, schemas and deadlines are documented; do not invent unsupported
  max-turn or output-token flags. Its documented effort choices are low/medium/high.
- [Gemini 3.8 Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash)
  and [Gemini 4 Argon](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-argon):
  reserve room according to the selected endpoint's context/output semantics.
- [Antigravity usage boundary](https://antigravity.google/docs/faq/#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching):
  direct scripting/CI is distinguished from consumer-plan third-party application
  wrapping. Qualify a supported deployment route instead of assuming the existing
  consumer login authorizes the intended application integration.

Native wrappers can perform registration or updates even for capability commands.
Use reviewed, version-pinned probe paths with those effects disabled through
supported controls; capability discovery must not start a coding session.

### Native qualification and full-history trial implementation

The operator qualification path uses a fixed canary and the same owned terminal
runner as normal jobs. Its narrowly scoped bootstrap cannot accept an arbitrary
prompt, source grammar, tool grant or changed result schema. Successful receipts
bound qualified input coverage to observed input and retain selected versus
served model identities. Output ceilings are configured acceptance limits; actual
canary output usage remains separate. External time and wire-byte containment and
native receipt rejection are explicit, without implying an unqualified hard
provider-generation limit. The normal launch path continues to require earned
qualification. Grok prompt-file recipes must preserve full input with its native
verbatim option; Muse recipes pin the native binary and suppress launcher updates.
Native protocol decoders preserve structured quota failures and provider usage.

Native admission reads the referenced qualification report and binds its completed
receipt to the fixed canary request, exact selected effort, executable/dependency
hashes, argv, transport and permissions. Editable checklist fields cannot expand
earned input, output or turn limits. Version 2 reports also hash the complete
request envelope; version 1 reports remain readable through their matching frozen
sibling request and owned receipt, without rewriting private evidence. Independently
validated semantic-fidelity evidence may be attached without changing this native
transport identity. A capability cannot inherit another effort's native proof.

Workspace-tools qualification is a separate explicit `campaign qualify-writer`
canary. It creates a new disposable repository/worktree and grants exactly one
owned file plus owned Git metadata. The native model must make the exact edit and
commit it; Python independently checks the commit, protected bytes/main ref,
identity, usage and process quiescence. An independent descendant under the same
frozen macOS sandbox attempts forbidden sibling/outside/ref writes and must be
denied. The launcher has an exact managed-interpreter shebang and pinned content,
with pinned sandbox/native dependencies; resolving the interpreter before launch
would discard virtualenv dependency resolution. No-tools canaries cannot earn
writer qualification. Earned input coverage, actual output/turn usage and configured
ceilings remain separate. This finite operation establishes an executable scoped
writer route, not unrestricted workflow or cross-harness completion qualification.

Read-only tools are a third explicit native permission, distinct from no-tools and
workspace-tools. The exact outer sandbox permits only scratch and frozen native
runtime paths, rejecting product/Git write scopes and overlapping runtime roots.
Its independent fixture requires a private workspace readback, unchanged product
and Git state, and actual descendant write denial. Normal reasoning may select
this route only from its frozen reasoning capability; it cannot borrow writer
evidence or silently widen no-tools authority.

The native Codex read-only backend keeps TeamRouter/hcom admission outside
Seatbelt: macOS refuses to execute its setuid process-inventory binary under even
an allow-all Seatbelt profile. It does not bypass or replace ownership checks.
The exact native read-only sandbox policy confines tools, and a deterministic
`command/exec` probe through that same policy must demonstrate product/Git/outside
and descendant write denial before inference. The native RPC response remains in
the immutable qualification receipt. This backend cannot authorize writers or
inherit an outer-sandbox proof.

Codex native app-server routes pin the owner wrapper and transport dependencies.
One ordinary `turn/start` binds native model/session/usage to the selected route.
A separate `review` capability uses the `codex-native-review` protocol: native
`review/start` followed by one schema-constrained mapping turn on the same thread.
Both turns count toward the bounded allowance. Preserve the raw native review and
all findings independently of the mapping result; unresolved native findings force
changes requested. Mapping supplies campaign-specific coordinates and acceptance
proof, and cannot waive the native verdict. Planning/publication/landing review
selects that explicit slot and fails closed if its proof is invalid. The native
review recipe requires its own earned qualification, never an edited ordinary
reasoning capability. No-tools is not claimed for either Codex transport.

Repositories that require both a native base comparison and a complementary
native review configure a separate `native_review_pair` stage. Its qualified
launcher includes `--review-pair`: two actual `review/start` calls compare the
candidate with its immutable accepted base and inspect the supplied complementary
requirements, followed by one schema-mapping turn. Preserve both native delegate
lineages, findings and usage. Verify the clean candidate and integrated base before
and after the pair. Its durable receipt and the separate independent publication
review must both be current; neither a mapping turn nor a contributing writer's
review supplies the independent-family requirement. The stage uses normal claims,
review capacity, stop/recovery and repair handling. Existing single-review recipes
remain available and cannot silently satisfy the configured pair.

The native app-server projects the structured review as rendered prose and does
not publish the delegate's token usage in its parent stream. Read immutable
native rollout snapshots using explicit parent-thread, root-turn, child-turn and
review-item identities. Preserve the native structured `review_output` and child
`token_count` records, plus the child's actual model/effort context. Add that
separate delegate usage to the mapping session's native usage. Missing or
ambiguous lineage fails qualification; requested model names and formatted review
text are not substitute evidence.

The Codex scoped-writer backend preserves native read-only builtins and exposes
one dynamic tool, `bmad_shell`. Its disposable client validates thread/turn/tool
identity before delegating to the exact frozen ownership sandbox and private
object bank. Durable call identities prevent repeated execution after duplicate
delivery or uncertain outcomes. The command budget is 32 calls, 60 seconds and
64 KiB output per call. Native builtin denial and scoped writer commit/history
proof are independent qualification checks. Quiescence covers the owned session
and observed descendants; it is not proof against a malicious daemon that detaches
and disappears between observations. Persistent background services are excluded.
The Claude scoped MCP route shares this bounded command boundary. Reusing an
unchanged nested native executable's served-model alias evidence never transfers
permission qualification to either writer route.

Ordinary Codex worker and stage results use a closed native transport schema
containing a serialized BMAD result object. The adapter validates and unwraps
that object before applying the existing stage contract or folding native review
findings. This preserves arbitrary required artifact fields without sending an
open schema that the native API rejects. Transport qualification and ordinary
worker composition must both exercise their actual schemas.

AGY's private no-tools hooks deny general tools and bound model invocations across
the complete job. Its native schema `finish` is an output operation, narrowly
allowed only with arguments matching the frozen schema. A model-generated tool
call cannot establish completion; the owned native terminal lifecycle and result
remain required. Preserve genuine native attribution and usage on malformed
structured output, without accepting concatenated JSON or tool parameters as the
missing terminal result.

Native transport framing has its own finite byte budget. Codex app-server input
echoes and token-delta events are not model output tokens; its wire allowance
accounts for the frozen input, model output and bounded scoped-tool results.
Model turn/token limits remain unchanged. AGY hook input
accounts for its complete frozen prompt and bounded arguments rather than a
small fixed read that can truncate native JSON.

The AGY scoped-writer recipe admits only `run_command` and schema-valid `finish`.
Its documented pre-tool `overwrite` replaces each `CommandLine` with the pinned
bounded command worker, binding native conversation/step identity to the original
argv. It denies all other direct file, messaging and MCP operations. The shared
scoped executor owns write authority and retained call outcomes; native invocation
limits and terminal completion remain independent checks. This route earns fresh
writer proof and inherits the foreground-command lifecycle limitation above.
Per-launch AGY permission settings allow only the exact pinned command frontend
prefix. They do not disable native permissions globally or expand its scoped
write grant.

Native Git writes use a private object bank for each launch. Canonical objects and
earlier banks remain read-only, and banks are never canonical Git alternates:
even a forged loose object can otherwise shadow a valid packed object. Trusted
checkpoint observation and terminal acceptance independently repack and verify the
exact new graph into canonical storage before inspecting it or publishing its SHA.
The copied objects use new inodes, so a writer's already-open descriptor cannot
modify an accepted checkpoint. A baseline available only in an unsealed bank, a
corrupt object, or an older global bank-alternate registration produces a visible
hold. Banks remain retained until a separate verified reachability cleanup; this
promotion path does not collect them.

Each writer also has a private index and owned branch ref, with a read-only
snapshot of other canonical refs for comparisons. Publishing a checkpoint first
seals its objects, then compares and swaps the canonical owned ref. The real Git
index lock protects staging installation. Recovery compares the recorded prior
and desired index content, preserves intervening owner staging, and reclaims only
an exact controller lock whose process death is confirmed. Completed publication
retries do not overwrite later staging. A lock or staging identity that cannot be
reconciled remains a visible hold.

Native launch requests, grants, lifecycle records and result receipts live outside
the writable product workspace, including when the legacy child engine keeps its
journal inside that workspace. Preparation, launch and worker startup enforce the
separation; explicit runtime write grants cannot expose those records or shared
Git storage. Broad product ownership still excludes the worktree's Git pointer
and controller run/campaign state. Private source views remain writable under
their exact grants. Protecting files also prevents moving their containing
directories to evade the boundary.
The CLI retains failed startup attempts and resumes only the same immutable job.

The separate `campaign trial-handoff` command reads explicitly sealed prefixes,
checks their immutable source hashes, deduplicates only verified inherited native
records, plans bounded maps and reducers, and persists each native receipt and
source-cited result. Jobs use the production validators. A failed job stops new
admissions while admitted jobs drain; rerunning the same frozen trial reuses its
validated results. Its structural-completion report cannot grant semantic
qualification. A separate independent-provider review must establish critical
constraint fidelity and absence of invented authority before automatic handoff.
The qualified whole-thread trial above satisfies that finite corpus gate for its
exact routes and contracts; a fixed transport canary or scripted regression alone
does not satisfy it or qualify a different route.

Rendered-input planning splits an oversized map or reducer independently rather
than shrinking every job's payload. Map splits retain exact native ranges; reducer
splits retain complete validated children, with citation enums drawn from their
actual retained facts. Owner quote selections identify verified source spans;
the controller extracts the original text instead of trusting reconstructed quote
strings. A model-result rejection permits two immutable corrective attempts and
retains the rejected receipts. Source corruption or uncertain native lifecycle is
not a semantic retry.

Reducer presentation exposes native citations and exact owner-quote references,
with child identities retained for coverage. Internal fact IDs remain in the
hash-bound original artifact rather than competing with eligible citations in
the model input. Existing frozen trial requests remain immutable. The operator
trial can reconcile an exhausted legacy reducer through one recorded presentation
revision, retaining all three prior attempts and allowing one additional native
attempt. This does not reset campaign lifetime retry counters or reinterpret an
old result against a new schema. Native schema rejections retain their session
identity and usage while remaining failed results.
An isolated native output-token excess with a complete structured result and
quiescent lifecycle can consume the remaining model-result correction budget.
The original receipt remains a limit failure; concurrent wall, wire, turn,
provider or incomplete-result failures are ineligible. Final stream sizes and
the whole-job deadline are checked again after owned descendants drain.

Campaign recovery can split a quiescent multi-child reducer after a typed native
output-token limit. Splits retain whole child summaries and their source coverage,
reuse applied or pending siblings, and preserve the original extraction frontier.
Attempt debt stays attached to the original children through both splitting and
subsequent combination. Splits consume the existing eight-round reduction bound;
an unknown worker claim prevents replacement. Other limit kinds retry the current
reducer without re-extracting the complete conversation. Scripted regression
coverage follows failed receipt, persisted cooldown, restart, splitting, successful
combination, auditing and successor assembly through the normal proposal path.

Planning treats the earned input allowance separately from the total context
window. Output consumes total context, but is not charged a second time against
the input-only allowance. Conservative JSON wrapping and prompt/working reserves
still bound planned payloads; the fully rendered request must independently pass
native admission. No budget adjustment grants a larger native capability.
An optional `claude-code-v1` native-limit recipe binds
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` to the requested output allowance and freezes
`MAX_STRUCTURED_OUTPUT_RETRIES=1`. Apply those values after inherited environment
restoration, retain them in the native receipt and pin their helper/worker
implementations. Adding these controls requires a fresh transport qualification;
an existing proof cannot acquire them through a manifest edit. These documented
[Claude controls](https://code.claude.com/docs/en/env-vars) request a per-request
output ceiling and one structured-output attempt; model caps may lower the output
allowance. Whole-job elapsed time and aggregate
token/turn validation remain independent limits, including provider reasoning
and any internal turns. Do not claim a per-request ceiling is an aggregate cap.
Reducer grouping measures the exact native-event projection, including its
source-input wrapper. The complete child summaries remain hash-bound and retained.
This avoids rejecting a usable route because internal metadata omitted from the
model input exceeds its window, while still refusing an oversized projected child.

Explicit trial reuse follows only retained bindings and revalidates each source,
native receipt and earned route qualification. Map and reducer routes may use
separately qualified efforts. The operator can configure up to 64 concurrent trial
jobs and drain admission while existing jobs finish; this is a bounded trial
ceiling, not a claim about provider capacity. Provider limits remain observable,
and structural success remains distinct from independent semantic qualification.
