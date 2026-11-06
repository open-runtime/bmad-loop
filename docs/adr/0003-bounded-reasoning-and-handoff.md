# ADR 0003: Bounded reasoning jobs and portable session handoff

- **Status:** Proposed; implementation and qualification pending
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

Extend stop/drain/terminate, observation, restart reconciliation and campaign
completion to job claims as well as unit claims. Draining closes new admissions
and accounts for already admitted dependencies; termination reconciles each owned
job through its transport. Required jobs gate their parent operation. Optional
background reports may be explicitly canceled and journaled, but a campaign cannot
report stopped or fully drained while a job remains live or ownership is unknown.
Parent cancellation cannot leave untracked orphan workers.

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
A bounded, explicitly scoped model-quality evaluation must report lost obligations,
contradiction handling and recall of critical details against a known corpus; fake
outputs do not prove summarization fidelity. Never add token-consuming live calls
to the repository test suite. Enable automatic semantic handoff only after the
finite qualification corpus retains every designated critical obligation and
owner hold, preserves unresolved contradictions, and yields no invented approval
or completion. Passing that corpus does not promise lossless compression of
arbitrary histories. Measure total input/output work, latency, failure
recovery, delivered throughput and working-context size before advertising scale.
Original merge, exact-install and selected REM-delivery gates still apply.

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
