"""Command line entry point for the `lba` command."""

import typer

app = typer.Typer(help="Legacy Bank Agent: learn a task once, replay it without an LLM.")


def _todo(name: str) -> None:
    typer.echo(f"`lba {name}` is not implemented yet.")


@app.command()
def bank() -> None:
    """Start the fake legacy bank web app."""
    _todo("bank")


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
