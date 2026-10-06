"""The feature CLI: every documented command parses, and `show` / `rebuild` behave."""

import io
import json
import re
import shlex
from datetime import date
from pathlib import Path

import pytest

from cageops_worker.features import build as build_module
from cageops_worker.features.cli import PROG, build_parser, main

DOCS = Path(__file__).parents[4] / "docs"
PREFIX = f"uv run python -m {PROG.removeprefix('python -m ')}"
ROW = {"sig_landed": 40, "sig_att": 80, "td_landed": 1, "td_att": 3, "sub_att": 1, "knockdowns": 0}


def documented_commands() -> list[str]:
    text = (DOCS / "RUNBOOK.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.S)
    lines = [line.strip() for block in blocks for line in block.splitlines()]
    return [line for line in lines if line.startswith(PREFIX)]


def test_the_runbook_documents_both_commands():
    verbs = {shlex.split(c)[len(PREFIX.split())] for c in documented_commands()}
    assert verbs == {"rebuild", "show"}


@pytest.mark.parametrize("command", documented_commands())
def test_documented_command_parses(command):
    argv = shlex.split(command)[len(PREFIX.split()) :]

    args = build_parser().parse_args(argv)  # a bad flag or verb exits with status 2

    assert args.command == argv[0]


def test_the_claude_md_commands_parse_too():
    text = (DOCS.parent / "CLAUDE.md").read_text(encoding="utf-8")
    lines = re.findall(r"`(uv run python -m cageops_worker\.features [^`]*)`", text)
    assert len(lines) == 2
    parser = build_parser()
    args = parser.parse_args(["rebuild", "--today", "2026-10-06"])
    assert args.today == date(2026, 10, 6)
    # CLAUDE.md writes the two ways to pick a fighter as "--fighter NAME | --fighter-id N"
    assert parser.parse_args(["show", "--fighter", "A B"]).fighter == "A B"
    assert parser.parse_args(["show", "--fighter-id", "7", "--json"]).fighter_id == 7


def test_show_needs_exactly_one_way_to_pick_a_fighter():
    with pytest.raises(SystemExit) as no_flag:
        build_parser().parse_args(["show"])
    with pytest.raises(SystemExit) as both:
        build_parser().parse_args(["show", "--fighter", "A", "--fighter-id", "1"])
    assert (no_flag.value.code, both.value.code) == (2, 2)


# -- against the database ----------------------------------------------------------------------


def run(world, *argv):
    out = io.StringIO()
    code = main(list(argv), engine=world.engine, out=out)
    return code, out.getvalue()


def career(world):
    world.fighter(1, "José Aldo")
    world.fighter(2, "Ben Smith")
    world.fighter(3, "Chan Lee")
    world.fight(1, 2, date(2021, 1, 1), winner=1, stats={1: ROW, 2: ROW})
    world.fight(1, 3, date(2021, 9, 1), winner=3, stats={1: ROW, 3: ROW})
    world.fight(
        1, 2, date(2021, 5, 1), winner=1, stats={1: ROW, 2: ROW}
    )  # inserted last, dated middle


def test_rebuild_prints_a_json_report(world):
    career(world)

    code, out = run(world, "rebuild", "--today", "2030-01-01")

    assert code == 0
    report = json.loads(out)
    assert (report["rows"], report["today"]) == (6, "2030-01-01")


def test_a_failed_rebuild_exits_1_and_prints_the_report_on_stderr(world, monkeypatch, capsys):
    career(world)
    real = build_module.build_rows
    monkeypatch.setattr(build_module, "build_rows", lambda *a, **k: real(*a, **k)[:-1])

    code, out = run(world, "rebuild", "--today", "2030-01-01")

    assert (code, out) == (1, "")
    err = capsys.readouterr().err
    assert "ERROR" in err and "fights_without_exactly_two_rows" in err


def test_show_lists_one_fighters_rows_in_date_order(world):
    career(world)
    world.build()

    code, out = run(world, "show", "--fighter", "Jose ALDO")  # case and accents ignored

    assert code == 0
    lines = out.strip().splitlines()
    assert lines[0].split()[:2] == ["event_date", "opponent"]
    assert [line.split()[0] for line in lines[1:]] == ["2021-01-01", "2021-05-01", "2021-09-01"]
    assert [line.split()[-9] for line in lines[1:]] == ["0", "1", "2"]  # prior_ufc_fights


def test_show_json_has_every_column(world):
    career(world)
    world.build()

    code, out = run(world, "show", "--fighter-id", "1", "--json")

    rows = json.loads(out)
    assert code == 0 and len(rows) == 3
    assert [r["event_date"] for r in rows] == ["2021-01-01", "2021-05-01", "2021-09-01"]
    assert {"opponent", "sig_str_def_last5", "load_run_id", "built_at"} <= set(rows[0])


def test_show_with_an_unknown_name_exits_1(world, capsys):
    career(world)
    world.build()

    code, out = run(world, "show", "--fighter", "Nobody Here")

    assert (code, out) == (1, "")
    assert "no fighter matches" in capsys.readouterr().err


def test_show_with_an_ambiguous_name_lists_the_candidates_and_exits_2(world):
    world.fighter(1, "Jon Smith")
    world.fighter(2, "Jon  SMITH")
    world.fighter(3, "Other One")
    world.fight(1, 3, date(2021, 1, 1), winner=1, stats={1: ROW, 3: ROW})
    world.build()

    code, out = run(world, "show", "--fighter", "jon smith")

    assert code == 2
    assert "--fighter-id" in out and "  1  Jon Smith" in out and "  2  Jon  SMITH" in out


def test_show_finds_a_fighter_by_a_stored_alias(world):
    career(world)
    world.sql(
        "INSERT INTO fighter_aliases (source, alias_norm, fighter_id) VALUES ('mdabbert', :a, 2)",
        a="benny smith",
    )
    world.build()

    code, out = run(world, "show", "--fighter", "Benny Smith")

    assert code == 0 and "Jose" not in out
    assert len(out.strip().splitlines()) == 1 + 2  # header plus Ben Smith's two fights


def test_show_before_any_rebuild_says_so_and_exits_1(world, capsys):
    career(world)

    code, _ = run(world, "show", "--fighter", "Ben Smith")

    assert code == 1
    assert "rebuild" in capsys.readouterr().err
