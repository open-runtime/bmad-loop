# Loop recovery guide

This guide covers the `[recovery]` policy table of the open-runtime fork of bmad-loop: how the loop handles a session that goes quiet, a turn that ends on an API error, a retry after a stalled session, a story finished by a resume, a plan whose baseline moved, and a policy edited mid-run. Every behaviour sits behind a key. The defaults keep upstream behaviour, except the two defect fixes (`post_story_on_resume`, `restamp_plan_baseline`), so a run started by an upstream engine resumes unchanged under this one.

The keys are listed in the [configuration reference](reference/configuration.md#recovery). The escalation side (what a pause looks like, and how to clear it) is in the [escalations guide](escalations-guide.md#turn-error-pauses).

## Keys at a glance

| Key                     | Default     | What it changes                                                                         |
| ----------------------- | ----------- | --------------------------------------------------------------------------------------- |
| `post_story_on_resume`  | `true`      | A story a resume finishes on a replay arm fires `post_story` once                       |
| `restamp_plan_baseline` | `true`      | The plan's `baseline_revision` follows an accepted baseline and a `pre_dev_phase` merge |
| `stall_check`           | `"classic"` | `"smart"`: park only on a question; otherwise nudge in place                            |
| `turn_end_nudge_s`      | `0`         | With `"smart"`: seconds after a status-report turn end before the nudge                 |
| `classify_turn_errors`  | `false`     | Classify a turn that ended on an API error; pause it uncharged                          |
| `transport_nudges`      | `1`         | In-place nudges after a transport error before it pauses                                |
| `nudge_via`             | `"tmux"`    | `"hcom"`: deliver nudges as hcom requests into the session                              |
| `max_session_resumes`   | `0`         | Retries that resume the previous CLI conversation instead of starting fresh             |
| `reload_policy`         | `"off"`     | `"story-boundary"`: re-read policy.toml between stories                                 |

A setup that wants every behaviour:

```toml
[recovery]
stall_check = "smart"
turn_end_nudge_s = 90
classify_turn_errors = true
transport_nudges = 1
nudge_via = "hcom"
max_session_resumes = 2
reload_policy = "story-boundary"
```

`max_session_resumes` also needs profiles that say how their CLI resumes ([Session resume](#session-resume)).

## A story finished by a resume fires post_story

`_finish_inflight` finishes an interrupted story on one of several arms: a recorded session's result replayed (`resume-verify`), the commit window completed (`resume-commit`), a spec-approval pause continued, a `resolve --reverify` replay, a dispatch pause resumed. Upstream, only the restart arm ran `post_story`, so a plugin that publishes on it (a push-and-merge plugin, a notifier) never saw such a story. With `post_story_on_resume`, any of these arms that leaves the story `done`, `deferred` or `awaiting-operator` fires `post_story` once, before the stories-mode done checkpoint, and journals `resume-post-story`. The restart arm is unchanged: it already fired the hook.

## The plan's baseline follows the recorded one

The dev gate rejects a plan whose `baseline_revision` differs from the baseline the run recorded for the attempt. Two engine moves used to leave the plan's claim behind:

- `bmad-loop resume --accept-baseline` adopts HEAD as the baseline. After the restart arm's rollback, the plan is re-stamped to it.
- A `pre_dev_phase` hook that moves HEAD (a sync-with-main merge) changes the baseline `_dev_phase` records. When the plan still claims the baseline it was dispatched on (the previous recorded baseline, or HEAD right before the hook), it is re-stamped to the new HEAD before the session launches. A claim the run never recorded is left alone, so the gate keeps its independent signal, and so is a plan whose HEAD the operator moved while the run was paused.

A landed write journals `plan-baseline-restamped` (`site`, `overwritten`, `baseline`). A plan that is missing, unreadable or unwritable journals `plan-baseline-restamp-skipped` with the reason, and the gate decides as before. In tickets mode the plan is the bound ticket's.

## The smart stall check

A dev or review session that stays silent for `limits.dev_stall_grace_s` reaches the stall gate. With `stall_check = "smart"` the gate reads the session's last turn from its own transcript (Claude Code's JSONL, Codex's rollout) and from what its Stop hook reported, and decides in this order:

1. A permission or quota prompt (a hook latch, or a `parked_prompt_patterns` match on the visible pane) parks the session, as upstream.
2. A usage-limit line on the pane parks it as usage-limited, as upstream.
3. A turn that ended on an API error is handled as a [turn error](#turn-errors) when `classify_turn_errors` is on.
4. A last message that asks a human a question (a line ending in `?`, an ask phrase such as "should I" or "please confirm", or an open `AskUserQuestion` call) parks the session. The parked evidence names the question.
5. Otherwise the session is nudged to continue in the same session. When a command or sub-agent it started is still pending (an open tool call, a Codex `wait_agent` that timed out, a Claude background task), the nudge names it and asks the session to wait for it in the foreground. Otherwise the nudge asks it to continue from where it stopped.

Claude Code's `idle_prompt` notification arrives about a minute after every turn. Upstream mapped it to a parked session; the smart check treats it as a turn end and parks only on a question. Nudges count against `limits.dev_stall_nudges_cap`, so a session that answers every nudge without finishing still ends `stalled`, and the engine retries it (or resumes it, below).

`turn_end_nudge_s` shortens the wait after a result-less turn that ended on a status report with nothing pending: the nudge comes after that many seconds instead of the full grace.

The question test is a heuristic over free text. It only ever chooses between parking (the upstream outcome) and one bounded nudge.

## Turn errors

With `classify_turn_errors = true`, a turn that ended on an API error is classified from the transcript (Claude: an `isApiErrorMessage` entry and its `error` code; Codex: a `task_complete` with an `error`) or from Claude's `StopFailure` hook when the profile registers it:

| Class       | Examples                                                                                      | What happens                                                                                                      |
| ----------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| transport   | `server_error`, `overloaded`, `stream disconnected before completion`, `exceeded retry limit` | Nudged in place `transport_nudges` times after a one-minute backoff, then the session ends with a transport fault |
| pool        | `rate_limit`, a 429 such as TeamRouter's "Local TeamRouter pool unavailable"                  | Ends at once with a pool fault, naming the return time when the error gives one                                   |
| environment | `failed to load workspace requirements`, an authentication failure                            | Ends at once with an environment fault: a nudge cannot fix the config                                             |
| usage       | `billing_error`, the CLI's own credit limit                                                   | Ends at once with the usage-limit reason                                                                          |

Codex runs no Stop hook on a turn that ends in error, so the heartbeat reads the rollout when it moved and decides such a turn after the backoff instead of after the stall grace. A turn-error session is never charged: the error ended the turn just now, and tokens spent before it cannot disprove it. Each one journals `turn-error` (`turn_error`, `source`, `detail`), and `session-end` carries `turn_error` (and `pool_return`). The pause reasons are in the [escalations guide](escalations-guide.md#turn-error-pauses).

To receive Claude's `StopFailure` hook, map it in the profile's `[hooks] events` (`StopFailure = "StopFailure"`). An upstream engine refuses a profile that maps it, so add it only once every engine that reads the profile runs this fork. The transcript read covers Claude without it.

## Nudges over hcom

A session launched through [hcom](https://github.com/aannoo/hcom) runs as a named instance in a room, and the bmad-loop hook relay reports that name (`HCOM_INSTANCE_NAME`) and room (`HCOM_DIR`) on every event. With `nudge_via = "hcom"`, every nudge (stall, stop, budget, contract, and the smart check's) is first sent as:

```text
hcom send @<name> --intent request --thread bmad-loop-<run id> --from bmad-loop --quiet -- <text>
```

with `HCOM_DIR` set to the session's room and the orchestrator's own hcom identity removed from the environment. It is sent only when `hcom list <name> --json` shows the instance `listening`. Anything else (no reported name, no `hcom` binary, another status, a failed send) falls back to tmux keystrokes, crumbed `nudge-hcom-fallback` with the reason in the task's `session-lifecycle.jsonl`. Nudge counting does not depend on the carrier. The fork never uses `hcom term inject`.

## Session resume

With `max_session_resumes > 0`, a dev or review retry continues the previous CLI conversation instead of starting a fresh session, when all of these hold:

- the role's previous session in this attempt chain ended `stalled`, `timeout` or `crashed` (including after a turn error), not parked on a human and not without work;
- it ran on the adapter the role runs now, its CLI session id is known, and its transcript still exists;
- the tree it left was kept: an in-run retry of such a session skips the rollback, and a restart that reset the tree (a rollback that undid changes, a discarded worktree) starts fresh;
- fewer than `max_session_resumes` resumes in a row.

A completed session whose product failed verify or review always starts fresh. The resumed session gets a plain-text continuation (never the skill command, which would restart the workflow): it names why the previous session ended, asks it to check the working tree first, and not to end its turn while work it started is pending.

A profile says how its CLI resumes:

```toml
# claude.toml: `claude --resume <id> "<prompt>" ...`
resume_flag = "--resume"

# codex.toml: `codex resume [launch_args] <id> "<prompt>" ...`
resume_subcommand = ["resume"]
```

`resume_prompt_template` (default `{prompt}`) renders the continuation. The keys are argv tokens, so they are part of the run's trusted launch digest. A resumed Claude session keeps its id, so hook attribution is pinned to it and no new `--session-id` is passed. A resumed conversation appends to the same transcript, so the session is charged only for usage past what the transcript held at launch.

Records, with resumes on: `session-bound` (the CLI session id at the first SessionStart), `session-resume` before a resumed launch (`session_id`, `ended`, `resumes`), and `resumed_from` on `session-end`. Session records in `state.json` gain `ended` and `resumed_from`; tasks gain `session_resumes` and `resume_blocked`.

## Policy reload at story boundaries

With `reload_policy = "story-boundary"`, the engine re-reads `policy.toml` and the profiles at the top of the story loop, where no session is live:

- a change limited to keys that do not reach the launch surface (limits, gates, review, model and effort, recovery, notify) is applied: the engine, its flows and its adapters take the new policy, `state.policy_snapshot` is re-stamped, and `policy-reloaded` lists the changed keys;
- a change to the launch surface (verify commands, the plugin allowlist, any role's binary, flags or environment: the run's trusted `config_digest`) or to a section that shapes the run (`[scm]`, `[plugins]`, `[mux]`, `[dev]`, `[tui]`) is journaled `policy-reload-deferred` with the reason and keys, once per distinct change, and waits for `bmad-loop resume`, which re-blesses it;
- an unreadable or invalid file is deferred the same way, and the run keeps its policy.

## Two audit fixes outside the table

- `[tickets] hitl = "defer"`: a hitl ticket first in build order no longer holds its epic's other ready tickets behind it. The pick passes it over (journaled as a `skipped` pick note, `hitl`), dispatches the next ready ticket of the same epic, and halts at `ticket-hitl` on it once nothing else of that epic can start. A later epic never starts ahead of it. The default `"halt"` is upstream's.
- `[limits] harvest_summary_chars` and `harvest_evidence_chars` (defaults 200 and 1000, upstream's fixed caps) set how much of a harvested deferred finding the deferred-work ledger keeps.

## Compatibility

- State: every new field loads with a default, so a run started by an upstream engine resumes here, and upstream ignores the new fields if it resumes a run this fork started.
- Policy: upstream ignores the `[recovery]` table, so a policy that sets it still loads there.
- Profiles: upstream ignores `resume_flag`, `resume_subcommand` and `resume_prompt_template`, but refuses a `StopFailure` event mapping (see [Turn errors](#turn-errors)).
- Journal: new kinds only; no existing kind changed shape, and `session-end` gains present-only fields.
