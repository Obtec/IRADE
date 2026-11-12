"""XML protocol encoder and decoder for ``coqidetop`` (Rocq / Coq STM)."""

from __future__ import annotations

from dataclasses import dataclass, field
import html
import re
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class RocqHypothesis:
    """A single hypothesis in a Rocq proof context."""

    identifiers: list[str]
    value: str | None
    type_str: str
    raw: str = ""

    def format_line(self) -> str:
        if self.raw:
            return self.raw
        names = ", ".join(self.identifiers)
        if self.value is not None:
            return f"{names} := {self.value} : {self.type_str}"
        return f"{names} : {self.type_str}"


@dataclass(frozen=True)
class RocqGoal:
    """A single proof goal with its local context and conclusion."""

    goal_id: str
    hypotheses: list[RocqHypothesis]
    conclusion: str
    name: str | None = None

    def format_block(self) -> str:
        lines = [h.format_line() for h in self.hypotheses]
        lines.append(f"⊢ {self.conclusion}")
        return "\n".join(lines)


@dataclass(frozen=True)
class RocqGoalsBundle:
    """Complete set of goals returned by a ``Goal`` call."""

    focused: list[RocqGoal] = field(default_factory=list)
    background: list[RocqGoal] = field(default_factory=list)
    shelved: list[RocqGoal] = field(default_factory=list)
    admitted: list[RocqGoal] = field(default_factory=list)

    @property
    def is_proved(self) -> bool:
        return (
            not self.focused
            and not self.background
            and not self.shelved
            and not self.admitted
        )


@dataclass(frozen=True)
class RocqFeedback:
    """Diagnostic or notice message emitted via ``<feedback>``."""

    state_id: int | None
    severity: str  # "notice" | "info" | "warning" | "error"
    message: str
    loc: dict[str, int] | None = None


@dataclass
class RocqValue:
    """Decoded response from ``coqidetop`` for a single XML ``<call>``."""

    ok: bool
    state_id: int | None = None
    union_state_id: int | None = None
    goals: RocqGoalsBundle | None = None
    feedbacks: list[RocqFeedback] = field(default_factory=list)
    error_message: str | None = None
    error_loc: dict[str, int] | None = None
    raw_xml: str = ""


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_init_call(file_path: str | None = None) -> str:
    """Encode ``<call val="Init">`` handshake."""
    if file_path is None:
        return '<call val="Init"><option val="none"/></call>'
    return f'<call val="Init"><option val="some"><string>{_xml_escape(file_path)}</string></option></call>'


def build_add_call(
    sentence: str,
    edit_id: int,
    state_id: int,
    verbose: bool = True,
    bp: int = 0,
    line_nb: int = 1,
    bol_pos: int = 0,
) -> str:
    """Encode ``<call val="Add">`` for Coq 8.14+ / Rocq 9.x STM."""
    esc = _xml_escape(sentence)
    v_str = "true" if verbose else "false"
    return (
        '<call val="Add">'
        "<pair>"
        "<pair>"
        "<pair>"
        f"<pair><string>{esc}</string><int>{edit_id}</int></pair>"
        f'<pair><state_id val="{state_id}"/><bool val="{v_str}"/></pair>'
        "</pair>"
        f"<int>{bp}</int>"
        "</pair>"
        f"<pair><int>{line_nb}</int><int>{bol_pos}</int></pair>"
        "</pair>"
        "</call>"
    )


def build_edit_at_call(state_id: int) -> str:
    """Encode ``<call val="Edit_at">`` to backtrack to an earlier ``state_id``."""
    return f'<call val="Edit_at"><state_id val="{state_id}"/></call>'


def build_goal_call() -> str:
    """Encode ``<call val="Goal">`` to query active proof goals."""
    return '<call val="Goal"><unit/></call>'


def build_status_call(force: bool = False) -> str:
    """Encode ``<call val="Status">`` to force STM evaluation."""
    f_str = "true" if force else "false"
    return f'<call val="Status"><bool val="{f_str}"/></call>'


def build_query_call(query: str, state_id: int, route_id: int = 0) -> str:
    """Encode ``<call val="Query">`` for non-mutating ``Check`` / ``Print`` / ``Search``."""
    esc = _xml_escape(query)
    return (
        '<call val="Query">'
        "<pair>"
        f'<route_id val="{route_id}"/>'
        f'<pair><string>{esc}</string><state_id val="{state_id}"/></pair>'
        "</pair>"
        "</call>"
    )


def build_quit_call() -> str:
    """Encode ``<call val="Quit">``."""
    return '<call val="Quit"><unit/></call>'


def _extract_text(elem: ET.Element | None) -> str:
    """Recursively extract plain text from a ``<richpp>`` or ``<string>`` element."""
    if elem is None:
        return ""
    raw = "".join(elem.itertext())
    return html.unescape(raw).replace("\u00a0", " ").strip()


def _extract_state_id(elem: ET.Element) -> int | None:
    """Find first ``<state_id>`` or ``<stateid>`` child/descendant and return its int value."""
    if elem.tag in ("state_id", "stateid"):
        val = elem.attrib.get("val")
        if val is not None and val.lstrip("-").isdigit():
            return int(val)
    for child in elem.iter():
        if child.tag in ("state_id", "stateid"):
            val = child.attrib.get("val")
            if val is not None and val.lstrip("-").isdigit():
                return int(val)
    return None


def _extract_loc(elem: ET.Element) -> dict[str, int] | None:
    for loc_el in elem.iter("loc"):
        out: dict[str, int] = {}
        for k, v in loc_el.attrib.items():
            if v.lstrip("-").isdigit():
                out[k] = int(v)
        if out:
            return out
    return None


def _parse_hypothesis_str(raw_line: str) -> RocqHypothesis:
    """Parse a formatted hypothesis string like ``n, m : nat`` or ``k := 42 : nat``."""
    cleaned = " ".join(raw_line.split())
    if ":=" in cleaned:
        lhs, rhs = cleaned.split(":=", 1)
        names = [x.strip() for x in lhs.split(",") if x.strip()]
        if ":" in rhs:
            val_part, ty_part = rhs.rsplit(":", 1)
            return RocqHypothesis(
                identifiers=names,
                value=val_part.strip(),
                type_str=ty_part.strip(),
                raw=raw_line.strip(),
            )
    if ":" in cleaned:
        lhs, ty_part = cleaned.split(":", 1)
        names = [x.strip() for x in lhs.split(",") if x.strip()]
        return RocqHypothesis(
            identifiers=names,
            value=None,
            type_str=ty_part.strip(),
            raw=raw_line.strip(),
        )
    return RocqHypothesis(
        identifiers=[cleaned],
        value=None,
        type_str="",
        raw=raw_line.strip(),
    )


def _parse_hyp_element(elem: ET.Element) -> RocqHypothesis:
    """Parse either a structured ``<hyp>`` or a ``<richpp>``/``<string>`` hypothesis node."""
    if elem.tag == "hyp":
        children = list(elem)
        names: list[str] = []
        val_str: str | None = None
        ty_str = ""
        if len(children) >= 1:
            for s in children[0].findall("string"):
                t = _extract_text(s)
                if t:
                    names.append(t)
        if len(children) >= 2 and children[1].tag == "option":
            if children[1].attrib.get("val") == "some" and len(children[1]) > 0:
                val_str = _extract_text(children[1][0])
        if len(children) >= 3:
            ty_str = _extract_text(children[2])
        raw = f"{', '.join(names)} := {val_str} : {ty_str}" if val_str else f"{', '.join(names)} : {ty_str}"
        return RocqHypothesis(identifiers=names, value=val_str, type_str=ty_str, raw=raw)

    text = _extract_text(elem)
    return _parse_hypothesis_str(text)


def _parse_goal_element(goal_el: ET.Element) -> RocqGoal:
    children = list(goal_el)
    goal_id = _extract_text(children[0]) if len(children) >= 1 else "0"
    hyps: list[RocqHypothesis] = []
    if len(children) >= 2 and children[1].tag == "list":
        for hyp_el in children[1]:
            hyps.append(_parse_hyp_element(hyp_el))
    conclusion = _extract_text(children[2]) if len(children) >= 3 else ""
    goal_name: str | None = None
    if len(children) >= 4 and children[3].tag == "option":
        if children[3].attrib.get("val") == "some" and len(children[3]) > 0:
            goal_name = _extract_text(children[3][0])
    return RocqGoal(goal_id=goal_id, hypotheses=hyps, conclusion=conclusion, name=goal_name)


def _parse_goals_bundle(goals_el: ET.Element) -> RocqGoalsBundle:
    lists = [c for c in goals_el if c.tag == "list"]
    focused: list[RocqGoal] = []
    background: list[RocqGoal] = []
    shelved: list[RocqGoal] = []
    admitted: list[RocqGoal] = []

    if len(lists) >= 1:
        for g in lists[0].findall("goal"):
            focused.append(_parse_goal_element(g))
    if len(lists) >= 2:
        # Background goals can be nested inside pairs of before/after goal lists
        for g in lists[1].iter("goal"):
            background.append(_parse_goal_element(g))
    if len(lists) >= 3:
        for g in lists[2].findall("goal"):
            shelved.append(_parse_goal_element(g))
    if len(lists) >= 4:
        for g in lists[3].findall("goal"):
            admitted.append(_parse_goal_element(g))

    return RocqGoalsBundle(
        focused=focused,
        background=background,
        shelved=shelved,
        admitted=admitted,
    )


def _sanitize_xml_stream(raw: str) -> str:
    """Replace HTML entities like ``&nbsp;`` emitted by ``coqidetop`` so ElementTree can parse."""
    sanitized = raw.replace("&nbsp;", "&#160;")
    # Strip control characters that are invalid in XML 1.0 (except \t, \n, \r)
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", sanitized)
    return f"<rocq_root>{sanitized}</rocq_root>"


def parse_xml_response(xml_data: str | bytes) -> RocqValue:
    """Decode a stream of ``<feedback>`` and ``<value>`` elements from ``coqidetop``."""
    raw_str = xml_data.decode("utf-8", errors="replace") if isinstance(xml_data, bytes) else xml_data
    wrapped = _sanitize_xml_stream(raw_str)

    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError as exc:
        return RocqValue(
            ok=False,
            error_message=f"XML Parse Error: {exc}",
            raw_xml=raw_str,
        )

    feedbacks: list[RocqFeedback] = []
    for fb_el in root.findall("feedback"):
        fb_sid = _extract_state_id(fb_el)
        for content_el in fb_el.findall("feedback_content"):
            if content_el.attrib.get("val") == "message":
                msg_el = content_el.find("message")
                if msg_el is not None:
                    lvl_el = msg_el.find("message_level")
                    severity = lvl_el.attrib.get("val", "notice") if lvl_el is not None else "notice"
                    rpp_el = msg_el.find("richpp")
                    if rpp_el is None:
                        rpp_el = msg_el.find("string")
                    msg_text = _extract_text(rpp_el)
                    loc = _extract_loc(msg_el)
                    if msg_text:
                        feedbacks.append(
                            RocqFeedback(
                                state_id=fb_sid,
                                severity=severity,
                                message=msg_text,
                                loc=loc,
                            )
                        )

    val_elements = root.findall("value")
    if not val_elements:
        return RocqValue(
            ok=True,
            feedbacks=feedbacks,
            raw_xml=raw_str,
        )

    val_el = val_elements[-1]
    status = val_el.attrib.get("val", "good")

    if status == "fail":
        fail_sid = _extract_state_id(val_el)
        fail_loc = _extract_loc(val_el)
        rpp_el = val_el.find("richpp")
        if rpp_el is None:
            rpp_el = val_el.find("string")
        err_text = _extract_text(rpp_el) if rpp_el is not None else _extract_text(val_el)
        if not err_text:
            for fb in reversed(feedbacks):
                if fb.severity == "error" and fb.message:
                    err_text = fb.message
                    break
        return RocqValue(
            ok=False,
            state_id=fail_sid,
            feedbacks=feedbacks,
            error_message=err_text or "Rocq command failed",
            error_loc=fail_loc,
            raw_xml=raw_str,
        )

    # status == "good"
    state_id = _extract_state_id(val_el)
    union_sid: int | None = None
    union_el = val_el.find(".//union")
    if union_el is not None and union_el.attrib.get("val") == "in_r":
        union_sid = _extract_state_id(union_el)

    goals_bundle: RocqGoalsBundle | None = None
    goals_el = val_el.find(".//goals")
    if goals_el is not None:
        goals_bundle = _parse_goals_bundle(goals_el)

    return RocqValue(
        ok=True,
        state_id=state_id,
        union_state_id=union_sid,
        goals=goals_bundle,
        feedbacks=feedbacks,
        raw_xml=raw_str,
    )
