"""Turning a discovery recording into an artifact.

A recording is the raw diary of one discovery run, mistakes included. The builder
keeps only what worked, tidies it, and adds sensible defaults for a human to edit.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from lba.surface.placeholders import PLACEHOLDER, Values
from lba.surface.target import Target

from .schema import Artifact, Expected, Input, Interruption, Locator, Metadata, Outcome, Output, Step, SuccessCheck

STEP_ACTIONS = {"click", "type", "read", "wait"}  # "done" and "ask_human" only end a run
MONEY = re.compile(r"[-+]?[$€£]?\s?(\d{1,3}(,\d{3})+(\.\d+)?|\d+\.\d+)")


class BuildError(ValueError):
    """The recording cannot be turned into an artifact."""


@dataclass
class BuildResult:
    artifact: Artifact
    warnings: list[str] = field(default_factory=list)  # things a human should look at


def load_recording(path) -> dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise BuildError(f"Cannot read {path}: {error.strerror or error}") from error
    except json.JSONDecodeError as error:
        raise BuildError(f"{path.name} is not valid JSON: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or "goal" not in data:
        raise BuildError(f"{path.name} does not look like a discovery recording.")
    return data


def default_name(recording: dict) -> str:
    """A file-safe name made from the goal, e.g. 'log-in-and-find-the-savings-balance'."""
    slug = re.sub(r"[^a-z0-9]+", "-", recording["goal"].lower()).strip("-")[:40].strip("-")
    return slug or "task"


# ------------------------------------------------------------------- helpers


def _rank(target: Target) -> int:
    """Lower is better: role+name, then label, then text, then a bare role, then css."""
    if target.role is not None:
        return 0 if target.name else 3
    if target.label is not None:
        return 1
    if target.text is not None:
        return 2
    return 4


def _locator(action_target: Target, candidates: list[dict], why: str) -> Locator:
    """Merge the target the agent used with the recorded fallbacks, best first."""
    targets = []
    for raw in candidates:
        try:
            targets.append(Target.model_validate(raw))
        except ValidationError:
            continue  # a hand-edited recording with a broken candidate: skip just that one
    targets.append(action_target)
    ordered = sorted(dict.fromkeys(targets), key=_rank)  # dict.fromkeys drops repeats, keeps order; sort is stable
    return Locator(primary=ordered[0], fallbacks=ordered[1:], why=why)


def _expected_after(this_url: str, next_url: str | None) -> Expected | None:
    """If the page address changed, expect the new path.

    The path is cut at the first part containing a digit, so an id from the
    recording (like /member/1001) never ends up in the artifact: /member.
    """
    if not next_url:
        return None
    before, after = urlparse(this_url).path, urlparse(next_url).path
    if before == after:
        return None
    kept = []
    for part in after.split("/"):
        if re.search(r"\d", part):
            break
        kept.append(part)
    path = "/".join(kept) or "/"
    return Expected(url_contains=path if path.startswith("/") else "/" + path)


def _infer_type(value: str) -> str:
    return "decimal" if MONEY.fullmatch(value.strip()) else "string"


def default_interruptions() -> list[Interruption]:
    return [
        Interruption(
            text="System maintenance notice",
            action="click",
            locator=Locator(
                primary=Target(role="button", name="OK"),
                fallbacks=[Target(text="OK", exact=True)],
                why="Default: the maintenance popup blocks the page until OK is pressed. Edit as needed.",
            ),
        )
    ]


def default_outcomes() -> list[Outcome]:
    return [
        Outcome(
            text="No member found",
            outcome="NOT_FOUND",
            description="Default: the search found no member with that id. Edit as needed.",
        )
    ]


# ------------------------------------------------------------------- builder


def build_artifact(
    recording: dict,
    *,
    name: str | None = None,
    version: int = 1,
    app: str = "First Legacy Bank",
    values: Values | None = None,
    source: str | None = None,
) -> BuildResult:
    """Convert a recording (already loaded as a dict) into a draft Artifact.

    `values` holds real input values and secrets. Any of them found in the text
    is turned back into {{name}} / {{secret:NAME}}, so none can end up in the file.
    """
    values = values or Values()
    warnings: list[str] = []
    steps_in = recording["steps"]
    recorded_outputs = recording.get("outputs") or {}

    if recording.get("stop_reason") != "done":
        warnings.append(
            f"The recording stopped with '{recording.get('stop_reason')}', not 'done', so the artifact may be incomplete."
        )

    steps: list[Step] = []
    skipped = collapsed = 0
    output_info: dict[str, dict] = {}  # output name -> {why, type}
    for index, recorded in enumerate(steps_in):
        action = recorded.get("action")
        if recorded.get("status") != "ok":
            skipped += 1  # a failed, blocked or invalid attempt
            continue
        if not action or action.get("action") not in STEP_ACTIONS:
            continue  # "done" and "ask_human" just end the run

        why = values.protect(recorded.get("reason") or action.get("reason") or "")
        locator = _locator(Target.model_validate(action["target"]), recorded.get("target_candidates") or [], why)
        text = values.protect(action["text"]) if action["action"] == "type" else None

        # Typing the same text into the same field twice is pointless: keep one.
        if steps and action["action"] == "type" and steps[-1].action == "type" \
                and steps[-1].locator.primary == locator.primary and steps[-1].text == text:
            collapsed += 1
            continue

        next_url = steps_in[index + 1].get("url") if index + 1 < len(steps_in) else None
        step = Step(
            action=action["action"],
            locator=locator,
            text=text,
            output_name=action.get("output_name") if action["action"] == "read" else None,
            expected=_expected_after(recorded.get("url") or "", next_url),
        )
        steps.append(step)

        if step.action == "read":
            output_info.setdefault(step.output_name, {"why": why, "type": _infer_type(str(recorded_outputs.get(step.output_name, "")))})
            value = str(recorded_outputs.get(step.output_name, ""))
            primary = step.locator.primary
            if primary.text and value and (primary.text.lower() in value.lower() or value.lower() in primary.text.lower()):
                warnings.append(
                    f"Output '{step.output_name}' is located by its own value as text. "
                    "Replaying with different inputs will not find it; give it a label or role instead."
                )

    if not steps:
        raise BuildError("The recording has no successful steps to build from.")
    if skipped:
        warnings.append(f"Left out {skipped} step(s) that failed or were invalid.")
    if collapsed:
        warnings.append(f"Merged {collapsed} repeated 'type' step(s) into one.")
    if not output_info:
        warnings.append("No step reads a value, so the artifact has no outputs.")

    # Inputs: everything the recording declared plus every {{placeholder}} the steps use.
    input_names = list(recording.get("inputs") or [])
    for step in steps:
        for match in PLACEHOLDER.finditer(step.text or ""):
            if not match.group(1) and match.group(2) not in input_names:
                input_names.append(match.group(2))

    outputs = [
        Output(name=n, type=info["type"], description=info["why"]) for n, info in output_info.items()
    ]
    try:
        artifact = Artifact(
            metadata=Metadata(
                name=name or default_name(recording),
                version=version,
                app=app,
                description=values.protect(recording["goal"]),
                status="draft",
                start_url=recording.get("start_url") or "/",
                source_recording=source,
            ),
            inputs=[Input(name=n, description=f"Describe this input ({n}). Edit me.") for n in input_names],
            steps=steps,
            outputs=outputs,
            success_check=SuccessCheck(outputs_present=[o.name for o in outputs]),
            known_outcomes=default_outcomes(),
            known_interruptions=default_interruptions(),
        )
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()[:5])
        raise BuildError(f"The recording does not make a valid artifact: {problems}") from error
    return BuildResult(artifact, warnings)
