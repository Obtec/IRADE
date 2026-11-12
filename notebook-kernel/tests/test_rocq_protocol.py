"""Offline unit tests for the Rocq (Coq) XML protocol, tokenizer, InfoView, and mock driver."""

from __future__ import annotations

from pathlib import Path
import sys
import textwrap

import pytest

from rocq_kernel.driver import (
    RocqProcess,
    RocqProcessCrashedError,
    RocqTimeoutError,
)
from rocq_kernel.infoview import (
    extract_rocq_infoview_payload,
    render_rocq_infoview_html,
)
from rocq_kernel.kernel import check_rocq_completeness
from rocq_kernel.session import (
    RocqSession,
    extract_declared_name,
    tokenize_sentences,
)
from rocq_kernel.xml_protocol import (
    build_add_call,
    build_edit_at_call,
    build_goal_call,
    build_init_call,
    build_query_call,
    parse_xml_response,
)


def test_tokenize_sentences_comments_strings_and_dots() -> None:
    code = """
    (* Outer comment with a period.
       (* Nested comment with another period. *)
       Still inside comment. *)
    Definition greeting : string := "Hello. World.".
    Lemma add_0_r : forall n : nat, n + 0 = n.
    Proof.
      intros n.
      induction n as [| n' IHn].
      - simpl. reflexivity.
      - simpl. rewrite IHn. reflexivity.
    Qed.
    """
    spans = tokenize_sentences(code)
    sentences = [s.sentence for s in spans]
    assert sentences == [
        'Definition greeting : string := "Hello. World.".',
        "Lemma add_0_r : forall n : nat, n + 0 = n.",
        "Proof.",
        "intros n.",
        "induction n as [| n' IHn].",
        "- simpl.",
        "reflexivity.",
        "- simpl.",
        "rewrite IHn.",
        "reflexivity.",
        "Qed.",
    ]
    assert spans[0].start_line == 5
    assert spans[-1].sentence == "Qed."


def test_extract_declared_name() -> None:
    assert extract_declared_name("Definition plus2 (n : nat) := n + 2.") == "plus2"
    assert extract_declared_name("(* comment *) Lemma add_comm : True.") == "add_comm"
    assert extract_declared_name("Local Fixpoint fact (n : nat) : nat := 1.") == "fact"
    assert extract_declared_name("intros n.") is None


def test_xml_builders_and_parsers() -> None:
    assert build_init_call() == '<call val="Init"><option val="none"/></call>'
    add_xml = build_add_call("Lemma a : True.", edit_id=1, state_id=1)
    assert '<call val="Add">' in add_xml
    assert "<string>Lemma a : True.</string>" in add_xml
    assert '<state_id val="1"/>' in add_xml

    assert build_edit_at_call(3) == '<call val="Edit_at"><state_id val="3"/></call>'
    assert build_goal_call() == '<call val="Goal"><unit/></call>'
    q_xml = build_query_call("Check nat.", state_id=2)
    assert '<call val="Query">' in q_xml
    assert "<string>Check nat.</string>" in q_xml

    # Parse Init response
    init_res = parse_xml_response('<value val="good"><state_id val="1"/></value>')
    assert init_res.ok is True
    assert init_res.state_id == 1

    # Parse Goal response with both <hyp> and <richpp> formats + &nbsp;
    goal_xml = """
    <feedback object="state" route="0">
      <state_id val="3"/>
      <feedback_content val="message">
        <message>
          <message_level val="notice"/>
          <option val="none"/>
          <richpp><_><pp>nat&nbsp;:&nbsp;Set</pp></_></richpp>
        </message>
      </feedback_content>
    </feedback>
    <value val="good">
      <option val="some">
        <goals>
          <list>
            <goal>
              <string>2</string>
              <list>
                <hyp><list><string>n</string><string>m</string></list><option val="none"/><string>nat</string></hyp>
                <richpp><_><pp>IHn&nbsp;:&nbsp;n&nbsp;+&nbsp;0&nbsp;=&nbsp;n</pp></_></richpp>
              </list>
              <richpp><_><pp>n&nbsp;+&nbsp;m&nbsp;=&nbsp;m&nbsp;+&nbsp;n</pp></_></richpp>
            </goal>
          </list>
          <list/>
          <list>
            <goal>
              <string>99</string>
              <list/>
              <string>True</string>
            </goal>
          </list>
          <list/>
        </goals>
      </option>
    </value>
    """
    parsed = parse_xml_response(goal_xml)
    assert parsed.ok is True
    assert len(parsed.feedbacks) == 1
    assert parsed.feedbacks[0].message == "nat : Set"
    assert parsed.goals is not None
    assert len(parsed.goals.focused) == 1
    fg = parsed.goals.focused[0]
    assert fg.goal_id == "2"
    assert fg.conclusion == "n + m = m + n"
    assert len(fg.hypotheses) == 2
    assert fg.hypotheses[0].identifiers == ["n", "m"]
    assert fg.hypotheses[0].type_str == "nat"
    assert fg.hypotheses[1].identifiers == ["IHn"]
    assert len(parsed.goals.shelved) == 1

    # Parse failure response
    fail_xml = """
    <value val="fail">
      <state_id val="2"/>
      <option val="some"><loc line_nb="1" bol_pos="0" start="0" stop="7"/></option>
      <richpp><_><pp>The&nbsp;term&nbsp;"0"&nbsp;has&nbsp;type&nbsp;"nat".</pp></_></richpp>
    </value>
    """
    fail_res = parse_xml_response(fail_xml)
    assert fail_res.ok is False
    assert fail_res.state_id == 2
    assert 'The term "0" has type "nat".' in (fail_res.error_message or "")


def test_rocq_completeness_heuristics() -> None:
    assert check_rocq_completeness("")["status"] == "complete"
    assert check_rocq_completeness("%goals")["status"] == "complete"
    assert check_rocq_completeness("Lemma foo : True.")["status"] == "complete"
    assert check_rocq_completeness("Lemma foo : True")["status"] == "incomplete"
    assert check_rocq_completeness("(* unclosed comment.")["status"] == "incomplete"
    assert check_rocq_completeness('"unclosed string.')["status"] == "incomplete"


def test_mock_coqidetop_session_and_infoview_ssr(tmp_path: Path) -> None:
    mock_script = tmp_path / "mock_coqidetop.py"
    mock_script.write_text(
        textwrap.dedent(
            """\
            import sys

            state_counter = 1
            last_sent = ""

            for raw_line in sys.stdin:
                line = raw_line.strip()
                if not line:
                    continue
                if 'val="Init"' in line:
                    sys.stdout.write('<value val="good"><state_id val="1"/></value>\\n')
                    sys.stdout.flush()
                elif 'val="Add"' in line:
                    state_counter += 1
                    last_sent = line
                    sys.stdout.write(f'<value val="good"><pair><state_id val="{state_counter}"/><union val="in_l"><unit/></union></pair></value>\\n')
                    sys.stdout.flush()
                elif 'val="Goal"' in line:
                    if "exact 0." in last_sent:
                        sys.stdout.write('<value val="fail"><state_id val="2"/><richpp><_><pp>Type mismatch in exact 0</pp></_></richpp></value>\\n')
                    elif "Lemma" in last_sent or "intros" in last_sent:
                        sys.stdout.write(
                            '<value val="good"><option val="some"><goals>'
                            '<list><goal><string>1</string>'
                            '<list><richpp><_><pp>n : nat</pp></_></richpp></list>'
                            '<richpp><_><pp>n = n</pp></_></richpp>'
                            '</goal></list><list/><list/><list/>'
                            '</goals></option></value>\\n'
                        )
                    else:
                        sys.stdout.write('<value val="good"><option val="none"/></value>\\n')
                    sys.stdout.flush()
                elif 'val="Edit_at"' in line:
                    sys.stdout.write('<value val="good"><union val="in_l"><unit/></union></value>\\n')
                    sys.stdout.flush()
                elif 'val="Quit"' in line:
                    sys.stdout.write('<value val="good"><unit/></value>\\n')
                    sys.stdout.flush()
                    break
            """
        ),
        encoding="utf-8",
    )

    proc = RocqProcess(custom_command=[sys.executable, "-u", str(mock_script)])
    session = RocqSession(process=proc)
    try:
        out1 = session.execute_command("Lemma refl_n : forall n : nat, n = n. intros n.", cell_id="c1")
        assert out1.ok is True
        assert out1.goals is not None
        assert len(out1.goals.focused) == 1

        payload = extract_rocq_infoview_payload(out1)
        html_str = render_rocq_infoview_html(payload)
        assert "l4iv-wrap" in html_str
        assert "l4iv-turnstile" in html_str
        assert "n = n" in html_str

        # Failed tactic rolls back to state after c1
        saved_state = session.current_state_id
        out_err = session.execute_command("exact 0.", cell_id="c2")
        assert out_err.ok is False
        assert session.current_state_id == saved_state
        assert "Type mismatch" in (out_err.error_message or "")
    finally:
        session.close()


def test_driver_timeout_and_crash_handling(tmp_path: Path) -> None:
    hang_script = tmp_path / "hang_coqidetop.py"
    hang_script.write_text(
        textwrap.dedent(
            """\
            import sys, time
            for line in sys.stdin:
                if 'val="Init"' in line:
                    sys.stdout.write('<value val="good"><state_id val="1"/></value>\\n')
                    sys.stdout.flush()
                else:
                    time.sleep(5)
            """
        ),
        encoding="utf-8",
    )
    proc = RocqProcess(
        custom_command=[sys.executable, "-u", str(hang_script)],
        default_timeout=0.5,
    )
    try:
        assert proc.start() == 1
        with pytest.raises((RocqTimeoutError, RocqProcessCrashedError)):
            proc.send_call(build_goal_call(), timeout=0.15)
    finally:
        proc.kill()
