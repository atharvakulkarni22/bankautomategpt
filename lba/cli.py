"""Command line entry point for the `lba` command."""

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


@app.command()
def discover(task: str = typer.Argument(..., help="Plain-English description of the task.")) -> None:
    """Let the agent learn a task and save it as an artifact."""
    _todo("discover")


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
