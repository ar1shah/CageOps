"""Registering, listing and activating versions, through the registry functions and the CLI."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cageops_trainer import registry
from cageops_trainer.cli import main
from cageops_trainer.train import run_training

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def two_versions(lab, tmp_path):
    names = []
    for minutes in (0, 1):
        result = run_training(
            lab.engine, seed=1, out_dir=tmp_path, now=NOW + timedelta(minutes=minutes)
        )
        names.append(result.version)
    return names


def active(db) -> list[str]:
    with db.connect() as conn:
        return list(
            conn.execute(
                text("SELECT version FROM model_versions WHERE is_active ORDER BY version")
            ).scalars()
        )


def run_cli(db, *argv):
    out = io.StringIO()
    code = main(list(argv), engine=db, out=out)
    return code, out.getvalue()


# -- the registry ---------------------------------------------------------------------------


def test_training_registers_an_inactive_version_and_never_activates_it(db, two_versions):
    assert len(two_versions) == 2
    assert active(db) == []


def test_activating_one_version_then_another_leaves_exactly_one_active(db, two_versions):
    first, second = two_versions

    with db.begin() as conn:
        assert registry.activate(conn, first) == {
            "version": first,
            "was_active": False,
            "deactivated": [],
        }
    assert active(db) == [first]

    with db.begin() as conn:
        result = registry.activate(conn, second)

    assert result["deactivated"] == [first]
    assert active(db) == [second]


def test_activating_the_active_version_again_changes_nothing(db, two_versions):
    first, _ = two_versions
    with db.begin() as conn:
        registry.activate(conn, first)
    with db.begin() as conn:
        result = registry.activate(conn, first)

    assert result["was_active"] is True and result["deactivated"] == []
    assert active(db) == [first]


def test_activating_an_unknown_version_changes_nothing(db, two_versions):
    first, _ = two_versions
    with db.begin() as conn:
        registry.activate(conn, first)

    with pytest.raises(registry.UnknownVersion), db.begin() as conn:
        registry.activate(conn, "no-such-version")

    assert active(db) == [first]


def test_the_database_itself_refuses_two_active_versions_whatever_the_code_does(db, two_versions):
    first, second = two_versions
    with db.begin() as conn:
        registry.activate(conn, first)

    with pytest.raises(IntegrityError, match="uq_model_versions_one_active"), db.begin() as conn:
        conn.execute(
            text("UPDATE model_versions SET is_active = true WHERE version = :v"), {"v": second}
        )


def test_list_is_newest_first_and_carries_the_metrics(db, two_versions):
    with db.connect() as conn:
        versions = registry.list_versions(conn)

    assert [v["version"] for v in versions] == list(reversed(two_versions))
    assert versions[0]["metrics"]["win_loss"]["test"]["all"]["model"]["log_loss"] > 0


# -- the CLI --------------------------------------------------------------------------------


def test_list_with_no_versions_says_so_and_exits_zero(lab):
    code, out = run_cli(lab.engine, "list")

    assert code == 0 and "no model versions registered" in out


def test_train_prints_a_json_report_registers_inactive_and_exits_zero(lab, tmp_path, capsys):
    code, out = run_cli(lab.engine, "train", "--seed", "3", "--out-dir", str(tmp_path))

    report = json.loads(out)
    assert code == 0
    assert report["registered"] is True and report["active"] is False
    assert report["next"].endswith(f"activate {report['version']}")
    assert report["metrics"]["splits"]["boundaries"]["test_start"]
    assert (tmp_path / report["version"] / "win_loss.txt").exists()
    # the plain-English summary goes to stderr so stdout stays JSON
    assert "Test:" in capsys.readouterr().err


def test_train_refuses_stale_features_with_exit_one_and_writes_nothing(lab, tmp_path, capsys):
    lab.sql("UPDATE fighters SET reach_cm = reach_cm + 5 WHERE id = 1")

    code, out = run_cli(lab.engine, "train", "--out-dir", str(tmp_path))

    assert code == 1 and out == ""
    assert "features rebuild" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
    with lab.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM model_versions")).scalar_one() == 0


def test_train_warns_on_a_newer_fetch_and_still_trains(lab, tmp_path, capsys):
    lab.sql(
        "INSERT INTO raw_pages (url, source, fetched_at, status, html)"
        " VALUES ('http://example.test/e', 'wikipedia', now(), 200, '<html/>')"
    )

    code, out = run_cli(lab.engine, "train", "--out-dir", str(tmp_path))

    assert code == 0
    assert "WARNING" in capsys.readouterr().err
    assert json.loads(out)["warnings"]


def test_bad_boundaries_exit_one(lab, tmp_path, capsys):
    code, _ = run_cli(
        lab.engine, "train", "--out-dir", str(tmp_path),
        "--val-start", "2023-01-01", "--test-start", "2022-01-01",
    )  # fmt: skip

    assert code == 1
    assert "before test_start" in capsys.readouterr().err


def test_list_marks_the_active_version(db, two_versions):
    first, _ = two_versions
    run_cli(db, "activate", first)

    code, out = run_cli(db, "list")

    assert code == 0
    marked = [line for line in out.splitlines() if line.startswith("*")]
    assert len(marked) == 1 and first in marked[0]


def test_list_json_is_machine_readable(db, two_versions):
    code, out = run_cli(db, "list", "--json")

    rows = json.loads(out)
    assert code == 0 and len(rows) == 2 and rows[0]["is_active"] is False


def test_activate_prints_what_changed_and_an_unknown_version_exits_one(db, two_versions, capsys):
    first, second = two_versions

    code, out = run_cli(db, "activate", first)
    assert code == 0 and json.loads(out)["version"] == first
    code, out = run_cli(db, "activate", second)
    assert json.loads(out)["deactivated"] == [first]

    code, out = run_cli(db, "activate", "nope")
    assert code == 1 and out == ""
    assert "no model version 'nope'" in capsys.readouterr().err
    assert active(db) == [second]


def test_a_missing_subcommand_or_a_bad_date_is_a_usage_error(db):
    with pytest.raises(SystemExit) as no_command:
        main([], engine=db)
    with pytest.raises(SystemExit) as bad_date:
        main(["train", "--test-start", "yesterday"], engine=db)

    assert no_command.value.code == 2 and bad_date.value.code == 2
