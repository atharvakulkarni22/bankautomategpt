"""Command line entry point for the `lba` command."""

import os
from pathlib import Path

import typer

app = typer.Typer(help="Legacy Bank Agent: learn a task once, replay it without an LLM.")


def _todo(name: str) -> None:
    typer.echo(f"`lba {name}` is not implemented yet.")


@app.command()
def bank(port: int = typer.Option(5000, help="Port to listen on (host is always 127.0.0.1).")) -> None:
    """Start the fake legacy bank web app."""
    # Imported here so `lba --help` stays fast and works without Flask settings.
    from dotenv import load_dotenv

    from lba.bankapp.app import create_app

    load_dotenv()  # reads BANK_USER, BANK_PASSWORD and fault switches from .env
    try:
        bank_app = create_app()
    except RuntimeError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)
    # threaded=True lets the page and its iframe load at the same time,
    # which matters when the "slow" fault is on.
    bank_app.run(host="127.0.0.1", port=port, threaded=True)


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    """Turn ["member_id=12345", ...] into {"member_id": "12345"}."""
    inputs = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")  # split at the FIRST "=", so values may contain "="
        if not sep or not name.isidentifier():
            raise typer.BadParameter(f"'{pair}' is not name=value (name: letters, digits, underscore).")
        inputs[name] = value
    return inputs


@app.command()
def discover(
    goal: str = typer.Option(..., "--goal", help="What the agent should achieve, in plain English."),
    inputs: list[str] = typer.Option([], "--input", help="An input as name=value. Repeat for several."),
    start_url: str = typer.Option(None, "--start-url", help="Where to begin (default: BANK_URL)."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    max_steps: int = typer.Option(25, help="Stop after this many steps."),
    max_seconds: int = typer.Option(180, help="Stop after this many seconds."),
    no_screenshot: bool = typer.Option(False, "--no-screenshot", help="Do not send screenshots (for text-only models)."),
    output_dir: Path = typer.Option("evidence/recordings", help="Where the recording is saved."),
) -> None:
    """Let the agent explore the bank app to reach a goal, and record what it did."""
    # Imported here so `lba --help` stays fast.
    from dotenv import load_dotenv

    from lba.agent import AgentLLM, Recorder, run_discovery
    from lba.llm import LLMConfigError, get_client
    from lba.surface import BrowserSurface, Values

    load_dotenv()
    values = Values(inputs=_parse_inputs(inputs))  # secrets come from .env, never from the command line
    start_url = start_url or os.environ.get("BANK_URL", "http://127.0.0.1:5000")
    try:
        client = get_client()
    except LLMConfigError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)

    recorder = Recorder(goal, values.input_names, start_url, getattr(client, "model", "unknown"), output_dir)
    typer.echo(f"Goal: {goal}\nModel: {recorder.data['model']}\nRecording: {recorder.path}\n")
    with BrowserSurface(headless=not headed, values=values) as surface:
        result = run_discovery(
            goal, surface, AgentLLM(client, use_screenshot=not no_screenshot), recorder, values, start_url,
            max_steps=max_steps, max_seconds=max_seconds,
        )

    typer.echo(f"Stopped: {result.stop_reason} after {result.steps} step(s).")
    for name, value in result.outputs.items():
        typer.echo(f"  {name} = {value}")
    if result.message:
        label = "Question for you" if result.stop_reason == "ask_human" else "Note"
        typer.echo(f"{label}: {result.message}")
    typer.echo(f"Recording saved to {result.recording_path}")
    if result.stop_reason != "done":
        raise typer.Exit(1)


@app.command()
def replay(
    artifact: str = typer.Argument(..., help="Artifact name (latest version), name.vN, or a file path."),
    inputs: list[str] = typer.Option([], "--input", help="An input as name=value. Repeat for several."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    start_url: str = typer.Option(None, "--start-url", help="Where to begin (default: the artifact's, on BANK_URL's host)."),
    timeout: float = typer.Option(10.0, help="Seconds to wait for each element or expected state."),
    artifacts_dir: Path = typer.Option("artifacts", help="Where artifacts are saved."),
    screenshot_dir: Path = typer.Option("evidence/screenshots", help="Where a failure screenshot is saved."),
) -> None:
    """Replay an approved artifact with new inputs. No LLM is used.

    Exit code: 0 success, 1 failure or refused, 2 business outcome (e.g. NOT_FOUND).
    """
    # Nothing here touches lba.llm or lba.agent: replay must run with no LLM.
    from dotenv import load_dotenv

    from lba.artifact import ArtifactError, load_artifact, resolve_artifact_path
    from lba.replay import BUSINESS_OUTCOME, SUCCESS, Replayer, ReplayRefused, prepare_run, resolve_start_url
    from lba.surface import BrowserSurface, Values

    load_dotenv()
    try:
        loaded = load_artifact(resolve_artifact_path(artifact, artifacts_dir))
        clean = prepare_run(loaded, _parse_inputs(inputs), Values().secret_names)
    except (ArtifactError, ReplayRefused) as error:
        _fail(str(error))

    values = Values(inputs=clean)  # secrets come from .env; the checked inputs from the command line
    url = start_url or resolve_start_url(loaded.metadata.start_url, os.environ.get("BANK_URL"))
    meta = loaded.metadata
    typer.echo(f"Replaying {meta.name} v{meta.version} from {url}")
    with BrowserSurface(headless=not headed, timeout_ms=int(timeout * 1000), values=values) as surface:
        result = Replayer(loaded, surface, start_url=url, timeout_s=timeout, screenshot_dir=screenshot_dir).run()

    for entry in result.log:
        where = f"step {entry.step}" if entry.step else "run"
        typer.echo(f"  {where}: {entry.message}" if entry.kind != "fallback" else f"  {where}: FALLBACK {entry.message}")

    if result.status == SUCCESS:
        typer.echo(f"SUCCESS in {result.seconds:.1f}s")
        for name, value in result.outputs.items():
            typer.echo(f"  {name} = {value}")
        return
    if result.status == BUSINESS_OUTCOME:
        typer.echo(f"BUSINESS OUTCOME: {result.outcome_code} ({result.message})")
        raise typer.Exit(2)
    failure = result.failure
    where = f"step {failure.step} ({failure.action})" if failure.step else f"the {failure.phase} of the run"
    typer.echo(f"FAILURE at {where}: {failure.error}\n  {failure.message}", err=True)
    if failure.expected:
        typer.echo(f"  expected: {failure.expected}", err=True)
    if failure.observed:
        typer.echo(f"  observed: {failure.observed}", err=True)
    if failure.screenshot:
        typer.echo(f"  screenshot: {failure.screenshot}", err=True)
    raise typer.Exit(1)


def _fail(message: str):
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


@app.command()
def build(
    recording: Path = typer.Argument(..., exists=True, dir_okay=False, help="A recording from `lba discover`."),
    name: str = typer.Option(None, "--name", help="Artifact name (default: made from the goal)."),
    version: int = typer.Option(None, "--version", help="Version number (default: the next free one)."),
    app_name: str = typer.Option("First Legacy Bank", "--app", help="Name of the application."),
    inputs: list[str] = typer.Option([], "--input", help="Optional name=value. If that value appears in the recording, it is replaced by {{name}}."),
    artifacts_dir: Path = typer.Option("artifacts", help="Where artifacts are saved."),
) -> None:
    """Turn a discovery recording into a draft artifact (a reusable task)."""
    from dotenv import load_dotenv

    from lba.artifact import ArtifactError, BuildError, build_artifact, default_name, load_recording, next_version, save_artifact
    from lba.surface import Values

    load_dotenv()  # so secrets from .env are recognised and kept out of the file
    try:
        data = load_recording(recording)
        name = name or default_name(data)
        version = version or next_version(artifacts_dir, name)
        result = build_artifact(
            data, name=name, version=version, app=app_name,
            values=Values(inputs=_parse_inputs(inputs)), source=recording.name,
        )
        path = save_artifact(result.artifact, artifacts_dir, values=Values())
    except (BuildError, ArtifactError) as error:
        _fail(str(error))

    artifact = result.artifact
    typer.echo(f"Built {artifact.metadata.name} v{artifact.metadata.version} (draft): {len(artifact.steps)} step(s), "
               f"{len(artifact.inputs)} input(s), {len(artifact.outputs)} output(s).")
    for warning in result.warnings:
        typer.echo(f"  Warning: {warning}")
    typer.echo(f"Saved to {path}\nNext: read and edit it, then run `lba approve {artifact.metadata.name}`.")


def _locator_line(locator) -> str:
    more = f" (+{len(locator.fallbacks)} fallback)" if locator.fallbacks else ""
    return f"{locator.primary}{more}"


@app.command()
def approve(
    artifact: str = typer.Argument(..., help="Artifact name (latest version), name.vN, or a file path."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation question."),
    artifacts_dir: Path = typer.Option("artifacts", help="Where artifacts are saved."),
) -> None:
    """Review a draft artifact and mark it approved, so it may be replayed."""
    from lba.artifact import ArtifactError, approve_artifact, load_artifact, resolve_artifact_path

    try:
        path = resolve_artifact_path(artifact, artifacts_dir)
        loaded = load_artifact(path)
    except ArtifactError as error:
        _fail(str(error))

    meta = loaded.metadata
    if meta.status == "approved":
        typer.echo(f"{meta.name} v{meta.version} is already approved.")
        return

    # Show what a reviewer needs to see before saying yes.
    typer.echo(f"{meta.name} v{meta.version}: {meta.description}\nFile: {path}\n")
    typer.echo(f"Inputs:  {', '.join(f'{i.name} ({i.type})' for i in loaded.inputs) or 'none'}")
    typer.echo(f"Outputs: {', '.join(f'{o.name} ({o.type})' for o in loaded.outputs) or 'none'}")
    typer.echo("Steps:")
    for number, step in enumerate(loaded.steps, start=1):
        extra = f" text={step.text!r}" if step.text is not None else ""
        typer.echo(f"  {number}. {step.action} {_locator_line(step.locator)}{extra}")
    for outcome in loaded.known_outcomes:
        typer.echo(f"Outcome:      '{outcome.text}' -> {outcome.outcome}")
    for interruption in loaded.known_interruptions:
        typer.echo(f"Interruption: '{interruption.text}' -> {interruption.action} {_locator_line(interruption.locator)}")

    if not yes and not typer.confirm("\nApprove this artifact for replay?"):
        typer.echo("Not approved. Nothing changed.")
        raise typer.Exit(1)
    try:
        approve_artifact(path)
    except ArtifactError as error:
        _fail(str(error))
    typer.echo(f"Approved {meta.name} v{meta.version}.")


@app.command()
def resume(run_id: str = typer.Argument(..., help="Run to resume after human takeover.")) -> None:
    """Resume a run after a human has taken over."""
    _todo("resume")


@app.command("list")
def list_artifacts(artifacts_dir: Path = typer.Option("artifacts", help="Where artifacts are saved.")) -> None:
    """List saved artifacts."""
    from lba.artifact import list_artifacts as find_artifacts

    entries = find_artifacts(artifacts_dir)
    if not entries:
        typer.echo(f"No artifacts in {artifacts_dir}. Build one with: lba build <recording>")
        return
    typer.echo(f"{'NAME':<28}{'VER':<5}{'STATUS':<10}{'STEPS':<7}{'INPUTS':<20}DESCRIPTION")
    for entry in entries:
        if entry.error:
            typer.echo(f"{entry.path.name:<28}INVALID: {entry.error.splitlines()[0]}")
            continue
        meta = entry.artifact.metadata
        inputs = ",".join(i.name for i in entry.artifact.inputs) or "-"
        typer.echo(f"{meta.name:<28}{meta.version:<5}{meta.status:<10}{len(entry.artifact.steps):<7}{inputs:<20}{meta.description[:50]}")


if __name__ == "__main__":
    app()
