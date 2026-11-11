# Audit and organize an existing repository

`bmad-loop repo` inventories existing GitHub work and local Git state before
organizing it under a frozen owner policy. It uses a separate ledger from
development campaigns. Auditing does not start stories, acquire their worktrees
or change GitHub. Reconciliation applies independently reviewed proposals within
the policy's mutation and closure limits.

The inventory includes issues, PRs, milestones, branches, local worktrees and
available ownership evidence. Bounded coding-CLI jobs interpret that evidence;
Python validates object identities, freshness, authority and outcomes. Large
inventories are partitioned into bounded jobs. An incomplete inventory or an
object that cannot fit its declared limit remains visible and prevents claiming
complete coverage.

Status exposes `inventory_coverage`, including catalog limits and separate
ownership-observer completeness. A truncated catalog remains held and cannot
be reconciled, including when an older ledger recorded it as complete. Resume
retains its frozen inventory; start a new audit with sufficient limits to obtain
complete coverage. Unavailable ownership evidence independently constrains
the actions that require it, without preventing safe organization of a complete
catalog.
The hold blocks new reasoning and remote mutations. Already issued jobs and
uncertain GitHub writes remain observable: their exact receipts and readback
can settle, including after stop, without replaying writes or granting complete
coverage. Uncertain outcomes and observation errors remain visible with their
original attempts, counters and frozen evidence.

Every source appears in a recorded shard. Related destinations are selected
deterministically from the full inventory using references, milestones and title
overlap, within a separate prompt budget. Candidate selection and omission
counts are retained. This gives complete source coverage without claiming an
exhaustive semantic comparison of every possible pair. A proposed destination
still needs fresh full detail and independent review before an action can apply.

## Configure standing authority

Save a local JSON or TOML policy. The capability manifest must contain exact,
earned reasoning permissions for both routes. Replace these model and effort
examples with qualified local routes from distinct provider families.

```toml
schema_version = 1
repository = "owner/repository"
capabilities = "/absolute/path/qualified-native-routes.json"
authorized_by = "repository-owner"
target = "main"
remote = "origin"
labels = ["duplicate", "superseded"]
protected_labels = ["bmad:hold"]
max_mutations = 100
max_closures = 20
max_sessions = 3
max_attempts = 2

[proposer]
profile = "codex"
model = "gpt-6.1-sol"
effort = "ultra"
provider_family = "openai"

[reviewer]
profile = "claude"
model = "claude-opus-5-5"
effort = "max"
provider_family = "anthropic"
```

`authorized_by` records the owner who supplied the local policy. Repository
comments and model output cannot supply or expand this authority. Only the
configured managed labels may be added; existing labels are retained and
protected labels remain holds.
Proposal and review sessions share `max_sessions`. Attempts and failure history
survive resume. Admission waits do not consume a launch attempt.

Optional `communication_room` enables a managed controller endpoint for each job.
Supply an existing absolute hcom room directory explicitly; the audit does not
borrow the caller's ambient room or impersonate a native agent. Endpoint replies
identify themselves as controller status, and incoming messages remain advisory
mail rather than owner authority. Status includes endpoint availability and
closing state.

To show jobs in an existing Paradev workspace, set both
`presentation_workspace` (its absolute path) and `presentation_workspace_id`.
The workspace must belong to the audited repository. This creates no worktree
or workspace and preserves existing tab names and layouts. Native control files
live outside the read-only repository workspace; the audit ledger retains the
evidence and frozen policy in Git's common directory.

The default limits are 64 MiB of aggregate metadata, 1,000,000 detail bytes per
object, 10,000 objects, 1,000 pagination pages and 128 reasoning jobs. Each job
defaults to 131,072 input bytes, 8,192 output tokens, four turns and 1,800 seconds,
further constrained by its exact native qualification. Set explicit limits for
larger repositories; exceeding a limit never silently turns a partial scan into
a complete audit.

## Audit, inspect and reconcile

```sh
bmad-loop repo audit --project /path/to/repository --policy /path/to/policy.toml --json
bmad-loop repo status AUDIT_ID --project /path/to/repository --json
bmad-loop repo history AUDIT_ID --project /path/to/repository --json
bmad-loop repo reconcile AUDIT_ID --project /path/to/repository --expected-revision REVISION --json
```

Audit produces evidence-backed proposed actions and an independent review.
`reconcile` activates the saved policy against that exact audit revision. Fresh
remote readback precedes writes; changed objects invalidate stale proposals.
Every write has a durable intent and readback. An uncertain acknowledgement is
reconciled before another write is considered.

Supported organization includes managed labels, assignment of unassigned issues
to existing open milestones, linked
consolidation comments and evidence-backed duplicate or superseded closures.
Closing an issue requires a retained successor and proof that its obligations
remain represented. An active or uncertain campaign owner holds issue closure,
including before it has opened a PR. PR closure also requires positive authenticated ownership
release and successor ancestry. Configure an installed `ownership_observer`
when that evidence is available; absence or unknown ownership holds PR closure.
The workflow never deletes a branch, worktree or original discussion.

```sh
bmad-loop repo stop AUDIT_ID --project /path/to/repository --json
bmad-loop repo resume AUDIT_ID --project /path/to/repository --json
```

Stop is durable and prevents new admissions and writes while issued work settles.
Resume retains the audit identity, proposals, attempts, usage and write history.
Quota waits remain distinct from invalid results. A launcher with an uncertain
outcome is never replaced until its ownership and completion are established.

Use [development campaigns](campaign-guide.md) to execute accepted stories and
deliver their reviewed PRs. Repository organization does not satisfy a story's
acceptance criteria or mark a campaign delivered.
