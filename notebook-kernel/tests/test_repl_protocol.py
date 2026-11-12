"""Unit tests for REPL JSON framing, parser helpers, SSR InfoView, and offline session."""

from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from lean4_kernel.infoview import extract_infoview_payload, render_infoview_html
from lean4_kernel.magics import dispatch_magic
from lean4_kernel.repl import (
    REPLBinaryManager,
    REPLProcess,
    REPLProcessCrashedError,
    REPLTimeoutError,
    extract_version_tag,
    parse_repl_json_block,
)
from lean4_kernel.session import (
    LeanSession,
    extract_declared_names,
    format_tactic_input,
    starts_with_header_import,
    strip_leading_lean_comments,
)


def test_parse_repl_json_multiline() -> None:
    lines = [
        '{"messages":',
        ' [{"severity": "info",',
        '   "pos": {"line": 1, "column": 0},',
        '   "data": "42"}],',
        ' "env": 0}',
    ]
    parsed, aux = parse_repl_json_block(lines)
    assert aux == ""
    assert parsed is not None
    assert parsed["env"] == 0
    assert parsed["messages"][0]["data"] == "42"


def test_parse_repl_json_with_prefix_stdout() -> None:
    lines = ['raw output before json{"env": 3}']
    parsed, aux = parse_repl_json_block(lines)
    assert parsed == {"env": 3}
    assert aux == "raw output before json"


def test_strip_comments_and_import_detection() -> None:
    code = """
    -- leading line comment
    /- outer
       /- nested block comment -/
       still comment -/
    import Init.Data.List.Basic

    def x := 1
    """
    stripped = strip_leading_lean_comments(code)
    assert stripped.startswith("import Init.Data.List.Basic")
    assert starts_with_header_import(code) is True
    assert starts_with_header_import("-- only comment\ndef import_lookalike := 1") is False


def test_extract_declared_names() -> None:
    code = """
    def double (n : Nat) : Nat := n + n
    private lemma helper_lem : 1 = 1 := rfl
    noncomputable def α_val : Nat := 3
    @[simp] theorem main_thm : double 2 = 4 := rfl
    @[inline, simp] scoped def scoped_fn (x : Nat) := x
    local instance instInhabitedPoint : Inhabited Nat := ⟨0⟩
    structure Point where
      x : Nat
    """
    names = extract_declared_names(code)
    assert names == (
        "double",
        "helper_lem",
        "α_val",
        "main_thm",
        "scoped_fn",
        "instInhabitedPoint",
        "Point",
    )


def test_redeclared_identifier_extraction_unicode_quotes() -> None:
    assert (
        LeanSession._extract_redeclared_ident(
            {"messages": [{"severity": "error", "data": "`sq` has already been declared"}]}
        )
        == "sq"
    )
    assert (
        LeanSession._extract_redeclared_ident(
            {"messages": [{"severity": "error", "data": "'sq' has already been declared"}]}
        )
        == "sq"
    )
    assert (
        LeanSession._extract_redeclared_ident(
            {"messages": [{"severity": "error", "data": "‘sq’ has already been declared"}]}
        )
        == "sq"
    )
    assert (
        LeanSession._extract_redeclared_ident(
            {"messages": [{"severity": "error", "data": "“MyNS.sq” has already been declared"}]}
        )
        == "MyNS.sq"
    )


def test_format_tactic_input_single_and_multiline() -> None:
    assert format_tactic_input("  intro h  ") == "intro h"
    multi = format_tactic_input("  intro hp\n  exact hp\n")
    assert multi == "(\n  intro hp\n  exact hp\n)"


def test_extract_version_tag() -> None:
    assert extract_version_tag("leanprover/lean4:v4.35.0") == "v4.35.0"
    assert extract_version_tag("leanprover/lean4:4.29.0-rc1") == "v4.29.0-rc1"
    assert extract_version_tag("nightly-2026-01-01") is None


def test_infoview_ssr_fallback_without_js() -> None:
    source = "theorem demo (p q : Prop) : p → q → p := by\n  intro hp hq\n  exact hp"
    sample_infotree = [
        {
            "kind": "TacticInfo",
            "node": {
                "name": "Lean.Parser.Tactic.tacticIntro___",
                "goalsBefore": ["p q : Prop\n⊢ p → q → p"],
                "goalsAfter": ["p q : Prop\nhp : p\nhq : q\n⊢ p"],
                "stx": {
                    "kind": "Lean.Parser.Tactic.tacticIntro___",
                    "pp": "intro hp hq",
                    "range": {
                        "synthetic": False,
                        "start": {"line": 2, "column": 2},
                        "finish": {"line": 2, "column": 13},
                    },
                },
            },
            "children": [
                {
                    "kind": "TermInfo",
                    "node": {
                        "expr": "hp",
                        "expectedType": "p",
                        "isBinder": True,
                        "stx": {
                            "pp": "hp",
                            "range": {
                                "synthetic": False,
                                "start": {"line": 2, "column": 8},
                                "finish": {"line": 2, "column": 10},
                            },
                        },
                    },
                    "children": [],
                }
            ],
        },
        {
            "kind": "TacticInfo",
            "node": {
                "name": "Lean.Parser.Tactic.exact",
                "goalsBefore": ["p q : Prop\nhp : p\nhq : q\n⊢ p"],
                "goalsAfter": [],
                "stx": {
                    "kind": "Lean.Parser.Tactic.exact",
                    "pp": "exact hp",
                    "range": {
                        "synthetic": False,
                        "start": {"line": 3, "column": 2},
                        "finish": {"line": 3, "column": 10},
                    },
                },
            },
            "children": [],
        },
    ]

    payload = extract_infoview_payload(
        source,
        ok=True,
        env=1,
        proof_state=None,
        infotree=sample_infotree,
        messages=[{"severity": "info", "pos": {"line": 1, "column": 0}, "data": "demo : ∀ (p q : Prop), p → q → p"}],
        notes=["Sample note"],
    )
    assert payload.initial_line == 2
    assert len(payload.tactic_steps) == 2

    html_out = render_infoview_html(payload)
    # Verify SSR pre-populates the right-hand info pane before `<script>` even runs
    before_script = html_out.split("<script>", 1)[0]
    assert "l4iv-line l4iv-line-selected' data-line='2'" in before_script
    assert "Goals Before" in before_script
    assert "Goals After" in before_script
    assert "⊢" in before_script
    assert "p → q → p" in before_script
    assert "Term at Cursor" in before_script
    assert "Sample note" in before_script
    assert "overscroll-behavior: contain" in before_script
    assert "flex-shrink: 0" in before_script
    assert "data-action='expand'" in before_script


def test_offline_lean_session_with_mock_repl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_repl = tmp_path / "mock_session_repl.py"
    mock_repl.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import json, sys

            next_env = 0
            envs = {}
            buf = []
            for line in sys.stdin:
                if line.strip() == "":
                    if not buf:
                        continue
                    req = json.loads("".join(buf))
                    buf = []
                    if "tactic" in req:
                        tac = req["tactic"]
                        if "exact" in tac:
                            resp = {"proofState": 1, "goals": [], "proofStatus": "Completed"}
                        else:
                            resp = {"proofState": 1, "goals": ["P : Prop\\nh : P\\n⊢ P"], "proofStatus": "Incomplete"}
                        sys.stdout.write(json.dumps(resp) + "\\n\\n")
                        sys.stdout.flush()
                        continue

                    cmd = req.get("cmd", "")
                    parent = req.get("env")
                    inherited = set(envs.get(parent, set())) if parent is not None else set()

                    if "broken" in cmd:
                        resp = {
                            "messages": [{"severity": "error", "pos": {"line": 1, "column": 0}, "data": "type mismatch"}],
                            "env": parent if parent is not None else 0,
                        }
                    elif "def foo" in cmd and "foo" in inherited:
                        resp = {
                            "messages": [{"severity": "error", "pos": {"line": 1, "column": 0}, "data": "‘foo’ has already been declared"}],
                        }
                    elif "sorry" in cmd:
                        eid = next_env
                        next_env += 1
                        envs[eid] = inherited
                        resp = {
                            "env": eid,
                            "sorries": [{"pos": {"line": 1, "column": 30}, "goal": "P : Prop\\n⊢ P → P", "proofState": 0}],
                            "messages": [{"severity": "warning", "data": "declaration uses ‘sorry’"}],
                        }
                    else:
                        eid = next_env
                        next_env += 1
                        if "def foo" in cmd:
                            inherited.add("foo")
                        envs[eid] = inherited
                        resp = {"env": eid, "messages": [{"severity": "info", "data": "ok"}]}

                    sys.stdout.write(json.dumps(resp) + "\\n\\n")
                    sys.stdout.flush()
                else:
                    buf.append(line.strip())
            """
        ),
        encoding="utf-8",
    )
    mock_repl.chmod(0o755)
    monkeypatch.setenv("LEAN_REPL_BIN", str(mock_repl))

    session = LeanSession()
    try:
        # 1. Define `@[simp] def foo`
        out1 = session.execute_command("@[simp] def foo : Nat := 1")
        assert out1.ok and out1.env == 0
        assert "foo" in session.user_identifiers()

        # 2. Error rollback preserves current_env
        out_err = session.execute_command("def broken : Nat := true")
        assert not out_err.ok
        assert session.current_env == 0

        # 3. Redeclaration with Unicode smart quotes (`‘foo’ has already been declared`) auto-backtracks
        out_redef = session.execute_command("@[simp] def foo : Nat := 2")
        assert out_redef.ok
        assert out_redef.env == 1
        assert any("Redefined `foo`" in n for n in out_redef.notes)

        # 4. Theorem with `sorry` filters Unicode warning and opens proofState #0
        out_sorry = session.execute_command("theorem t (P : Prop) : P → P := by sorry")
        assert out_sorry.ok
        assert out_sorry.messages == []
        assert session.active_proof_state == 0

        # 5. Interactive tactic execution via magic commands
        m1 = dispatch_magic(session, "%tactic intro h")
        assert m1.ok and m1.mime_bundle is not None
        assert "⊢ P" in m1.mime_bundle["text/plain"]

        m2 = dispatch_magic(session, "%%tactic\nexact h")
        assert m2.ok and m2.mime_bundle is not None
        assert "Goals accomplished!" in m2.mime_bundle["text/plain"]
    finally:
        session.close()


def test_repl_process_timeout_and_crash_with_mock_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_repl = tmp_path / "mock_repl.py"
    mock_repl.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import json, sys, time

            buf = []
            for line in sys.stdin:
                if line.strip() == "":
                    if not buf:
                        continue
                    req = json.loads("".join(buf))
                    buf = []
                    cmd = req.get("cmd", "")
                    if cmd == "hang":
                        time.sleep(10)
                    elif cmd == "crash":
                        sys.stderr.write("fatal panic in mock repl\\n")
                        sys.stderr.flush()
                        sys.exit(1)
                    else:
                        sys.stdout.write(json.dumps({"env": 0, "messages": [{"severity": "info", "data": "ok"}], " cmd": cmd}, indent=1) + "\\n\\n")
                        sys.stdout.flush()
                else:
                    buf.append(line.strip())
            """
        ),
        encoding="utf-8",
    )
    mock_repl.chmod(0o755)
    monkeypatch.setenv("LEAN_REPL_BIN", str(mock_repl))

    proc = REPLProcess(binary_manager=REPLBinaryManager(), working_dir=tmp_path)
    try:
        res = proc.request({"cmd": "hello"}, timeout=5.0)
        assert res.data["env"] == 0

        with pytest.raises(REPLTimeoutError):
            proc.request({"cmd": "hang"}, timeout=0.2)
        assert not proc.is_alive

        # Automatically restarts on next request, then test crash diagnostics
        with pytest.raises(REPLProcessCrashedError, match="fatal panic in mock repl"):
            proc.request({"cmd": "crash"}, timeout=5.0)
    finally:
        proc.kill()
