---
name: bmad-loop-campaign-plan
description: Run an explicitly mapped installed BMAD planning or planning-review workflow inside an unattended bmad-loop campaign stage. Adapt the installed v6 or v7 workflow to the approved stage packet and record unresolved product decisions without inventing commands or approval authority.
---

# Campaign planning

Use only inside a campaign session with `BMAD_LOOP_MODE=1` and an orchestrator
stage packet. Read the entire packet, its current GitHub inputs, prior artifacts,
approved brief, exclusions and result contract before writing.

Read the exact `Qualified native workflow` SKILL.md at its supplied path. Its
name, SHA256 and BMAD era identify the capability selected for this stage. Use
that mapped workflow, including its referenced steps, rather than guessing a
command from another BMAD version. Do not install, upgrade or modify skills.

Use the native workflow's documented unattended, YOLO or fast path when present.
Never invent CLI flags or claim that every BMAD version exposes the same mode.
When a workflow requests routine coaching input, apply the supplied approved
brief, authoritative owner notes, current repository evidence and prior stage
artifacts. Follow its substantive steps without asking routine questions.

When those inputs do not answer a material product or authority question, write
a durable decision artifact in this same draft PR and return `decisions` in the
structured result. Each entry has a unique `id`, a precise `question`, an
`evidence` array of source references already inspected, and a boolean
`blocking`. Mark unresolved questions that prevent sound planning as blocking.
Do not invent requirements, approvals, delegation or an owner answer. A blocking
decision stops stage advancement until the orchestrator resolves the hold.

Keep requirements, architecture and decomposition substantive and grounded in
existing issues, PRs, branches and work ownership. Update only selected product
artifacts in this owned checkout and preserve delivered work. Commit useful
progress in this same draft PR. Planning review is read-only and independent;
return an explicit verdict and findings covering the exact proposed graph.

Return the packet's exact structured schema, coordinates, artifact paths and
other stage-specific fields; include `decisions: []` when no question remains.
The result file is a proposal for deterministic validation, never permission to
publish, merge, waive gates or launch another agent.
