"""StepGate: unauthorised capabilities, permission escalation, argument smuggling, out-of-root paths,
and when approval is required (security table rows: escalation, path traversal, approval bypass)."""

from __future__ import annotations

import subprocess

import pytest

from tests.helpers import ScriptedProvider, SlowCapability, WriteNote, make_deps
from xodus.orchestrator.gate import GateContext, ProposedStep


@pytest.fixture
def deps(tmp_path, sandbox):
    return make_deps(tmp_path, sandbox, ScriptedProvider(), extra=[WriteNote("sandbox", sandbox), SlowCapability()])


def ctx(deps, tainted=False, allowed=None):
    return GateContext("t1", frozenset(allowed if allowed is not None else deps.registry.available_ids()), tainted)


def ev(deps, cap, args, **kw):
    return deps.gate.evaluate(ProposedStep(1, cap, args, "react"), ctx(deps, **kw))


@pytest.mark.parametrize("cap", ["fs.delete_all", "fs__list_files", "", None, 42, ["fs.list_files"], {"id": "x"},
                                 "FS.LIST_FILES", "fs.list_files ", "a" * 10_000])
def test_unknown_or_malformed_capability_ids_are_denied(deps, cap):
    decision = ev(deps, cap, {})
    assert decision.verdict == "deny" and decision.code == "unknown_capability"


def test_capability_not_offered_for_the_task_is_denied(deps):
    decision = ev(deps, "test.slow", {}, allowed={"fs.list_files"})
    assert decision.verdict == "deny" and decision.code == "not_offered"


def test_disabled_or_ungranted_capability_is_denied(tmp_path, sandbox):
    deps = make_deps(tmp_path, sandbox, ScriptedProvider(), extra=[WriteNote("sandbox", sandbox)], grants=("fs.read",))
    decision = deps.gate.evaluate(ProposedStep(1, "test.write_note", {"path": "n.txt", "text": "x"}, "react"),
                                  GateContext("t1", frozenset(deps.registry.ids())))
    assert decision.verdict == "deny" and decision.code == "unavailable"
    assert "fs.write" in decision.message


@pytest.mark.parametrize(
    "args, fragment",
    [
        ("path=..", "JSON object"),
        (["."], "JSON object"),
        (None, "JSON object"),
        ({"path": 5}, "path"),
        ({"path": ".", "root": "C:\\"}, "Additional properties"),
        ({"path": ".", "permissions": ["fs.write:sandbox"]}, "Additional properties"),
        ({"path": ".", "requires_approval": "never"}, "Additional properties"),
        ({"path": "x" * 400}, "path"),
    ],
)
def test_bad_or_smuggled_arguments_are_denied(deps, args, fragment):
    decision = ev(deps, "fs.list_files", args)
    assert decision.verdict == "deny" and decision.code == "invalid_arguments"
    assert fragment in decision.message


@pytest.mark.parametrize("path", ["..", "../outside", "sub/../../outside", "C:\\Windows", "\\\\srv\\share", "\\\\?\\C:\\",
                                  "a.txt:secret", "CON", "PROGRA~1", "sub.", "sub "])
def test_out_of_root_paths_are_denied_before_the_capability_runs(deps, path):
    decision = ev(deps, "fs.list_files", {"path": path})
    assert decision.verdict == "deny" and decision.code == "path_violation"


def test_write_targets_outside_root_are_denied(deps):
    for path in ("..\\outside\\x.txt", "C:\\Users\\x.txt", "sub\\..\\..\\x.txt"):
        decision = ev(deps, "test.write_note", {"path": path, "text": "x"})
        assert decision.verdict == "deny" and decision.code == "path_violation", path


def test_junction_in_write_target_is_denied(deps, sandbox, tmp_path):
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(sandbox / "jump"), str(tmp_path / "outside")], capture_output=True)
    if made.returncode != 0:
        pytest.skip("could not create a junction")
    decision = ev(deps, "test.write_note", {"path": "jump\\pwned.txt", "text": "x"})
    assert decision.verdict == "deny" and decision.code == "path_violation"


def test_read_only_step_is_allowed_with_resolved_target(deps, sandbox):
    decision = ev(deps, "fs.list_files", {"path": "sub"})
    assert decision.verdict == "allow"
    assert decision.targets == [str((sandbox / "sub").resolve())]


def test_default_path_value_is_also_confined(deps, sandbox):
    decision = ev(deps, "fs.list_files", {})
    assert decision.verdict == "allow" and decision.targets == [str(sandbox.resolve())]


def test_side_effect_needs_approval_with_bound_hash(deps):
    decision = ev(deps, "test.write_note", {"path": "note.txt", "text": "hi"})
    assert decision.verdict == "approve"
    assert "it changes files" in decision.reasons and "it writes outside the XODUS data folder" in decision.reasons
    other = ev(deps, "test.write_note", {"path": "note.txt", "text": "hi!"})
    assert other.action_hash != decision.action_hash


def test_untrusted_content_makes_any_side_effect_need_approval(deps):
    clean = ev(deps, "test.slow", {"seconds": 1})
    assert clean.verdict == "allow"
    tainted = ev(deps, "test.write_note", {"path": "note.txt", "text": "x"}, tainted=True)
    assert "the task has already read untrusted content" in tainted.reasons
    still_read_only = ev(deps, "fs.list_files", {"path": "."}, tainted=True)
    assert still_read_only.verdict == "allow"  # reading stays allowed; only effects need approval


def test_gate_does_not_mutate_or_trust_caller_arguments(deps):
    args = {"path": "sub"}
    decision = ev(deps, "fs.list_files", args)
    decision.args["path"] = ".."  # changing the decision's copy ...
    assert args == {"path": "sub"}  # ... never touches the caller's object
