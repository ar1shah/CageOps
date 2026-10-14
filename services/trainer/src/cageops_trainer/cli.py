"""The trainer's command line: train a version, list versions, activate one.

    python -m cageops_trainer train [--seed N] [--val-start D] [--test-start D]
                                    [--train-start D] [--out-dir P]
    python -m cageops_trainer list [--json]
    python -m cageops_trainer activate VERSION

Exit codes: 0 ok, 1 error (stale features, unknown version, bad boundaries, missing config),
2 bad usage (argparse). JSON goes to stdout, logs and warnings to stderr.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError
from sqlalchemy import Engine

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine
from cageops_common.logs import configure_logging
from cageops_trainer import registry
from cageops_trainer.config import get_trainer_settings
from cageops_trainer.dataset import StaleFeatures
from cageops_trainer.splits import SplitError
from cageops_trainer.train import run_training

PROG = "python -m cageops_trainer"
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
DEFAULT_SEED = 42


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    train = sub.add_parser("train", help="train, evaluate and register a new (inactive) version")
    train.add_argument("--seed", type=int, default=DEFAULT_SEED)
    for name, text in (
        ("val-start", "first day of the validation year (default: derived from the data)"),
        ("test-start", "first day of the test year (default: the last 365 days of data)"),
        ("train-start", "ignore fights before this day (default: all history)"),
    ):
        train.add_argument(f"--{name}", type=date.fromisoformat, metavar="YYYY-MM-DD", help=text)
    train.add_argument("--out-dir", type=Path, help="default: MODEL_ARTIFACT_DIR")

    listing = sub.add_parser("list", help="show registered versions, newest first")
    listing.add_argument("--json", action="store_true", help="full metrics as JSON")

    activate = sub.add_parser("activate", help="make one version the active one")
    activate.add_argument("version")
    return parser


def cmd_train(engine: Engine, args: argparse.Namespace, out: TextIO) -> int:
    out_dir = args.out_dir or get_trainer_settings().model_artifact_dir
    try:
        result = run_training(
            engine,
            seed=args.seed,
            out_dir=out_dir,
            val_start=args.val_start,
            test_start=args.test_start,
            train_start=args.train_start,
        )
    except (StaleFeatures, SplitError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    report = {
        "version": result.version,
        "registered": True,
        "active": False,
        "artifact_path": result.artifact_path,
        "next": f"{PROG} activate {result.version}",
        "warnings": result.warnings,
        "metrics": result.metrics,
    }
    for line in result.metrics["summary"]:
        print(line, file=sys.stderr)
    for warning in result.warnings:
        print(f"WARNING: features were built before newer data: {warning}", file=sys.stderr)
    print(json.dumps(report, indent=2, default=str), file=out)
    return EXIT_OK


def cmd_list(engine: Engine, args: argparse.Namespace, out: TextIO) -> int:
    with engine.connect() as conn:
        versions = registry.list_versions(conn)
    if args.json:
        print(json.dumps(versions, indent=2, default=str), file=out)
        return EXIT_OK
    if not versions:
        print("no model versions registered", file=out)
        return EXIT_OK
    print(
        f"{'':1} {'version':16} {'trained_at':20} {'data_cutoff':12} {'test n':>6} {'log loss':>9}",
        file=out,
    )
    for v in versions:
        test = v["metrics"].get("win_loss", {}).get("test", {}).get("all", {})
        model = test.get("model", {})
        loss = model.get("log_loss")
        print(
            f"{'*' if v['is_active'] else ' '} {v['version']:16} "
            f"{v['trained_at']:%Y-%m-%d %H:%M:%S}  {v['data_cutoff']!s:12} "
            f"{test.get('n', ''):>6} {'' if loss is None else format(loss, '.4f'):>9}",
            file=out,
        )
    print("(the version marked with * is the active one)", file=out)
    return EXIT_OK


def cmd_activate(engine: Engine, args: argparse.Namespace, out: TextIO) -> int:
    try:
        with engine.begin() as conn:
            result = registry.activate(conn, args.version)
    except registry.UnknownVersion as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(result, indent=2), file=out)
    return EXIT_OK


COMMANDS = {"train": cmd_train, "list": cmd_list, "activate": cmd_activate}


def main(
    argv: Sequence[str] | None = None, *, engine: Engine | None = None, out: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors())
        print(
            f"ERROR: configuration is missing or invalid ({fields}); see .env.example",
            file=sys.stderr,
        )
        return EXIT_ERROR
    configure_logging(settings.log_level, stream=sys.stderr)
    logging.getLogger("lightgbm").setLevel(logging.WARNING)
    engine = engine or make_engine(settings.database_url)
    return COMMANDS[args.command](engine, args, out or sys.stdout)
