"""The API image must not carry training code (ROADMAP 2b carry-over).

Two layers, because each catches what the other misses: a static scan sees an import that sits in
a branch never executed, and the subprocess sees an import that arrives through a dependency.
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCANNED = [ROOT / "services" / "api", ROOT / "packages" / "cageops_common"]
FORBIDDEN = "cageops_trainer"


def python_files():
    for base in SCANNED:
        for path in base.rglob("*.py"):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def imported_modules(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_nothing_under_the_api_or_the_shared_package_imports_the_trainer():
    offenders = []
    for path in python_files():
        if path == Path(__file__).resolve():
            continue
        for module in imported_modules(ast.parse(path.read_text())):
            if module == FORBIDDEN or module.startswith(FORBIDDEN + "."):
                offenders.append(f"{path.relative_to(ROOT)}: {module}")
    assert not offenders, offenders


def test_importing_the_api_does_not_load_the_trainer_at_runtime():
    code = (
        "import sys; import cageops_api.main; "
        "import cageops_common.inference.bundle, cageops_common.inference.rows; "
        f"assert {FORBIDDEN!r} not in sys.modules, 'the trainer was imported'"
    )
    # -I: isolated mode, so nothing in the current directory can shadow a module.
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_scan_would_catch_an_offender():
    tree = ast.parse("from cageops_trainer.model import fit\nimport cageops_trainer.features")
    assert list(imported_modules(tree)) == ["cageops_trainer.model", "cageops_trainer.features"]
