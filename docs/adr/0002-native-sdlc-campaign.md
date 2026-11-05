# ADR 0002: A deterministic SDLC campaign with GitHub delivery

- **Status:** Accepted design; implementation planned
- **Date:** 2026-11-05
- **Scope:** An opt-in campaign supervisor, isolated workers, planning and GitHub synchronization

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
2. The complete PR head includes the recorded current target head. Code and
   artifact synchronization preserves published history.
3. Local verification and independent final review identify the same full PR
   head, target head and trusted policy fingerprint. A later change invalidates
   the affected evidence.
4. Required GitHub checks and reviews are satisfied; unresolved review requests,
   conflicting changes, human holds and unknown observations prevent merging.
5. The merge uses the expected head identity and server-enforced up-to-date
   protection or a merge queue. A client-side mutex cannot protect against other
   publishers moving the target.
6. GitHub reports the PR merged. Its merge commit is reachable from a freshly
   fetched canonical target. A closed, unmerged PR remains unfinished.
7. Source records, linked issues and milestone/parent progress are reconciled.
   Only then does the campaign release dependent units and report delivery.

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

| Stage | Deliverable                                                     | Acceptance                                                                                                                                  |
| ----- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | Versioned campaign specifications, state, transitions and store | Invalid/cyclic inputs refuse before effects; persistence is atomic; duplicate coordinators refuse.                                          |
| 2     | GitHub gateway, outbox and publication controller               | Early drafts, pagination, exact-head/base gates, unknown-write reconciliation and merge read-back work through fake transport.              |
| 3     | Issue hierarchy, managed content and discussion inbox           | Stable IDs, native subissues, milestone progress and linked comments survive retry without duplicates or lost human edits.                  |
| 4     | Planning sessions and source import                             | A brief yields validated product plans and ready units through existing session completion authority.                                       |
| 5     | Isolated workers and dependency scheduler                       | Multiple units execute concurrently without shared engine state; dependencies release only after delivery.                                  |
| 6     | CLI and complete delivery/recovery loop                         | One command plans, dispatches, reviews, publishes, waits, merges, reconciles and continues; stop/resume preserves identities.               |
| 7     | Qualification and migration                                     | Zero-token end-to-end tests cover crashes and moving heads; configured project preflight proves readiness before replacing its automations. |

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
