# The open-runtime fork

This fork extends [upstream bmad-loop](https://github.com/bmad-code-org/bmad-loop)
for unattended development across a factory of epic workspaces. The control loop
remains deterministic Python. Coding CLIs do the implementation and review;
the orchestrator checks their results and runs the configured verification commands.

## What comes from upstream

The implementation, review, verification and commit pipeline; sprint, stories and
ticket-tree modes; worktree isolation; deferred-work sweeps; CLI profiles; plugins;
and the per-run TUI are upstream capabilities. This fork retains those capabilities
and integrates upstream fixes.

## What the fork adds

| Capability                                               | Benefit                                                                                                                             |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Recovery that preserves source and published Git history | A retry or failed finalization can retain existing work and completed verification without replacing published commits.             |
| Native conversation continuation                         | An eligible interrupted session can continue its conversation instead of losing its context to a fresh development attempt.         |
| Review recovery                                          | Source-bound implementation attestations and retained review obligations let supported recoveries finish the review they still owe. |
| Smart stall handling                                     | A silent session that asks a question parks; a session with unfinished work can receive a bounded nudge.                            |
| Provider-error classification                            | Transport, account, usage and TeamRouter pool failures get distinct, visible recovery paths.                                        |
| hcom nudges                                              | Nudges can reach a listening agent through hcom, with terminal delivery as a fallback.                                              |
| Policy reload at story boundaries                        | Supported policy changes apply between stories; launch-affecting changes wait for an explicit resume.                               |
| Resume bookkeeping                                       | Completed resumed stories fire their post-story hook, and supported baseline changes update the plan consistently.                  |
| Repository housekeeping                                  | Engine commits honor configured signing, migrated spec paths remain usable, and retained run archives stay out of story commits.    |
| Factory dashboard                                        | One read-only view summarizes many epic workspaces, verification locks, scheduler state, TeamRouter capacity and pull requests.     |
| Ticket and findings options                              | Ticket runs can defer human-only tickets within an epic, and findings can retain configurable amounts of summary and evidence.      |

See the [recovery guide](loop-recovery-guide.md),
[factory dashboard](tui-guide.md#factory-overview) and
[configuration reference](reference/configuration.md) for controls and defaults.
Most recovery options are opt-in; installing the fork does not enable every option.

## Parallel stories and epics

| Scope                  | Current behavior                                                                                                           |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| One bmad-loop run      | One story or ticket at a time. `scm.max_parallel` is clamped to `1`.                                                       |
| Independent runs       | Multiple runs can execute concurrently in separate worktrees, each with its own state and terminal sessions.               |
| Factory scheduling     | An external scheduler can start eligible epic runs and bound their concurrency. The factory dashboard observes these runs. |
| Coding-agent subagents | A coding CLI may use its own subagents within a story; that does not add simultaneous story execution to the orchestrator. |

Use a separate workspace/worktree for each concurrent epic, assign disjoint work,
and coordinate shared verification and publication resources in the external
scheduler. Separate worktrees isolate files; they do not eliminate dependencies
between epics or conflicts when changes are merged. The parent-initiative chain
also dispatches its child runs in order, not concurrently.

Internal parallel story dispatch is still [planned](ROADMAP.md#parallel-unit-execution-scm-max_parallel).

## TeamRouter

The bundled `claude` and `codex` profiles launch their executable by name from
`PATH`. When those names resolve to TeamRouter wrappers, bmad-loop sessions use
TeamRouter's account routing. Keep the wrapper's arguments and native CLI hooks
compatible with the chosen profile. A shell alias alone is insufficient for a
subprocess launcher; the executable or profile must select the wrapper.

The integration has three parts:

- **Session routing:** the wrapper and TeamRouter service route the coding CLI's
  requests. bmad-loop does not store provider credentials or choose accounts.
- **Recovery:** `[recovery] classify_turn_errors = true` recognizes TeamRouter
  pool refusals and records an uncharged pool-fault pause, including a return time
  when supplied. This cannot manufacture capacity when no eligible account exists.
- **Observation:** the factory dashboard can read `teamrouter status --json`
  through its optional `[factory.capacity]` configuration.

Check `command -v claude`, `command -v codex` and `teamrouter status --json` to
inspect local routing and availability without starting a model session. Initialize
the corresponding native hooks with `bmad-loop init --cli claude --cli codex` in
the target project and complete each CLI's first-run setup before unattended runs.

## Install this fork

Pin the reviewed fork commit or release you intend to run:

```bash
uv tool install --force --reinstall \
  "bmad-loop[tui,non-linux,opencode] @ git+https://github.com/open-runtime/bmad-loop.git@<commit-or-tag>"
bmad-loop --version
uv tool list
```

The source URL matters: an upstream installation does not include these fork
extensions. Update at a quiet boundary, preserve environments used by existing
processes, and refresh each project's installed skills and hooks using the
[setup guide](setup-guide.md#upgrading).
