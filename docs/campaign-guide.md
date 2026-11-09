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

Ticket sources default to `completion_scope = "epic"`: selecting only some
children still retains the native epic closure obligation, which waits for every
required child and a substantive retrospective. To deliver a bounded selection
from an ongoing epic, explicitly select ticket refs and opt into
`completion_scope = "selected-units"`:

```toml
[[sources]]
id = "selected-stories"
kind = "tickets"
initiative = "initiative-api"
units = ["1.2", "3.4"]
completion_scope = "selected-units"
```

This mode completes only after selected work, required dependencies and repair
descendants pass the normal review, merge, actual-landing acceptance and source/
issue reconciliation gates. It creates no new epic closure work, writes no parent
retrospective and does not close parent issues or milestones, even if the current
GitHub projection has no remaining open children. Unselected native siblings stay
untouched. Status reports **Selected work delivered; parent scope remains open**
and exposes `tracking.source_completion_scopes` and `tracking.selected_work`.
The persisted choice survives resume; omitted fields retain legacy epic scope.

Selection does not erase native dependencies. An omitted done ticket still needs
verified reconciliation. A hard dependency on whole-epic closure must already
have complete native closure evidence, including its retrospective, before it can
be reconciled in this mode. Missing closure work requires a separate full-epic
campaign; a selected-units worker cannot author it as an implicit expansion.

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

For repositories requiring two actual native Codex reviews, configure the optional
role below with an earned paired-review capability in that route's `review` slot:

```toml
[roles.native_review_pair]
profile = "codex"
model = "gpt-6-astra"
effort = "high"
max_sessions = 2
```

The pair reviews the exact integrated base and then the complementary acceptance
and risk requirements. It runs before the separate `publication_review` stage and
shares the review and total-session limits. Both native findings sets are retained;
the third turn only maps them into the campaign result. Changed candidate or base
invalidates the pair. Findings enter same-PR repair, and publication waits for a
fresh approved pair plus independent review. A single-review or ordinary reasoning
capability cannot satisfy this role. Leave the role absent for repositories whose
policy does not require the pair.

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
bypass in strict or queue mode.

`direct-authorized` is a separate, explicitly repository-bound policy. It uses
ordinary exact-head GitHub merge first. `allow_admin_merge = true` authorizes an
administrative retry only after a definitive policy refusal and fresh gate checks;
an ambiguous response is reconciled before any retry. This mode retains holds,
configured checks, selected local reviews, verification and history preservation.
It does not claim server-enforced atomic freshness of the base. The actual landed
commit must pass acceptance before delivery; failed landing proof creates one
linked repair PR and keeps original tracking pending until the repair is accepted.
Repair drafts retain the original accepted product specification from its exact
reviewed Git head, including embedded native plan text and source hashes. This
also works when ticket acceptance lives in the native plan rather than duplicated
campaign criteria; the repair does not acquire the parent's lifecycle selector.

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
campaign state. Project-command is an optional integration for repositories that
maintain such an executor. Repositories using ordinary GitHub publication select
a native merge mode instead; no project-specific merge script is implied.

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

Managed workspace, task and terminal titles start with the observed hcom name,
epic, issue and PR: `@tina · E:epic · I:#42 · PR:#18 · description`. Missing
bindings display `pending`; no name is inferred from a model or another worker.
Groups aggregate their members' actors/issues/PRs, with counts when the bounded
label omits additional members. Stable workspace and group identities survive
renaming. Actor changes and discovered GitHub links refresh the labels, including
quiescent group members; native tasks also receive the linked PR URL.

Campaign-created tabs inherit terminal titles. Existing manually titled tabs
retain their operator label and can mask a terminal's automatic tab title.
Operator terminal text follows the managed navigation prefix. Native tags are
lowercased; earlier-sorting operator tags can still determine the sidebar folder,
which reflection reports as `operator-tag-precedence`.

Headless reasoning jobs use an owned campaign workspace for their terminal while
keeping their input/output state outside the editing tree. Portfolio jobs wait
visibly until a workspace is available. Job status includes the terminal and
workspace IDs; navigation fields precede the job identity, role and model. A
portfolio job has explicit unbound/none work fields. With managed communication,
the title uses the public bridge address registered for that launch. Native
conversation identity remains a separate harness observation; a parent's name
is never borrowed.

`communication.mode = "observe"` enables bounded hcom observations and structured
sibling context where the installed launch chain supports them. Campaign messages
are advisory and cannot assign work, transfer ownership or establish completion.
Automatic inbound peer wakeups remain unavailable until their Stop-event closing
behavior is qualified; preexisting hcom hooks can still deliver messages.

TeamRouter capacity observation does not prove that a process was launched through
TeamRouter. Execution follows the resolved profile or qualified capability's
executable and arguments. Select and qualify the installed wrapper when that route
requires TeamRouter; a direct native binary retains its own authentication path.

Hcom communication defaults to off. Observation mode does not enroll agents or make every
headless job messageable. A configured room is carried into fresh headless
requests after inherited actor identities are removed; the selected harness must
still provide its own enrollment and receive loop. Grok and Muse's bounded
headless profiles do not claim native hcom hooks.

Set `communication.mode = "managed"` with an absolute `communication.room` to
give each coding launch and reasoning job its own controller bridge address.
This mode requires qualified bounded-headless routes. Mandatory Claude/Codex
wrapper enrollment uses private per-job rooms; the controller bridge is the
public address shown in Paradev. Grok, AGY and Muse use the same public bridge
without claiming native hcom delivery support.

An outside agent can send a status request to that public address. The response
identifies itself as controller facts, not an answer generated by the model.
For steering, use the CLI or send a JSON envelope with `bmad: 1`, `kind: "steer"`,
the exact `target` from `campaign endpoints`, and `text`. The target binds campaign,
unit/job, launch and generation; a stale generation cannot steer its successor.
Mail is saved before acknowledgement and included at the next eligible stage
boundary. Unit mail targets coding stages; `--job` targets one reasoning job.
Acceptance, inclusion in a completed stage and compliance are distinct: an
`applied` receipt does not assert the model obeyed the message. Inspect `messages`
for pending, applied or closed receipts. Oversized messages are rejected; mail
that cannot fit a reasoning request remains queued.

Closed endpoints never restart a writer. Lost acknowledgements, unknown owners
and a replaced room database remain explicit errors. Completion/drain waits for
owned bridge closure. Managed communication uses the installed hcom service and
does not require a companion hcom feature PR.

An outside agent can inspect or message a registered, listening actor using its
own identity in the same project room. Verify the current name and session before
sending a check-in. Queued mail is not proof of delivery to a running model, and a
one-shot process with no receive loop cannot answer it. Peer messages remain
advisory; use campaign controls for holds, releases and other controller state
changes. A delegated agent's message does not itself establish owner approval.

## Operator controls

```bash
bmad-loop campaign list --project /path/to/project --json
bmad-loop campaign status CAMPAIGN_ID --project /path/to/project --json
bmad-loop campaign jobs CAMPAIGN_ID --unit QUALIFIED_UNIT --limit 100 --json
bmad-loop campaign jobs CAMPAIGN_ID --job JOB_ID --details --json
bmad-loop campaign endpoints CAMPAIGN_ID --after 0 --limit 100 --json
bmad-loop campaign messages CAMPAIGN_ID --unit QUALIFIED_UNIT --json
bmad-loop campaign steer CAMPAIGN_ID --unit QUALIFIED_UNIT --text "Preserve the agreed acceptance" --json
bmad-loop campaign steer CAMPAIGN_ID --job JOB_ID --text "Consider the new owner evidence" --json
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

When reasoning and `handoff_map` are enabled with a qualified summarization route,
live sessions with exact hook-backed transcripts prepare optional prefix summaries.
Warmup starts after 60 seconds, revisits growth of at least 64 KiB, and prepares at
most four maps per observation. Required reasoning jobs have admission priority.
`status --json` exposes `units[].operator.history_warmup` with availability, coverage and
failure details; `jobs` retains the map receipts. Missing bindings or routes do not
hold coding work. Active managed-headless streams wait for their quiescent export.

These summaries grant no writer lease. Handoff rechecks cached raw prefixes,
source fragments and their original qualified extraction contract against final
capture, then audits the current owner constraints before replacing a worker.
Changed or corrupt optional cache entries are re-extracted. Complete native-store
migration is not provided; portable context and source retrieval preserve the
handoff contract.

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
BMAD_RELEASE_ROOT="$HOME/.local/share/uv/bmad-loop-releases/FULL_MERGED_SHA"
env UV_TOOL_DIR="$BMAD_RELEASE_ROOT/tools" UV_TOOL_BIN_DIR="$BMAD_RELEASE_ROOT/bin" \
  uv tool install "bmad-loop[tui,paradev,non-linux] @ git+https://github.com/open-runtime/bmad-loop.git@FULL_MERGED_SHA"
"$BMAD_RELEASE_ROOT/bin/bmad-loop" campaign provenance --expected-commit FULL_MERGED_SHA --json
```

Use a new permanent release directory; do not replace an environment used by
older live or paused workers. On macOS, `non-linux` supplies psutil for authenticated
process identities. Qualify installed native paths before switching the future
global entry. The default uv registry can still describe the preserved old tool;
inspect or upgrade the new release with its explicit `UV_TOOL_DIR` and
`UV_TOOL_BIN_DIR`. Never relocate an already qualified environment. Installation,
canonical base update, loaded-process provenance and target-project preflight are
separate checks.

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
no-tools reasoning, read-only tools and scoped execution. Preflight checks qualification and
campaign creation freezes the manifest. Route installation alone is insufficient.
When one model has separately qualified thinking levels, keep their earned
capabilities under `by_effort` in its `profile:model` route:

```json
{
  "schema_version": 1,
  "routes": {
    "grok:grok-4.7": {
      "by_effort": {
        "medium": { "reasoning": { "efforts": ["medium"] } },
        "xhigh": { "reasoning": { "efforts": ["xhigh"] } }
      }
    }
  }
}
```

The abbreviated capability objects above must each be replaced with that
effort's complete earned capability. A variant may also contain an independently
qualified `execution` capability. The selected role's exact effort chooses the
variant; missing levels fail visibly and never borrow another effort's proof.
Existing single-capability and `reasoning`/`execution` route forms remain supported.

Configure `routing`, `reasoning`, `graph_review`, `reporting`, `summarization` and
`handoff_audit` role pools as needed, alongside the ordinary triage/execution
roles. Each job also respects total campaign, role and current provider capacity.
The packet limit is an initial context ceiling, not an aggregate history limit;
retained exact sources and obligation indexes remain available for retrieval.
The hash-bound summary catalog also retains intermediate summaries and their
inputs. Omitted final summaries remain retrievable after audited context fitting;
their child references lead to the detailed summary tree. Retrieval verifies the
catalog, summary bytes and original input, and refuses missing or changed data.

After independent current-constraint audits, the initial packet includes complete
canonical obligations and a bounded view of whole historical facts. Each fact
names its immutable parent summary and fact identity; omitted facts and complete
nodes remain explicitly indexed. Partial views carry context, not approval or
completion authority. Missing canonical obligations or a packet with no room for
any useful historical fact requires context recovery before writable continuation.
Successor launch rechecks the bound authority inputs and audit artifacts. Saved
packets lacking that binding refresh their evidence before reuse; a running
successor is allowed to reach its normal completion boundary.

For retrieval inside the macOS execution sandbox, prefer a direct interpreter
command such as `python -c` or an explicitly granted script file. Shell heredocs
may need temporary files outside the permitted paths even when `TMPDIR` names a
private directory. A denied temporary-file operation does not establish missing
history; use a compatible command without widening the frozen grants.
Preserve exact JSON escaping and newlines when returning source quotations.

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

Changing the effort, launcher, transport or permissions requires matching native evidence;
copying a capability and editing its checklist does not qualify the new route.
Keep its report, frozen request, source and native receipt together. New reports
bind the complete request file; earlier reports use their matching frozen request
and owned receipt without modifying the retained proof.
Native selected aliases and served model names remain separate in receipts.

An explicitly configured `reasoning` capability can use `read-only-tools` when
the harness needs repository reads. This is a distinct earned permission: its
filesystem sandbox permits scratch and pinned native runtime writes, with no product
scope, Git branch/index/object or parent-directory write grant. It cannot borrow a
writer proof. The same `qualify-writer` command accepts read-only proposals; its
fixture asks the model to read a private workspace marker and verifies unchanged
product/Git state plus write denial in a separate descendant.

Codex uses the pinned owner wrapper through native app-server stdio. The
`codex-app-server` recipe performs one schema-constrained turn. On macOS the owner
wrapper remains outside Seatbelt so its process-ownership checks can run. The
`codex-native-read-only-v1` confinement selects native read-only tools with network
disabled. Qualification exercises `command/exec` through the identical native
policy to prove product, Git, outside-path and descendant write denial before
submitting the model readback. A read-only result alone cannot earn that proof.
Its separate
`codex-native-review` recipe performs `review/start`, retains the native review
artifact, then runs one bounded schema-mapping turn for campaign coordinates and
acceptance evidence. The native RPC renders review output as prose; immutable
native rollout snapshots retain the structured review and delegate token usage.
The reader binds the parent thread, review root turn, child turn and review item
before using those records. Both native review turns are counted. Every native finding
is preserved as required and unresolved, so the mapping cannot approve it away.

Put that independently qualified capability in a `review` slot beside `reasoning`
and `execution`; planning, publication and landing review prefer the explicit
review slot. A failed selected review proof blocks admission. Routes without this
slot retain their existing review behavior. Neither Codex route claims no-tools.

Other harnesses can place their independently qualified `read-only-tools`
capability in `review` while retaining a no-tools capability in `reasoning`.
For example, a Grok code reviewer can inspect the checkout without replacing the
larger no-tools route and semantic proofs used by history reducers. A prompted
code review is distinct from a native Codex review; it cannot satisfy
`native_review_pair`. Writer capabilities are not accepted in the review slot.

The separate `codex-scoped-tools-v1` writer recipe keeps native tools read-only
and registers `bmad_shell` through the native dynamic-tool interface. Only that
callback can write, using the frozen campaign grants and private Git object bank.
Callbacks bind the native thread, turn, tool and call identity; a repeated call
returns its retained result instead of executing again. Each launch permits at
most 32 commands, 60 seconds and 64 KiB output per command. Qualification proves
native builtin write denial separately from the scoped edit/commit and protected
history checks. Foreground commands and observed descendants must drain; persistent
background services are outside this route's lifecycle contract.

When a selected model reports a native served-model alias, that alias can retain
its identity evidence beneath the exact scoped sandbox wrapper only when the
nested native executable hash is unchanged. The writer still needs its own
permission and lifecycle qualification.

The separate AGY writer recipe uses the documented pre-tool argument rewrite.
It allows only schema-valid native `finish` and native `run_command`, rewriting
each command to the bounded scoped executor. Other direct file, network and MCP
tools are denied. Native conversation/step identities bind retained calls, and
the pre-invocation hook bounds the complete job. The writer requires its own
actual edit/commit qualification; no-tools evidence cannot grant this capability.

The pinned AGY no-tools recipe installs private native pre-tool and pre-invocation
hooks. It denies all general tools and allows only the native `finish` output
operation with arguments validated against the frozen result schema. This permits
native schema completion without granting file, execution, network or messaging
tools. Native terminal output supplies completion data; a finish call alone is
never completion. Invocation limits apply across the whole job. Malformed schema
output retains native model/session/usage metadata as a failed result.

Workspace-tools routes need their own qualification:
`bmad-loop campaign qualify-writer PROPOSAL --directory NEW_PRIVATE_DIRECTORY --json`.
The command creates a disposable fixture repository and isolated worktree, then
asks the native CLI to edit and commit exactly one owned file. Independent readback
checks committed bytes, clean Git state, unchanged sibling/outside files and the
main ref, native session/model/usage and process quiescence. A separate descendant
under the identical frozen OS sandbox verifies that protected writes are denied.
The current write confinement is macOS only. Use a private directory approved for
local worktrees; the command refuses existing occupied directories unless they
contain its exact resumable job. It never uses the current repository as its fixture.

The proposal's executable is a regular, hash-pinned launcher containing
`campaign_execution_qualification.launcher_text()` with executable permissions.
Its managed-interpreter shebang preserves the virtual environment. Pass
`--grants {grants_path} -- ABSOLUTE_NATIVE_CLI ...` and pin the sandbox module,
resolved Python interpreter, native executable and all native wrapper dependencies.
Use native tool arguments appropriate to the fixture; the mandatory outer sandbox
remains the write boundary. Native runtime write directories must be explicit in
the proposal. No-tools qualification cannot substitute for this proof. Passing
qualifies the measured input and configured budgets for this finite operation;
it does not qualify every coding workflow, cross-provider continuation or a
provider's advertised limits.

Native control files live outside the writable checkout. Tool grants cannot
expose requests, lifecycle receipts, the worktree's Git pointer, controller state
or shared Git storage. The writer uses private staging and refs; checkpoint and
completion publication seal objects before advancing the owned canonical branch.
Comparisons can read a snapshot of main and other refs. If an owner changes
canonical staging during publication, the campaign preserves it and reports a
reconciliation hold. Replaying completed publication does not replace newer
staging.

For Grok, large prompt files require its native `--verbatim` option to avoid
abbreviated input that would require a later tool read. Writable sessions use
`streaming-messages-json` so the native terminal result preserves final assistant
text separately from progress prose. Native usage counters and turn counts still
supply attribution and limits; prose is never stripped heuristically to invent
a result. For Muse, the qualified
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

The exact rendered input includes schema and source metadata. Oversized chunks
split independently, preserving chunks that already fit; reducers retain whole
validated children and the citations actually represented by those children.
Native owner quotations use immutable span IDs, and the controller retrieves the
exact bytes. Each rejected completed result permits at most two separately
recorded corrective jobs; source corruption and unknown actor state do not retry.
A complete structured result rejected only for measured native output tokens can
use that same remaining correction budget. Its receipt remains a limit failure;
wall, wire, turn, quota and incomplete-result failures cannot use this path.

Reducer input separates eligible native citations from internal fact IDs; complete
original artifacts remain hash-bound. An exhausted frozen legacy trial reducer
can receive one recorded presentation revision with one additional attempt,
preserving all earlier failures and usage. This does not renew campaign retries.
Output token limits use verified native telemetry when available, including the
provider's reported reasoning contribution. Unknown usage retains a conservative
UTF-8 byte bound. Model-supplied usage fields do not establish that measurement.

Campaign reducers retain complete validated artifacts separately from the bounded
initial context. Native input, output, turn and time limits still apply. Packet
assembly budgets the exact remaining bytes after mandatory state and independently
audited constraints; it selects whole facts without clipping their text. Explicit
artifact limits on older jobs remain enforced, and a later packet policy cannot
turn an earlier rejected result into a successful receipt. Retry cooldowns are
persisted per failed attempt, so polling and restart do not continually postpone
the only available route.

When a stopped reducer exceeds its native output-token allowance, recovery can
split its input at whole-child summary boundaries. Completed sibling summaries,
source coverage and retry counts survive; the pipeline does not restart all
history extraction. Unknown worker state blocks replacement. At eight completed
reduction rounds, or when a completed frontier stops shrinking, the campaign
records limited compaction and retains its complete artifacts for audited context
selection. Failed, live or structurally incomplete jobs cannot use that fallback.

Completed job receipts and validated results allow the same command to resume;
changed sources, route or limits require a new trial directory. Failure stops new
admissions while existing bounded jobs drain. `--reuse PRIOR_DIRECTORY` follows
explicit retained trial bindings, revalidates native qualification and source
coverage, and preserves completed work across route changes. `--reducer-capability`
selects a separately qualified reducer route, allowing a different effort from maps.
Trial concurrency is explicitly bounded to 1–64 jobs. `--drain` stops admissions
while admitted jobs finish; rerunning after the drained report resumes the work.

The report distinguishes structural completion from semantic qualification: automatic handoff additionally requires a
separately recorded review from an independent provider that verifies critical
obligations, conflicts and authority against the source records. Tests for these
commands use scripted native processes and consume no model tokens.

Semantic qualification uses a version 2 proof list attached to a native capability.
Each proof binds the exact model, effort, transport qualification, current readers,
schemas and prompts to the operation it qualifies: history extraction, reduction,
audit, constraint reduction/audit or recovery extraction/audit. The independent
native reviewer must attest to those same bindings and the source corpus. Generic
history receipts include `semantic_contract_digests` for each qualified operation,
binding its used readers, schemas and prompt. Updating the manifest cannot extend
an earlier native result to a changed contract. Finite constraint/recovery proofs
retain their own schema attestations; unrelated history-schema changes do not
invalidate them.

A generic history-auditor proof also binds its current audit prompt version. A
successful history summary does not qualify an auditor or a different thinking
level. Retain all referenced proof and source files; changed or missing evidence
leaves the operation waiting for qualification. Earlier generic semantic proofs
require renewed assessment; native transport proof compatibility is separate.

History audits retain every character of native owner messages through stable
source spans and exact controller-reconstructed quotations. Current and contextual
spans both remain available to the successor, including requirements adopted from
quoted plans. A contextual status question does not create an approval hold.
Historical text cannot be excluded based on a model's completion claim: no current
producer supplies the authenticated span-specific resolution proof needed for that
exclusion. Large constraint views pass through separately qualified compression
and independent audit while their exact source index remains retrievable.
