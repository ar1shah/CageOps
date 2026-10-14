"""Every trainer command written in the docs parses, so the runbook can't drift from the CLI."""

import re
import shlex
from datetime import date
from pathlib import Path

import pytest

from cageops_trainer.cli import PROG, build_parser

REPO = Path(__file__).parents[3]
PREFIX = f"uv run {PROG}"


def runbook_commands() -> list[str]:
    text = (REPO / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.S)
    lines = [line.strip() for block in blocks for line in block.splitlines()]
    return [line for line in lines if line.startswith(PREFIX)]


def test_the_runbook_documents_every_command():
    verbs = {shlex.split(c)[len(PREFIX.split())] for c in runbook_commands()}

    assert verbs == {"train", "list", "activate"}


@pytest.mark.parametrize("command", runbook_commands())
def test_each_documented_command_parses(command):
    argv = shlex.split(command)[len(PREFIX.split()) :]

    args = build_parser().parse_args(argv)  # a bad flag or verb exits with status 2

    assert args.command == argv[0]


def test_claude_md_lists_every_command():
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")

    verbs = set(re.findall(r"`uv run python -m cageops_trainer (\w+)", text))

    assert verbs == {"train", "list", "activate"}


def test_the_documented_flags_do_what_the_docs_say():
    parser = build_parser()

    args = parser.parse_args(
        ["train", "--seed", "7", "--test-start", "2025-10-04", "--val-start", "2024-10-04"]
    )

    assert args.seed == 7
    assert args.test_start == date(2025, 10, 4) and args.val_start == date(2024, 10, 4)
    assert args.train_start is None
    assert parser.parse_args(["activate", "20261014-153012"]).version == "20261014-153012"
    assert parser.parse_args(["list", "--json"]).json is True
