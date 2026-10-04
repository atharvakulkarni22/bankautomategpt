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
def replay(artifact: str = typer.Argument(..., help="Name of the saved artifact.")) -> None:
    """Replay a saved artifact without an LLM."""
    _todo("replay")


@app.command()
def approve(run_id: str = typer.Argument(..., help="Run waiting for approval.")) -> None:
    """Approve a risky step that a run is paused on."""
    _todo("approve")


@app.command()
def resume(run_id: str = typer.Argument(..., help="Run to resume after human takeover.")) -> None:
    """Resume a run after a human has taken over."""
    _todo("resume")


@app.command("list")
def list_artifacts() -> None:
    """List saved artifacts."""
    _todo("list")


if __name__ == "__main__":
    app()
