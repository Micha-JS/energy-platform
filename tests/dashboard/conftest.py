"""Fixtures for rendering the dashboard under test.

Two worlds are needed, and both are real Postgres rather than a mock: the seeded warehouse CI
builds, and an empty schema that has never held a mart. Mocking the second would test the mock --
the failure being guarded against is a real ``UndefinedTable`` reaching a real page.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from energy_platform.config import PostgresConfig

ROOT = Path(__file__).resolve().parents[2]
VIEWS = ROOT / "dashboard" / "views"
APP = ROOT / "dashboard" / "app.py"

#: Every page, by the name its file carries. Parametrising over this rather than listing pages in
#: each test means a fifth page added to the app is covered by every test here the moment it lands
#: -- including the empty-warehouse one, which is the test most likely to be forgotten.
PAGES = ("overview", "economics", "dispatch", "forecasts")


def page_path(name: str) -> str:
    return str(VIEWS / f"{name}.py")


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    """A connection able to create schemas: the platform's credentials, not the app's."""
    return PostgresConfig.from_env().dsn


@pytest.fixture
def postgres_admin(admin_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    try:
        conn = psycopg.connect(admin_dsn, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("ENERGY_REQUIRE_POSTGRES"):
            pytest.fail(f"ENERGY_REQUIRE_POSTGRES is set but no Postgres is reachable ({exc})")
        pytest.skip(f"no Postgres available ({exc})")
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def empty_marts_schema(
    postgres_admin: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Point the dashboard at a marts schema that exists and is empty.

    Dropped before *and* after, the same belt-and-braces as the ``raw_repo`` fixture in the root
    conftest: a previous crashed run must not leave a schema that makes this test pass for the
    wrong reason.

    The schema is granted to ``dashboard_ro`` so the app connects as its real, read-only self.
    Testing this path as ``dagster`` would quietly check a permission set the app never has.
    """
    schema = "analytics_marts_empty_test"
    with postgres_admin.cursor() as cur:
        cur.execute(f"drop schema if exists {schema} cascade")
        cur.execute(f"create schema {schema}")
        # Best-effort: on a database where the role was never created (a bare `pytest` against a
        # hand-rolled Postgres) the app falls back to reporting an unreachable warehouse, which is
        # itself a graceful state -- so the test still means something, just about a different
        # branch. Where the role does exist, this makes the run faithful.
        cur.execute("select 1 from pg_roles where rolname = 'dashboard_ro'")
        if cur.fetchone():
            cur.execute(f"grant usage on schema {schema} to dashboard_ro")

    monkeypatch.setenv("ENERGY_MARTS_SCHEMA", schema)
    try:
        yield schema
    finally:
        with postgres_admin.cursor() as cur:
            cur.execute(f"drop schema if exists {schema} cascade")


@pytest.fixture(autouse=True)
def _clear_streamlit_cache() -> Iterator[None]:
    """Drop ``st.cache_data`` between tests.

    Without this, the first test's warehouse status is served to the next one from cache, and the
    empty-warehouse tests would silently assert against seeded results -- the exact failure mode
    where a graceful-degradation test passes without ever exercising degradation.
    """
    import streamlit as st

    st.cache_data.clear()
    yield
    st.cache_data.clear()
