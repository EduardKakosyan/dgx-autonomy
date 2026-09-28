"""Checkpoints: continuity across fresh conversations.

OpenHands manages context inside one conversation (its condenser summarizes old
events). Some situations need a fresh conversation instead: the SDK's stuck detector
fired, the conversation keeps erroring, its context is nearly full anyway, the same
checks keep failing, or the builder declared itself blocked. The controller then
rolls the run over to a new conversation:

    1. ask the old conversation for a handoff (the write_handoff tool); the tool's
       answer carries validation problems back so the agent can fix them
    2. record a checkpoint: the validated handoff, or, if none arrives in time, a
       controller fallback built from the last valid checkpoint and what the
       controller can observe itself. A fallback never invents a completion boundary.
    3. pause the old conversation; start the new one with a recovery context: the
       frozen brief, the checkpoint (labelled as the builder's claims), the results
       the environment verified itself, recent failures, and selected evidence,
       within a budget

A handoff is validated for structure and for its evidence references (project
paths that exist, evaluations that exist), not for the truth of its claims. Claims
and verified results are kept in separate fields: the handoff may not carry a
`verified` section of its own.

Blocked. A builder that finds no viable path calls declare_blocked with the missing
capability and the alternatives it tried. The first such declaration is not
final: a fresh conversation reviews it (rollover reason `blocked-review`). Only a
declaration confirmed by that fresh conversation ends the run as `blocked`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent_files import AgentFileError, open_dir

ROADMAP_STATUSES = ("done", "in_progress", "todo", "blocked")
ATTEMPT_OUTCOMES = ("worked", "failed", "abandoned")
PROJECT_PREFIX = "/workspace/project/"
_EVAL_REF = re.compile(r"^eval:(\d{1,6})$")
_HANDOFF_FIELDS = frozenset(
    {"request_id", "summary", "roadmap", "decisions", "attempts", "open_failures", "next_steps",
     "notes"}
)  # fmt: skip
MAX_ITEMS = 60
MAX_TEXT = 2000
MAX_SHORT = 400

# Why a conversation was replaced; the recovery context words each differently.
REASONS = ("stuck", "errors", "context", "failures", "blocked-review", "forced")


@dataclass(frozen=True)
class EvidenceIndex:
    """What a handoff may point at: project paths that exist, evaluations that exist."""

    path_exists: Callable[[str], bool]
    evaluations: frozenset[int] = frozenset()


def project_path_exists(project: Path, rel: str) -> bool:
    """Whether `project/rel` exists, without following any symlink below `project`."""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return False
    try:
        with open_dir(project, *parts[:-1]) as fd:
            os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
    except (OSError, AgentFileError):
        return False
    return True


def normalize_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith(PROJECT_PREFIX):
        ref = ref[len(PROJECT_PREFIX) :]
    return ref.removeprefix("./")


# --- validating a handoff ------------------------------------------------------------


def _text(value: object, where: str, problems: list[str], *, limit: int, required: bool) -> str:
    if value is None or value == "":
        if required:
            problems.append(f"{where} is required")
        return ""
    if not isinstance(value, str):
        problems.append(f"{where} must be text")
        return ""
    if len(value) > limit:
        problems.append(f"{where} is longer than {limit} characters")
        return value[:limit]
    return value.strip()


def _items(value: object, where: str, problems: list[str], *, required: bool) -> list[Any]:
    if value is None:
        if required:
            problems.append(f"{where} is required (a list)")
        return []
    if not isinstance(value, list):
        problems.append(f"{where} must be a list")
        return []
    if required and not value:
        problems.append(f"{where} must not be empty")
    if len(value) > MAX_ITEMS:
        problems.append(f"{where} has more than {MAX_ITEMS} entries")
        return value[:MAX_ITEMS]
    return value


def _refs(value: object, where: str, index: EvidenceIndex, problems: list[str]) -> list[str]:
    refs = []
    for raw in _items(value, where, problems, required=False)[:10]:
        if not isinstance(raw, str) or not raw.strip():
            problems.append(f"{where}: every reference must be non-empty text")
            continue
        ref = normalize_ref(raw)
        m = _EVAL_REF.match(ref)
        if m:
            if int(m.group(1)) not in index.evaluations:
                problems.append(f"{where}: there is no evaluation #{m.group(1)} ({raw!r})")
                continue
        elif ref.startswith("/") or not index.path_exists(ref):
            problems.append(
                f"{where}: {raw!r} does not exist in the project (give a path relative to"
                f" {PROJECT_PREFIX}, or eval:N for an evaluation)"
            )
            continue
        refs.append(ref)
    return refs


def validate_handoff(
    raw: object, index: EvidenceIndex, *, request_id: str
) -> tuple[dict[str, Any], list[str]]:
    """The handoff normalized, and the problems that make it unusable (empty: valid)."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["the handoff must be an object"]
    unknown = sorted(set(raw) - _HANDOFF_FIELDS)
    if unknown:
        extra = (
            "; the environment records verified results itself"
            if any("verif" in k for k in unknown)
            else ""
        )
        problems.append(f"unknown fields {', '.join(unknown)}{extra}")
    if raw.get("request_id") != request_id:
        problems.append(
            f"request_id must be {request_id!r} (the id in the handoff request), got"
            f" {raw.get('request_id')!r}"
        )
    out: dict[str, Any] = {
        "summary": _text(raw.get("summary"), "summary", problems, limit=MAX_TEXT, required=True)
    }
    roadmap = []
    for i, item in enumerate(_items(raw.get("roadmap"), "roadmap", problems, required=True)):
        where = f"roadmap[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object with item, status, evidence")
            continue
        status = item.get("status")
        if status not in ROADMAP_STATUSES:
            problems.append(f"{where}.status must be one of {', '.join(ROADMAP_STATUSES)}")
        evidence = _refs(item.get("evidence"), f"{where}.evidence", index, problems)
        if status == "done" and not evidence:
            problems.append(
                f"{where} is done but names no evidence (a file in the project, or eval:N)"
            )
        roadmap.append(
            {
                "item": _text(item.get("item"), f"{where}.item", problems, limit=MAX_SHORT,
                              required=True),
                "status": status,
                "evidence": evidence,
            }
        )  # fmt: skip
    out["roadmap"] = roadmap
    out["decisions"] = [
        {
            "decision": _text(d.get("decision"), f"decisions[{i}].decision", problems,
                              limit=MAX_SHORT, required=True),
            "why": _text(d.get("why"), f"decisions[{i}].why", problems, limit=MAX_SHORT,
                         required=False),
        }
        for i, d in enumerate(_items(raw.get("decisions"), "decisions", problems, required=False))
        if _is_obj(d, f"decisions[{i}]", problems)
    ]  # fmt: skip
    attempts = []
    for i, a in enumerate(_items(raw.get("attempts"), "attempts", problems, required=False)):
        if not _is_obj(a, f"attempts[{i}]", problems):
            continue
        if a.get("outcome") not in ATTEMPT_OUTCOMES:
            problems.append(f"attempts[{i}].outcome must be one of {', '.join(ATTEMPT_OUTCOMES)}")
        attempts.append(
            {
                "approach": _text(a.get("approach"), f"attempts[{i}].approach", problems,
                                  limit=MAX_SHORT, required=True),
                "outcome": a.get("outcome"),
                "notes": _text(a.get("notes"), f"attempts[{i}].notes", problems,
                               limit=MAX_SHORT, required=False),
            }
        )  # fmt: skip
    out["attempts"] = attempts
    out["open_failures"] = [
        {
            "what": _text(f.get("what"), f"open_failures[{i}].what", problems, limit=MAX_SHORT,
                          required=True),
            "detail": _text(f.get("detail"), f"open_failures[{i}].detail", problems,
                            limit=MAX_TEXT, required=False),
        }
        for i, f in enumerate(
            _items(raw.get("open_failures"), "open_failures", problems, required=False)
        )
        if _is_obj(f, f"open_failures[{i}]", problems)
    ]  # fmt: skip
    out["next_steps"] = [
        _text(s, f"next_steps[{i}]", problems, limit=MAX_SHORT, required=True)
        for i, s in enumerate(_items(raw.get("next_steps"), "next_steps", problems, required=True))
    ]
    out["notes"] = _text(raw.get("notes"), "notes", problems, limit=MAX_TEXT, required=False)
    return out, problems


def _is_obj(value: object, where: str, problems: list[str]) -> bool:
    if isinstance(value, dict):
        return True
    problems.append(f"{where} must be an object")
    return False


# --- blocked -------------------------------------------------------------------------


def validate_blocker(raw: object) -> tuple[dict[str, Any], list[str]]:
    """A blocker names a concrete missing capability and at least two alternatives."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["the declaration must be an object"]
    missing = _text(
        raw.get("missing_capability"), "missing_capability", problems, limit=MAX_TEXT,
        required=True,
    )  # fmt: skip
    if missing and len(missing) < 15:
        problems.append("missing_capability must say concretely what is missing")
    tried = [
        _text(a, f"alternatives_tried[{i}]", problems, limit=MAX_SHORT, required=True)
        for i, a in enumerate(
            _items(raw.get("alternatives_tried"), "alternatives_tried", problems, required=True)
        )
    ]
    if len([t for t in tried if t]) < 2:
        problems.append("alternatives_tried must list at least two different approaches you tried")
    needed = _text(raw.get("needed"), "needed", problems, limit=MAX_TEXT, required=True)
    return {"missing_capability": missing, "alternatives_tried": tried, "needed": needed}, problems


# --- the fallback ----------------------------------------------------------------------


def fallback_handoff(
    previous: Mapping[str, Any] | None,
    *,
    why: str,
    recent_activity: Sequence[str],
) -> dict[str, Any]:
    """What the controller records when the agent wrote no usable handoff.

    It carries the last valid handoff forward, marked as older, and adds only what
    the controller observed. It never marks anything done that was not before.
    """
    return {
        "summary": (
            f"No handoff from the previous conversation ({why}). The environment recorded"
            " this checkpoint from what it could observe; inspect the project before"
            " trusting anything below."
        ),
        "previous_handoff": dict(previous) if previous else None,
        "recent_activity": list(recent_activity),
    }


# --- the recovery context ----------------------------------------------------------------


@dataclass
class Section:
    title: str
    body: str
    # Trimmed in descending order: higher numbers are clipped (and dropped, if
    # droppable) first. No section is clipped below `keep` characters.
    priority: int
    keep: int
    droppable: bool = False
    pointer: str = ""  # where the full text is, if it gets clipped

    def render(self) -> str:
        return f"--- {self.title} ---\n{self.body.rstrip()}\n"


@dataclass(frozen=True)
class RecoveryInputs:
    run_id: str
    n: int  # the new conversation's number
    reason: str
    detail: str | None
    now: datetime
    deadline: datetime
    brief: str
    checkpoint_source: str
    handoff: Mapping[str, Any]
    verified: Mapping[str, Any]
    demo: str
    recent_failures: str = ""
    recent_activity: Sequence[str] = field(default_factory=list)
    blocker: Mapping[str, Any] | None = None
    # The operator's feedback so far (the `feedback` op), oldest first.
    operator_feedback: str = ""


_REASON_TEXT = {
    "stuck": (
        "the environment detected that it was stuck, repeating the same actions without progress"
    ),
    "errors": "it kept failing with errors",
    "context": "its context was nearly full",
    "failures": "the same acceptance checks kept failing across several attempts",
    "blocked-review": "it declared itself blocked, and a fresh look is required first",
    "forced": "the operator replaced it",
}


def _handoff_text(h: Mapping[str, Any]) -> str:
    lines = []
    if h.get("summary"):
        lines.append(str(h["summary"]))
    roadmap = h.get("roadmap") or []
    if roadmap:
        lines.append("\nRoadmap (claimed):")
        for r in roadmap:
            ev = f"  [evidence: {', '.join(r['evidence'])}]" if r.get("evidence") else ""
            lines.append(f"- [{r.get('status')}] {r.get('item')}{ev}")
    if h.get("decisions"):
        lines.append("\nDecisions:")
        lines += [
            f"- {d['decision']}" + (f" (why: {d['why']})" if d.get("why") else "")
            for d in h["decisions"]
        ]
    if h.get("attempts"):
        lines.append("\nApproaches tried:")
        lines += [
            f"- {a['approach']}: {a['outcome']}" + (f" ({a['notes']})" if a.get("notes") else "")
            for a in h["attempts"]
        ]
    if h.get("open_failures"):
        lines.append("\nOpen failures:")
        lines += [
            f"- {f['what']}" + (f": {f['detail']}" if f.get("detail") else "")
            for f in h["open_failures"]
        ]
    if h.get("next_steps"):
        lines.append("\nNext steps:")
        lines += [f"- {s}" for s in h["next_steps"]]
    if h.get("notes"):
        lines.append(f"\nNotes: {h['notes']}")
    previous = h.get("previous_handoff")
    if previous:
        lines.append("\nThe last handoff before that (older; the work may have moved on):")
        lines.append(_handoff_text(previous))
    if h.get("recent_activity"):
        lines.append("\nThe previous conversation's last actions, as the environment saw them:")
        lines += [f"- {a}" for a in h["recent_activity"]]
    return "\n".join(lines)


def _verified_text(v: Mapping[str, Any]) -> str:
    ev = v.get("latest_evaluation")
    if not ev:
        return "No acceptance evaluation has run yet."
    snapshot = str(ev.get("snapshot"))[:12]
    lines = [f"Evaluation #{ev['n']} ({ev['status']}) at project snapshot {snapshot}:"]
    for key, r in (ev.get("results") or {}).items():
        summary = f" ({r['summary']})" if r.get("summary") else ""
        lines.append(f"- {key}: {r.get('status')}{summary}")
    return "\n".join(lines)


def assemble_recovery_context(inputs: RecoveryInputs, budget_chars: int) -> str:
    """The new conversation's first message, within `budget_chars`.

    Lower-priority sections are clipped, then dropped, to fit; the header, the
    instructions and the start of the brief always stay.
    """
    remaining = inputs.deadline - inputs.now
    hours = max(0.0, remaining.total_seconds() / 3600)
    why = _REASON_TEXT.get(inputs.reason, inputs.reason)
    header = (
        f"You are continuing an unattended run in a fresh conversation (#{inputs.n}). The"
        f" previous conversation was replaced because {why}"
        + (f": {inputs.detail}" if inputs.detail else "")
        + ".\nNothing from it is in your context except what follows. Nobody will answer"
        f" questions. The deadline is {inputs.deadline.isoformat()} ({hours:.1f} h left).\n"
        "Your project is /workspace/project, with its files as the previous conversation"
        " left them. Terminal sessions and processes it started may still be running;"
        " check (ps, ss -ltnp) before starting servers again.\n"
        f"The demo: {inputs.demo}\n"
    )
    instructions = (
        "How to continue:\n"
        "- The checkpoint below is the previous conversation's own account: claims, not"
        " facts. Check the project's actual state before relying on it.\n"
        "- Results under VERIFIED were established by the environment itself.\n"
        "- Do not redo work that is really done. Where an approach failed, choose a"
        " different one rather than repeating it.\n"
        "- The brief and the acceptance checks are read-only in /brief. Finish when the"
        " brief is complete and you have verified it; the environment then runs the checks.\n"
    )
    if inputs.reason in ("stuck", "failures", "errors"):
        instructions += (
            "- Diagnose first: work out why the previous conversation made no progress"
            " before acting, and change approach.\n"
        )
    sections = [
        Section("BRIEF (the agreement; also at /brief/brief.md)", inputs.brief, 2, 600,
                pointer="the full brief is at /brief/brief.md"),
    ]  # fmt: skip
    if inputs.operator_feedback:
        instructions += (
            "- The operator reviewed the app and sent it back with the feedback below. It is"
            " product direction from the person who accepts the work: the app must meet it,"
            " and the brief and the checks still stand.\n"
        )
        sections.append(
            Section("OPERATOR FEEDBACK (oldest first)", inputs.operator_feedback, 0, 6000)
        )
    if inputs.blocker:
        tried = "\n".join(f"- {a}" for a in inputs.blocker.get("alternatives_tried") or [])
        sections.append(
            Section(
                "BLOCKER TO REVIEW",
                "The previous conversation declared that it cannot go on:\n"
                f"Missing: {inputs.blocker.get('missing_capability')}\n"
                f"Tried:\n{tried}\nNeeded: {inputs.blocker.get('needed')}\n"
                "Review this with fresh eyes. Verify that the blocker is real and try at least"
                " one approach not listed above. Only if you confirm it, call declare_blocked"
                " yourself; the run then ends as blocked.",
                0,
                4000,
            )
        )
    source = (
        "written by the previous conversation (its claims)"
        if inputs.checkpoint_source == "agent_handoff"
        else "recorded by the environment: the previous conversation wrote no usable handoff"
    )
    sections += [
        Section(f"CHECKPOINT ({source})", _handoff_text(inputs.handoff), 1, 1500),
        Section("VERIFIED BY THE ENVIRONMENT", _verified_text(inputs.verified), 1, 600),
    ]
    if inputs.recent_failures:
        sections.append(Section("RECENT FAILURES", inputs.recent_failures, 3, 400, droppable=True))
    if inputs.recent_activity:
        sections.append(
            Section(
                "THE PREVIOUS CONVERSATION'S LAST ACTIONS",
                "\n".join(f"- {a}" for a in inputs.recent_activity),
                4,
                0,
                droppable=True,
            )
        )
    return _fit(header + "\n" + instructions, sections, budget_chars)


def _fit(head: str, sections: list[Section], budget: int) -> str:
    for s in sections:
        s.body = s.body.rstrip()

    def total() -> int:
        return len(head) + sum(len(s.render()) + 1 for s in sections)

    for s in sorted(sections, key=lambda s: -s.priority):
        over = total() - budget
        if over <= 0:
            break
        note = f"\n… (clipped to fit{'; ' + s.pointer if s.pointer else ''})"
        target = max(s.keep, len(s.body) - over)
        if target < len(s.body):
            s.body = s.body[: max(0, target - len(note))] + note
        if total() > budget and s.droppable:
            sections.remove(s)
    return head + "\n" + "\n".join(s.render() for s in sections)


# --- what the old conversation is asked ----------------------------------------------


def handoff_request(request_id: str, reason: str, detail: str | None) -> str:
    why = _REASON_TEXT.get(reason, reason)
    return (
        f"HANDOFF REQUEST {request_id}. This conversation is about to be replaced by a"
        f" fresh one because {why}" + (f" ({detail})" if detail else "") + ". Stop the"
        " current task now. Call the write_handoff tool once, with request_id"
        f" {request_id!r}: summarize where the work stands, the roadmap with each item's"
        " status and evidence (paths of project files, or eval:N), the decisions, the"
        " approaches tried and how they went, the open failures, and the next steps. If"
        " the tool reports problems, fix them and call it again. Then finish with the"
        " message 'handoff written'. Your next action must be that write_handoff call:"
        " do not run another command first, and do not finish the task you were on."
        " Unfinished work belongs in next_steps, for the fresh conversation."
    )


def handoff_request_prefix(request_id: str, n: int) -> str:
    """How the n-th message asking for handoff `request_id` begins."""
    return (
        f"HANDOFF REQUEST {request_id}."
        if n <= 1
        else f"HANDOFF REQUEST {request_id} (reminder {n - 1})."
    )


def handoff_reminder(request_id: str, n: int, actions: int) -> str:
    return (
        f"HANDOFF REQUEST {request_id} (reminder {n - 1}). Since this conversation was"
        f" asked for its handoff it has taken {actions} more actions and has not called"
        " write_handoff. It will be replaced whether or not you write one; without it,"
        " the next conversation starts from a summary the environment writes itself and"
        " loses your plan. Stop now. Your next action must be write_handoff with"
        f" request_id {request_id!r}. Put the unfinished work in next_steps."
    )
