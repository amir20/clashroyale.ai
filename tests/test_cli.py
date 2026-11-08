from pathlib import Path

import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

from crai.cli import app

group = typer.main.get_command(app)
assert isinstance(group, TyperGroup)  # typer 0.28 vendors click
COMMANDS = sorted(group.commands)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_builds(command: str) -> None:
    """typer resolves annotations lazily; --help forces every option type to be understood."""
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output


def test_stats_runs(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("CR_DB_PATH", str(db_path))
    result = CliRunner().invoke(app, ["stats", "--min-games", "1", "--sort", "adj_win_rate"])
    assert result.exit_code == 0, result.output
    assert "Hog Rider" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["schema"],
        ["schema", "battles", "--rows", "2"],
        ["schema", "--snapshot"],
        ["sql", "--snapshot", "SELECT count(*) AS n FROM decks"],
        ["sql", "SELECT count(*) AS n FROM battles"],
    ],
)
def test_schema_and_sql(monkeypatch: pytest.MonkeyPatch, db_path: Path, args: list[str]) -> None:
    monkeypatch.setenv("CR_DB_PATH", str(db_path))
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output


def test_schema_unknown_table(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("CR_DB_PATH", str(db_path))
    assert CliRunner().invoke(app, ["schema", "nope"]).exit_code != 0
