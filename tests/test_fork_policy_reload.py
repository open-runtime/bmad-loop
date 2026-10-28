"""open-runtime fork F8: `[recovery] reload_policy = "story-boundary"` re-reads
policy.toml between stories and applies a change whose launch surface still
matches the run's trusted digest; anything else waits for a resume."""

from __future__ import annotations

from conftest import dev_effect, review_effect, write_sprint
from test_engine import make_engine

from bmad_loop import policy as policy_mod
from bmad_loop import runsetup
from bmad_loop.journal import load_state
from bmad_loop.runs import write_trusted_config_digest

BASE = """\
[gates]
mode = "none"

[notify]
desktop = false
file = true

[scm]
rollback_on_failure = true

[recovery]
reload_policy = "{reload}"
"""


def _setup(project, *, reload="story-boundary"):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    path = project.project / policy_mod.POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BASE.format(reload=reload), encoding="utf-8")
    policy = policy_mod.load(path)
    return path, policy


def _editing_dev(project, path, extra: str):
    """Story 1's dev session; it leaves `extra` appended to policy.toml, so the
    next story boundary sees the edit (as an operator's mid-run change would)."""
    inner = dev_effect(project, "1-1-a")

    def effect(spec):
        result = inner(spec)
        path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
        return result

    return effect


def _run(project, path, policy, extra):
    engine, adapter = make_engine(
        project,
        [
            _editing_dev(project, path, extra),
            review_effect(project, "1-1-a", clean=True),
            dev_effect(project, "1-2-b"),
            review_effect(project, "1-2-b", clean=True),
        ],
        policy=policy,
    )
    digest = runsetup.config_digest(
        policy, project.project, profiles=runsetup.resolve_profiles(policy, project.project)
    )
    write_trusted_config_digest(project.project, engine.run_dir.name, digest)
    return engine, adapter, engine.run()


def _rows(engine, kind):
    return [e for e in engine.journal.entries() if e["kind"] == kind]


def test_a_limits_and_model_change_applies_at_the_next_story(project):
    path, policy = _setup(project)
    engine, adapter, summary = _run(
        project,
        path,
        policy,
        '\n[limits]\nmax_review_cycles = 5\n\n[adapter.dev]\nmodel = "opus-x"\n',
    )

    assert summary.done == 2
    (row,) = _rows(engine, "policy-reloaded")
    assert row["keys"] == ["adapter.dev", "limits.max_review_cycles"]
    assert engine.policy.limits.max_review_cycles == 5
    dev_models = [s.model for s in adapter.sessions if s.role == "dev"]
    assert dev_models == ["", "opus-x"]
    snapshot = load_state(engine.run_dir).policy_snapshot
    assert snapshot["limits"]["max_review_cycles"] == 5


def test_a_launch_surface_change_waits_for_a_resume(project):
    path, policy = _setup(project)
    engine, adapter, summary = _run(project, path, policy, '\n[verify]\ncommands = ["make evil"]\n')

    assert summary.done == 2
    assert _rows(engine, "policy-reloaded") == []
    (row,) = _rows(engine, "policy-reload-deferred")
    assert (row["reason"], row["keys"]) == ("launch-surface", ["verify.commands"])
    assert engine.policy.verify.commands == ()


def test_a_frozen_section_change_waits_for_a_resume(project):
    path, policy = _setup(project)
    engine, _adapter, _summary = _run(
        project, path, policy, "\n[mux]\nhonor_ambient_psmux_data_dir = true\n"
    )

    (row,) = _rows(engine, "policy-reload-deferred")
    assert (row["reason"], row["keys"]) == ("frozen-section", ["mux.honor_ambient_psmux_data_dir"])
    assert engine.policy.mux.honor_ambient_psmux_data_dir is False


def test_reload_is_off_by_default(project):
    path, policy = _setup(project, reload="off")
    engine, adapter, summary = _run(project, path, policy, '\n[adapter.dev]\nmodel = "opus-x"\n')

    assert summary.done == 2
    assert _rows(engine, "policy-reloaded") == [] and _rows(engine, "policy-reload-deferred") == []
    assert [s.model for s in adapter.sessions if s.role == "dev"] == ["", ""]


def test_an_invalid_policy_file_is_deferred_once(project):
    path, policy = _setup(project)
    engine, _adapter, summary = _run(project, path, policy, '\n[limits]\nmax_dev_attempts = "x"\n')

    assert summary.done == 2
    rows = _rows(engine, "policy-reload-deferred")
    assert len(rows) == 1 and rows[0]["reason"].startswith("unreadable: PolicyError")


def test_a_story_source_change_waits_for_a_resume(project):
    """`[stories]` is frozen with the run state that froze the source at launch."""
    path, policy = _setup(project)
    engine, _adapter, _summary = _run(
        project, path, policy, '\n[stories]\nsource = "stories"\nspec_folder = "epic-1"\n'
    )

    (row,) = _rows(engine, "policy-reload-deferred")
    assert row["reason"] == "frozen-section"
    assert row["keys"] == ["stories.source", "stories.spec_folder"]
