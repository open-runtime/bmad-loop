# ADR 0003: Bounded reasoning jobs and portable session handoff

- **Status:** Implemented in the development PR; native qualification and review gates remain incomplete
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

Build summaries incrementally while work proceeds. Hash sealed source ranges,
prompt/schema versions, route identities and extraction settings for cache keys.
New input recomputes the tail and affected ancestors. New authoritative decisions
invalidate affected interpretations even when old raw chunks remain reusable.
An interrupted reduction resumes completed jobs instead of replaying the pipeline.

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
3. Execution route pools, typed quota receipts, fenced replacement, native import
   adapters, attribution and hcom/Paradev lineage.
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

Semantic qualification uses schema-2 proofs for exact operation, effort and
earned transport-evidence hash. A capability may reference several independent
proofs for history extraction, reduction, audit and the separate constraint or
recovery operations. The native independent result must bind the complete
route-purpose list digest as well as the corpus and any additional contract
schemas. Editing a manifest cannot extend an existing receipt to another effort
or purpose. Older generic semantic proofs require requalification; verified
version-1 native transport canaries retain their separate supported read path.
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
reason to manufacture qualification or silently omit history. Current full-history
and independent-fidelity qualification remain incomplete.

A delivered revised plan does not itself waive prior acceptance. The explicit
`authorize-scope` control binds the operator's reason to the exact ledger revision,
before/after scope and delivered planning revision; changed coordinates refuse it.
Reintroducing a waived criterion makes it required again. This operation never
clears another owner hold or supplies a GitHub review approval.

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
The CLI retains failed startup attempts and resumes only the same immutable job.

The separate `campaign trial-handoff` command reads explicitly sealed prefixes,
checks their immutable source hashes, deduplicates only verified inherited native
records, plans bounded maps and reducers, and persists each native receipt and
source-cited result. Jobs use the production validators. A failed job stops new
admissions while admitted jobs drain; rerunning the same frozen trial reuses its
validated results. Its structural-completion report cannot grant semantic
qualification. A separate independent-provider review must establish critical
constraint fidelity and absence of invented authority before automatic handoff.
The real whole-thread trial remains a qualification gate; a successful fixed
transport canary and scripted regression coverage do not satisfy it.

Rendered-input planning splits an oversized map or reducer independently rather
than shrinking every job's payload. Map splits retain exact native ranges; reducer
splits retain complete validated children, with citation enums drawn from their
actual retained facts. Owner quote selections identify verified source spans;
the controller extracts the original text instead of trusting reconstructed quote
strings. A model-result rejection permits two immutable corrective attempts and
retains the rejected receipts. Source corruption or uncertain native lifecycle is
not a semantic retry.

Explicit trial reuse follows only retained bindings and revalidates each source,
native receipt and earned route qualification. Map and reducer routes may use
separately qualified efforts. The operator can configure up to 64 concurrent trial
jobs and drain admission while existing jobs finish; this is a bounded trial
ceiling, not a claim about provider capacity. Provider limits remain observable,
and structural success remains distinct from independent semantic qualification.
