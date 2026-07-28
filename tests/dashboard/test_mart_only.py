"""Guard: the dashboard is a presentation layer that computes nothing.

The claim is precise. Every number on screen is a column of a warehouse mart; the app reshapes and
formats and never re-derives. It is worth what its enforcement is worth, and prose is worth
nothing, so it is enforced in three places -- the third of which lives at runtime and not here:

* **Static** (this file) -- walk the AST of every module under ``dashboard/`` and fail if any
  module other than ``warehouse.py`` imports psycopg or holds a SQL string. One module owns data
  access, so there is exactly one place to audit.
* **Contract** (this file) -- every column the app declares it selects must exist on that mart.
  This is the guard that turns "if a number is not in a mart, change the mart" from advice into a
  build failure, and it is only possible because ``warehouse.py`` declares its reads as data rather
  than as SQL text. A regex over query strings would be a different, weaker test. Checked in two
  halves: mart *names* against ``dbt/target/manifest.json``, which is offline and catches a rename
  immediately, and mart *columns* against the built warehouse, because the manifest lists a column
  only where ``_marts.yml`` documents it and the marts deliberately document the columns that carry
  a claim rather than all of them.
* **Runtime** (``dashboard/sql/grant_read_only.sql``) -- the ``dashboard_ro`` role can SELECT from
  the marts schema and nothing else. Neither static check can see a query composed at runtime;
  the database can.

Plus a **positive control**, for the same reason ``tests/test_import_containment.py`` carries one:
a guard that passes because the feature was deleted or renamed is indistinguishable from a guard
that passes because the feature is well-behaved. So the last tests assert that ``warehouse.py``
really does query, and that the contract check really is looking at columns rather than at an
empty mapping it would find nothing wrong with.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from dashboard import warehouse
from energy_platform.config import PostgresConfig
from tests.dbt.warehouse_guard import skip_or_fail

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "dashboard"
MANIFEST = ROOT / "dbt" / "target" / "manifest.json"

#: The single module permitted to talk to the database.
DATA_ACCESS_MODULE = "warehouse.py"

#: Import roots that mean "this module talks to a database".
_DATABASE_IMPORTS = frozenset({"psycopg", "psycopg2", "sqlalchemy", "asyncpg"})

#: A string that looks like a query. Deliberately loose -- a false positive here is a module that
#: has to move its SQL into warehouse.py, which is the outcome the rule wants anyway.
_SQL_PATTERN = re.compile(r"\bselect\b.+\bfrom\b", re.IGNORECASE | re.DOTALL)

#: Words that may appear in a `where` / `order_by` fragment without being a column.
_SQL_KEYWORDS = frozenset(
    {"and", "or", "not", "is", "null", "in", "between", "like", "true", "false", "asc", "desc"}
)


def _modules() -> list[Path]:
    return sorted(path for path in DASHBOARD.rglob("*.py") if path.name != "__init__.py")


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


def _string_constants(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_only_the_query_layer_touches_the_database() -> None:
    """No page, chart or formatter may open a connection of its own."""
    offenders = {
        path.relative_to(ROOT).as_posix(): sorted(_import_roots(_tree(path)) & _DATABASE_IMPORTS)
        for path in _modules()
        if path.name != DATA_ACCESS_MODULE and _import_roots(_tree(path)) & _DATABASE_IMPORTS
    }
    assert not offenders, (
        f"only dashboard/{DATA_ACCESS_MODULE} may import a database driver; "
        f"these do too: {offenders}"
    )


def test_no_module_outside_the_query_layer_holds_sql() -> None:
    """SQL in a page is a number the contract check below would never see."""
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        if path.name == DATA_ACCESS_MODULE:
            continue
        # Docstrings explain the rule and quote column names; they are not executable SQL.
        found = [
            text
            for text in _string_constants(_tree(path))
            if _SQL_PATTERN.search(text) and "\n" not in text.strip()
        ]
        if found:
            offenders[path.relative_to(ROOT).as_posix()] = found
    assert not offenders, (
        f"SQL belongs in dashboard/{DATA_ACCESS_MODULE}, where its columns are declared and "
        f"checked against the dbt manifest; found: {offenders}"
    )


def test_every_declared_query_reads_a_mart() -> None:
    """No query may reach past the marts into raw, derived, staging or intermediate."""
    non_marts = {
        name: query.mart
        for name, query in warehouse.QUERIES.items()
        if not query.mart.startswith("mart_")
    }
    assert not non_marts, f"the dashboard may only read mart_* relations; found: {non_marts}"


def test_predicates_only_reference_declared_columns() -> None:
    """A `where` on an undeclared column is a dependency the contract check cannot see."""
    problems: dict[str, set[str]] = {}
    for name, query in warehouse.QUERIES.items():
        # Strip the psycopg placeholders first, or their `s` reads as an identifier.
        fragment = " ".join([query.where or "", *query.order_by]).replace("%s", " ")
        words = {word.lower() for word in re.findall(r"[a-z_][a-z0-9_]*", fragment, re.IGNORECASE)}
        undeclared = words - set(query.columns) - _SQL_KEYWORDS
        if undeclared:
            problems[name] = undeclared
    assert not problems, (
        f"where/order_by may only mention columns listed in `columns`; found: {problems}"
    )


def _manifest_models() -> set[str]:
    """Every model name in the compiled dbt manifest."""
    if not MANIFEST.exists():
        skip_or_fail(f"dbt manifest not found at {MANIFEST}; run `dbt build` first")
    manifest: dict[str, Any] = json.loads(MANIFEST.read_text())
    return {
        node["name"] for node in manifest["nodes"].values() if node.get("resource_type") == "model"
    }


def test_every_mart_the_dashboard_reads_is_a_real_model() -> None:
    """A typo'd mart name is caught from the manifest alone, with no database in the loop.

    Cheap and offline, which is why it is separate from the column check below: this one holds in
    any checkout that has dbt artefacts, so a renamed model fails immediately rather than waiting
    for someone to open the page.
    """
    models = _manifest_models()
    unknown = sorted(warehouse.MARTS - models)
    assert not unknown, f"these are not dbt models: {unknown}"


def _warehouse_columns() -> dict[str, set[str]]:
    """Mart name -> the columns the built warehouse actually has."""
    config = PostgresConfig.from_env()
    try:
        conn = psycopg.connect(config.dsn, connect_timeout=3)
    except psycopg.OperationalError as exc:
        skip_or_fail(f"no Postgres reachable for the column contract check ({exc})")
    columns: dict[str, set[str]] = {}
    with conn, conn.cursor() as cur:
        cur.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = %s",
            (config.marts_schema,),
        )
        for table, column in cur.fetchall():
            columns.setdefault(table, set()).add(column)
    return columns


@pytest.mark.postgres
def test_every_selected_column_exists_in_the_warehouse() -> None:
    """THE CONTRACT. A column the dashboard wants and the marts do not have fails the build.

    This is what turns "if a number is not in a mart, change the mart" from advice into something
    that breaks CI, and it only works because ``warehouse.py`` declares its reads as data. A regex
    over SQL strings would be a weaker and more fragile test.

    Checked against the built warehouse rather than the manifest because the manifest lists a
    column only when ``_marts.yml`` documents or tests it, and the marts deliberately document the
    columns that carry a claim rather than all of them. Asserting against the documented subset
    would fail on columns that genuinely exist -- a guard that cries wolf gets suppressed, and a
    suppressed guard protects nothing. Skips without a warehouse; CI's ``ENERGY_REQUIRE_DBT``
    turns that skip into a failure, as it does for every other guard in this repo.
    """
    available = _warehouse_columns()
    problems: dict[str, dict[str, list[str]]] = {}
    for name, query in warehouse.QUERIES.items():
        known = available.get(query.mart)
        if known is None:
            problems.setdefault(query.mart, {})[name] = ["<relation not in the warehouse>"]
            continue
        missing = sorted(set(query.columns) - known)
        if missing:
            problems.setdefault(query.mart, {})[name] = missing
    assert not problems, (
        "the dashboard selects columns the warehouse does not have. The fix is a dbt model "
        f"change, not a dashboard one: {json.dumps(problems, indent=2)}"
    )


# -- Positive controls ------------------------------------------------------------------------


def test_the_query_layer_actually_queries() -> None:
    """Otherwise every guard above would pass on a dashboard that reads nothing at all."""
    tree = _tree(DASHBOARD / DATA_ACCESS_MODULE)
    assert _import_roots(tree) & _DATABASE_IMPORTS, "warehouse.py no longer imports a driver"
    assert any(_SQL_PATTERN.search(text) for text in _string_constants(tree)), (
        "warehouse.py no longer contains a SELECT"
    )
    assert warehouse.QUERIES, "no queries are declared"
    assert warehouse.MARTS, "no marts are declared"


@pytest.mark.postgres
def test_the_contract_check_is_looking_at_real_columns() -> None:
    """Guards the contract test against a warehouse where it would be vacuously true.

    If ``_warehouse_columns`` returned an empty mapping -- wrong schema, wrong database -- the
    loop above would report every mart as absent and the failure would look like a dashboard bug
    rather than a misconfigured check. This pins that the marts really are there with columns on
    them, so a green contract test means what it says.
    """
    available = _warehouse_columns()
    for mart in sorted(warehouse.MARTS):
        assert mart in available, f"{mart} is missing from the built warehouse"
        assert available[mart], f"{mart} has no columns, so the contract check is vacuous"


@pytest.mark.parametrize("name", sorted(warehouse.QUERIES))
def test_every_declared_query_composes(name: str) -> None:
    """A query that cannot be composed would only be discovered by rendering its page."""
    statement = warehouse.QUERIES[name].statement("analytics_marts")
    assert statement is not None
