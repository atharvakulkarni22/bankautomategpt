import importlib

import pytest
from typer.testing import CliRunner

import bag
from bag.cli import app

SUBPACKAGES = ["bankapp", "surface", "agent", "artifact", "replay", "safety", "handoff"]
COMMANDS = ["bank", "discover", "build", "replay", "approve", "resume", "list"]


def test_version():
    assert bag.__version__


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name):
    importlib.import_module(f"bag.{name}")


def test_cli_lists_all_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output
