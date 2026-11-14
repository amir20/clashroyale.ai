"""The `crai` command line. Importing the command modules registers them on `app`."""

import sys

from crai.cli import data_commands, ml_commands, question_commands, server_commands  # noqa: F401
from crai.cli.app import app


def main() -> None:
    sys.exit(app())
