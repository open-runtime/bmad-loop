# Architecture decision records

Each record states one design decision: the problem, the options that were weighed, what was chosen, and why. Records are numbered and never renumbered. A superseded record stays in place and links to the record that replaces it.

| ADR                                           | Title                                                        | Status                                                                     |
| --------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------- |
| [0001](0001-mux-env-transport.md)             | Carrying the state root into session and parked-window panes | Accepted                                                                   |
| [0002](0002-native-sdlc-campaign.md)          | A deterministic SDLC campaign with GitHub delivery           | Implemented; rollout gates open                                            |
| [0003](0003-bounded-reasoning-and-handoff.md) | Bounded reasoning jobs and portable session handoff          | Implemented with opt-in prelaunch authentication; qualification gates open |

ADR 0002's [implementation contract](0002-campaign-implementation.md#implementation-goals-and-traceability)
tracks the shared G0–G8 requirements. Accepted design, implemented code,
normal-runtime coverage, native qualification, review readiness and project
activation are distinct milestones; a record's acceptance does not imply delivery.

The old REM pilot is retired; preserve its evidence and branches without resuming
or recreating it. First qualify hosted two-story delivery in a separate disposable
repository, with fresh REM activation later. Handoff, managed messaging, live
controls and load qualification remain separate gates; a narrow Claude/Grok
delivery does not establish general unattended readiness.
