"""Guard: the scientific stack is confined to ``energy_platform.forecasting``.

M7 adds pvlib and scikit-learn, and with them pandas and scipy. The claim made in
``pyproject.toml`` is deliberately narrow: **no first-party module outside ``forecasting/`` imports
the scientific stack.** That is what keeps the ingestion path stdlib-only, which is in turn what
M3's cross-platform content-hash guarantee rests on (``connectors/synthetic.py``: "no numpy -- so
float reprs stay identical across machines and the hash is bulletproof").

It is *not* the claim that the process is numpy-free. ``highspy`` has pulled numpy in transitively
since M6, so ``numpy`` is in ``sys.modules`` the moment anything imports the optimiser. Stretching
the claim to cover that would make it false -- the same scoping discipline the README applies to
bit-reproducibility versus value-stability.

Two layers, because neither alone is sufficient:

* **Static** -- walk the AST of every module under ``src/energy_platform``. This catches imports the
  runtime check cannot: a lazy import inside a branch that never executes, or one inside
  ``if TYPE_CHECKING:``. Deliberately *not* exempting ``TYPE_CHECKING`` -- a type-only pandas import
  under ``dispatch/`` is exactly the drift being guarded against.
* **Runtime** -- import ``energy_platform.cli`` in a subprocess and assert the heavy modules are
  absent from ``sys.modules``. This catches what the AST cannot: ``importlib``, ``__import__``, and
  a first-party module that pulls the stack in transitively through an innocent-looking import.

Plus a **positive control**. A guard that passes because the feature was deleted or renamed is
indistinguishable from one that passes because the feature is well-behaved, so the last test
asserts that ``forecasting/`` does import the stack. Every singular test in ``dbt/tests/`` carries
the same empty-input sentinel for the same reason.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "energy_platform"
FORECASTING = SRC / "forecasting"

# The distribution roots M7 introduces (plus numpy, which predates it transitively but must still
# never be imported by first-party code outside forecasting/).
SCIENTIFIC_STACK = frozenset({"numpy", "pandas", "scipy", "sklearn", "pvlib", "joblib"})

# Dynamic-import escape hatches. `importlib.metadata` is legitimately used to report the solver
# version (dispatch/optimizer.py), so the ban is on the two functions that can smuggle a module
# past a static walk -- not on the package.
BANNED_DYNAMIC_IMPORTS = frozenset({"importlib.import_module", "__import__"})


def _import_roots(tree: ast.AST) -> set[str]:
    """Every top-level distribution name imported anywhere in the tree.

    Walks the whole tree rather than ``tree.body``: lazy imports inside function bodies already
    exist in this codebase (``connectors/smard.py`` imports ``bisect`` inside a function, ``cli.py``
    imports ``zoneinfo`` inside one), and those are exactly as capable of pulling in pandas.

    Both spellings collapse to the same root: ``import pvlib.irradiance`` gives an alias name of
    ``"pvlib.irradiance"``, ``from pvlib import irradiance`` gives a module of ``"pvlib"``.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            # `node.module` is None for `from . import x`; ruff's TID bans those anyway, but the
            # guard must not crash before ruff runs.
            roots.add(node.module.split(".")[0])
    return roots


def _dynamic_import_calls(tree: ast.AST) -> set[str]:
    """Calls to ``importlib.import_module`` or ``__import__``, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "__import__":
            found.add("__import__")
        elif isinstance(func, ast.Attribute) and func.attr == "import_module":
            found.add("importlib.import_module")
    return found


def _modules_outside_forecasting() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if FORECASTING not in p.parents]


def test_scientific_stack_is_confined_to_forecasting() -> None:
    """No first-party module outside forecasting/ may import numpy/pandas/scipy/sklearn/pvlib."""
    offenders = {
        str(path.relative_to(SRC)): sorted(
            _import_roots(ast.parse(path.read_text())) & SCIENTIFIC_STACK
        )
        for path in _modules_outside_forecasting()
    }
    offenders = {module: imports for module, imports in offenders.items() if imports}
    assert not offenders, (
        "the scientific stack is confined to energy_platform.forecasting -- the ingestion path is "
        f"stdlib-only and M3's content-hash guarantee depends on it. Offenders: {offenders}"
    )


def test_no_module_smuggles_imports_past_the_static_walk() -> None:
    """``importlib.import_module`` / ``__import__`` would defeat the AST guard; nothing uses them.

    ``importlib.metadata`` is a legitimate exception and is deliberately not banned -- the optimiser
    reports its solver version through it.
    """
    offenders = {
        str(path.relative_to(SRC)): sorted(calls)
        for path in sorted(SRC.rglob("*.py"))
        if (calls := _dynamic_import_calls(ast.parse(path.read_text())) & BANNED_DYNAMIC_IMPORTS)
    }
    assert not offenders, (
        f"dynamic imports would make the containment guard above unenforceable; found {offenders}"
    )


def test_importing_the_cli_does_not_load_the_heavy_stack() -> None:
    """The static guard proves first-party hygiene; this proves the transitive consequence.

    numpy is deliberately excluded from the assertion: highspy has pulled it in since M6, so
    requiring its absence would assert something already false rather than something worth holding.
    """
    probe = (
        "import energy_platform.cli, sys;"
        "heavy = {'pandas', 'scipy', 'sklearn', 'pvlib'} & set(sys.modules);"
        "assert not heavy, f'importing the CLI loaded {sorted(heavy)}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        "`energy-platform backfill` must not pay pandas/sklearn import cost -- the forecasting "
        f"import belongs inside _run_forecast, not at module level.\n{result.stderr}"
    )


def test_matplotlib_stays_out_of_the_package_entirely() -> None:
    """M8's README figure is a script, not a capability of the platform -- enforced, not intended.

    A stricter rule than the one above and a different shape: pvlib and sklearn are *confined* to
    forecasting/, but matplotlib belongs nowhere under ``src/`` at all. It is a dev dependency
    (pyproject's ``dev`` group) because plotting is a reporting step, and the moment a module here
    imports it, `docker compose up` starts shipping a plotting stack and someone starts rendering
    charts inside the pipeline. ``scripts/report_regret.py`` lives outside the package precisely so
    this line can be drawn.
    """
    offenders = {
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if "matplotlib" in _import_roots(ast.parse(path.read_text()))
    }
    assert not offenders, (
        "matplotlib is a dev-only reporting dependency and must not be imported anywhere under "
        f"src/energy_platform -- put the figure in scripts/ instead. Offenders: {sorted(offenders)}"
    )


def test_forecasting_actually_imports_the_stack() -> None:
    """Positive control: the guard must not pass by the package having been deleted or renamed.

    Without this, removing forecasting/ entirely would leave every assertion above green.
    """
    assert FORECASTING.is_dir(), f"{FORECASTING} is missing; the containment guard is vacuous"
    imported: set[str] = set()
    for path in sorted(FORECASTING.rglob("*.py")):
        imported |= _import_roots(ast.parse(path.read_text())) & SCIENTIFIC_STACK
    assert {"numpy", "pvlib", "sklearn"} <= imported, (
        "forecasting/ is expected to import numpy, pvlib and sklearn -- if it no longer does, the "
        f"containment guard above is asserting nothing. Found: {sorted(imported)}"
    )
