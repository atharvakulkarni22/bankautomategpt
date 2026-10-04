"""Command line entry point for the `bag` command."""

import os
from pathlib import Path

import typer

app = typer.Typer(help="Bank Automate GPT: learn a task once, replay it without an LLM.")


def _todo(name: str) -> None:
    typer.echo(f"`bag {name}` is not implemented yet.")


@app.command()
def bank(port: int = typer.Option(5000, help="Port to listen on (host is always 127.0.0.1).")) -> None:
    """Start the fake legacy bank web app."""
    # Imported here so `bag --help` stays fast and works without Flask settings.
    from dotenv import load_dotenv

    from bag.bankapp.app import create_app

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


def _safety(config_path: Path, values):
    """Load the safety rules and build the guard and redactor every run uses.

    A missing or invalid config stops everything: with no rules the safe answer is "no".
    """
    from bag.safety import AuditLog, Guard, Redactor, SafetyConfigError, load_safety_config, terminal_approver

    try:
        config = load_safety_config(config_path)
    except SafetyConfigError as error:
        _fail(str(error))
    redactor = Redactor(values)
    guard = Guard(config, approver=terminal_approver, audit=AuditLog(config.audit_log, redactor), redactor=redactor)
    return config, guard, redactor


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
    safety_config: Path = typer.Option("config/safety.yaml", help="The safety rules."),
    takeover: bool = typer.Option(False, "--takeover", help="When the AI asks for help, let a person take the browser. Needs --headed."),
    interventions_dir: Path = typer.Option("evidence/interventions", help="Where takeover files are saved."),
    run_id: str = typer.Option(None, "--run-id", help="Name of this run's evidence folder (default: made from the time and the task)."),
    evidence_dir: Path = typer.Option("evidence", help="Where run folders are saved."),
) -> None:
    """Let the agent explore the bank app to reach a goal, and record what it did."""
    # Imported here so `bag --help` stays fast.
    from dotenv import load_dotenv

    from bag.agent import AgentLLM, Recorder, run_discovery
    from bag.handoff import HumanTakeover
    from bag.llm import LLMConfigError, get_client
    from bag.logging import RunLogError, RunLogger
    from bag.surface import BrowserSurface, Values

    if takeover and not headed:
        _fail("--takeover needs --headed: a person cannot work in a browser window they cannot see.")
    load_dotenv()
    values = Values(inputs=_parse_inputs(inputs))  # secrets come from .env, never from the command line
    start_url = start_url or os.environ.get("BANK_URL", "http://127.0.0.1:5000")
    config, guard, redactor = _safety(safety_config, values)
    try:
        client = get_client()
    except LLMConfigError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)

    try:
        run_log = RunLogger("discovery", goal, evidence_dir, run_id, redactor)
    except RunLogError as error:
        _fail(str(error))

    recorder = Recorder(goal, values.input_names, start_url, getattr(client, "model", "unknown"), output_dir, redactor)
    typer.echo(redactor.text(f"Goal: {goal}\nModel: {recorder.data['model']}\nRecording: {recorder.path}\n"))
    surface = BrowserSurface(
        headless=not headed, values=values,
        blur_screenshots=config.blur_screenshots, blur_selectors=config.blur_selectors,
    )
    with surface:
        handoff = (
            HumanTakeover(surface, directory=interventions_dir, redactor=redactor, run_log=run_log) if takeover else None
        )
        result = run_discovery(
            goal, surface, AgentLLM(client, use_screenshot=not no_screenshot), recorder, values, start_url,
            guard=guard, redactor=redactor, takeover=handoff, run_log=run_log, max_steps=max_steps,
            max_seconds=max_seconds,
        )

    for item in result.interventions:
        typer.echo(f"A human took over at step {item.step} ({item.status}, {item.event_count} action(s)): {item.path}")
    typer.echo(f"Stopped: {result.stop_reason} after {result.steps} step(s).")
    for name, value in result.outputs.items():
        typer.echo(f"  {name} = {redactor.text(value)}")
    if result.message:
        label = "Question for you" if result.stop_reason == "ask_human" else "Note"
        typer.echo(f"{label}: {redactor.text(result.message)}")
    typer.echo(f"Recording saved to {result.recording_path}")
    typer.echo(f"Evidence saved to {run_log.directory}")
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
    safety_config: Path = typer.Option("config/safety.yaml", help="The safety rules."),
    takeover: bool = typer.Option(False, "--takeover", help="When a step cannot be done, let a person take the browser. Needs --headed."),
    interventions_dir: Path = typer.Option("evidence/interventions", help="Where takeover files are saved."),
    run_id: str = typer.Option(None, "--run-id", help="Name of this run's evidence folder (default: made from the time and the task)."),
    evidence_dir: Path = typer.Option("evidence", help="Where run folders are saved."),
) -> None:
    """Replay an approved artifact with new inputs. No LLM is used.

    Exit code: 0 success, 1 failure or refused, 2 business outcome (e.g. NOT_FOUND).
    """
    # Nothing here touches bag.llm or bag.agent: replay must run with no LLM.
    from dotenv import load_dotenv

    from bag.artifact import ArtifactError, load_artifact, resolve_artifact_path
    from bag.handoff import HumanTakeover
    from bag.logging import RunLogError, RunLogger
    from bag.replay import BUSINESS_OUTCOME, SUCCESS, Replayer, ReplayRefused, prepare_run, resolve_start_url
    from bag.surface import BrowserSurface, Values

    if takeover and not headed:
        _fail("--takeover needs --headed: a person cannot work in a browser window they cannot see.")
    load_dotenv()
    try:
        loaded = load_artifact(resolve_artifact_path(artifact, artifacts_dir))
        clean = prepare_run(loaded, _parse_inputs(inputs), Values().secret_names)
    except (ArtifactError, ReplayRefused) as error:
        _fail(str(error))

    values = Values(inputs=clean)  # secrets come from .env; the checked inputs from the command line
    config, guard, redactor = _safety(safety_config, values)
    url = start_url or resolve_start_url(loaded.metadata.start_url, os.environ.get("BANK_URL"))
    meta = loaded.metadata
    try:
        run_log = RunLogger("replay", f"{meta.name}-v{meta.version}", evidence_dir, run_id, redactor)
    except RunLogError as error:
        _fail(str(error))
    typer.echo(f"Replaying {meta.name} v{meta.version} from {url}")
    surface = BrowserSurface(
        headless=not headed, timeout_ms=int(timeout * 1000), values=values,
        blur_screenshots=config.blur_screenshots, blur_selectors=config.blur_selectors,
    )
    with surface:
        handoff = (
            HumanTakeover(surface, directory=interventions_dir, redactor=redactor, run_log=run_log) if takeover else None
        )
        result = Replayer(
            loaded, surface, guard=guard, redactor=redactor, handoff=handoff, run_log=run_log, start_url=url,
            timeout_s=timeout,
        ).run()

    for entry in result.log:
        where = f"step {entry.step}" if entry.step else "run"
        typer.echo(f"  {where}: {entry.message}" if entry.kind != "fallback" else f"  {where}: FALLBACK {entry.message}")
    typer.echo(f"Evidence saved to {run_log.directory}")

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
    recording: Path = typer.Argument(..., exists=True, dir_okay=False, help="A recording from `bag discover`."),
    name: str = typer.Option(None, "--name", help="Artifact name (default: made from the goal)."),
    version: int = typer.Option(None, "--version", help="Version number (default: the next free one)."),
    app_name: str = typer.Option("First Legacy Bank", "--app", help="Name of the application."),
    inputs: list[str] = typer.Option([], "--input", help="Optional name=value. If that value appears in the recording, it is replaced by {{name}}."),
    artifacts_dir: Path = typer.Option("artifacts", help="Where artifacts are saved."),
) -> None:
    """Turn a discovery recording into a draft artifact (a reusable task)."""
    from dotenv import load_dotenv

    from bag.artifact import ArtifactError, BuildError, build_artifact, default_name, load_recording, next_version, save_artifact
    from bag.surface import Values

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
    typer.echo(f"Saved to {path}\nNext: read and edit it, then run `bag approve {artifact.metadata.name}`.")


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
    from bag.artifact import ArtifactError, approve_artifact, load_artifact, resolve_artifact_path

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
def resume(
    run_id: str = typer.Argument(None, help="Which paused run (an id or the start of one). Default: the one that is waiting."),
    interventions_dir: Path = typer.Option("evidence/interventions", help="Where takeover files are saved."),
) -> None:
    """Tell a paused run that the human has finished, so it carries on.

    Run this in a second terminal while `bag replay --takeover` (or `bag discover --takeover`)
    is waiting. Pressing Enter in the paused terminal does the same thing.
    """
    from bag.handoff import find_intervention, request_resume

    try:
        waiting = find_intervention(interventions_dir, run_id)
    except LookupError as error:
        _fail(str(error))
    request_resume(interventions_dir, waiting["id"])
    typer.echo(f"Resume requested for {waiting['id']} (step {waiting.get('step')}: {waiting.get('reason', '')[:80]}).")
    typer.echo("The paused run will carry on within a second or two.")


@app.command("list")
def list_artifacts(artifacts_dir: Path = typer.Option("artifacts", help="Where artifacts are saved.")) -> None:
    """List saved artifacts."""
    from bag.artifact import list_artifacts as find_artifacts

    entries = find_artifacts(artifacts_dir)
    if not entries:
        typer.echo(f"No artifacts in {artifacts_dir}. Build one with: bag build <recording>")
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
