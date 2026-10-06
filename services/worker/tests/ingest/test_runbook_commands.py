"""Every ingestion command written in docs/RUNBOOK.md must parse with the real argument parser, so a
typo'd command or flag in the document fails CI instead of failing the person following it."""

import re
import shlex
from pathlib import Path

import pytest

from cageops_worker.ingest.cli import PROG, build_parser

DOCS = Path(__file__).parents[4] / "docs"
PREFIX = f"uv run python -m {PROG.removeprefix('python -m ')}"
PLACEHOLDERS = {"<RUN_ID>": "abc123", "<NEW_RUN_ID>": "def456"}


def documented_commands() -> list[str]:
    text = (DOCS / "RUNBOOK.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.S)
    lines = [line.strip() for block in blocks for line in block.splitlines()]
    return [line for line in lines if line.startswith(PREFIX)]


def test_the_runbook_documents_the_commands_it_should():
    commands = documented_commands()
    verbs = {shlex.split(c)[len(PREFIX.split())] for c in commands}

    assert {"status", "backfill", "worker", "dlq", "breaker"} <= verbs
    assert len(commands) >= 15  # the end-to-end walkthrough alone has about twenty


@pytest.mark.parametrize("command", documented_commands())
def test_documented_command_parses(command):
    argv = shlex.split(command)[len(PREFIX.split()) :]
    argv = [PLACEHOLDERS.get(arg, arg) for arg in argv]

    args = build_parser().parse_args(argv)  # a bad flag or verb exits with status 2

    assert args.command == argv[0]


def test_every_command_in_the_claude_md_list_parses_too():
    text = (DOCS.parent / "CLAUDE.md").read_text(encoding="utf-8")
    lines = re.findall(r"`(uv run python -m cageops_worker\.ingest [^`]*)`", text)
    assert len(lines) >= 6
    for line in lines:
        # CLAUDE.md shows alternatives like "list [--reason R] | inspect <job_id>"; check the verb
        words = line.split()[len(PREFIX.split()) :]
        assert words[0] in {"backfill", "scrape-upcoming", "worker", "status", "dlq", "breaker"}
