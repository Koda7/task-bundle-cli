"""LLM-as-judge failure analysis (the ``--analyze`` flag).

For a run that did NOT resolve the task, this asks a model to read the agent's
trajectory and the per-test outcomes, then classify *why* it failed into the
exact taxonomy from the SWE-Bench Pro paper (Appendix B). This turns a bare
"unresolved" into an actionable post-mortem an expert can act on.

It only makes sense for agent runs (a stub/golden failure means the bundle is
broken, not the model), and it is strictly best-effort: any error returns None
and never fails the run.
"""

from __future__ import annotations

import json
from typing import Optional

from task_bundle.bundle import Bundle

# The categories are taken verbatim from the paper's Appendix B judge prompt.
FAILURE_CATEGORIES = [
    "identified_incorrect_file",
    "missed_edge_case",
    "misunderstood_problem_statement",
    "wrong_solution",
    "tool_error",
    "infinite_loop",
    "endless_file_reading",
    "context_overflow_from_listing",
    "syntax_error",
    "other",
]

_JUDGE_SYSTEM = (
    "You are an expert software engineer analyzing why a coding agent failed to "
    "resolve an issue. Read the problem, the agent's trajectory, and which tests "
    "still fail. Give a one-paragraph explanation, then choose EXACTLY one "
    "category from this set:\n"
    + ", ".join(FAILURE_CATEGORIES)
    + "\nDo not invent categories. If none fits, use 'other'."
)


def analyze_failure(
    bundle: Bundle,
    solver_result,
    graded,
    *,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Return {'category', 'reasoning'} or None if analysis can't run."""
    try:
        from openai import OpenAI
    except ImportError:
        return None

    # Summarize the trajectory compactly (the judge doesn't need full tool output).
    traj_lines = []
    for ev in solver_result.transcript[-30:]:
        if ev.get("event") == "tool":
            traj_lines.append(f"- tool {ev.get('name')} {json.dumps(ev.get('args', {}))[:160]}")
        elif ev.get("event") == "assistant" and ev.get("text"):
            traj_lines.append(f"- assistant: {ev['text'][:200]}")
        elif ev.get("event") in {"stop", "api_error"}:
            traj_lines.append(f"- {ev.get('event')}: {ev.get('reason') or ev.get('error')}")
    trajectory = "\n".join(traj_lines) or "(no trajectory recorded)"

    failing = [
        tid for tid, st in graded.fail_to_pass.items() if st.value != "passed"
    ] + [tid for tid, st in graded.pass_to_pass.items() if st.value != "passed"]

    description = (
        bundle.read_description() if bundle.description_path.is_file() else ""
    )[:4000]

    user = (
        f"PROBLEM STATEMENT:\n{description}\n\n"
        f"AGENT TRAJECTORY (last steps):\n{trajectory}\n\n"
        f"TESTS STILL FAILING ({len(failing)}):\n" + "\n".join(failing[:20]) + "\n\n"
        "Respond as JSON: {\"reasoning\": \"...\", \"category\": \"<one_category>\"}."
    )

    client = OpenAI()
    try:
        resp = client.chat.completions.create(
            model=model or "gpt-4o",
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return None

    category = str(data.get("category", "other")).strip().lower()
    if category not in FAILURE_CATEGORIES:
        category = "other"
    return {"category": category, "reasoning": str(data.get("reasoning", "")).strip()}
