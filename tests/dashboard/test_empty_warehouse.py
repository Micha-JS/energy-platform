"""Every page must degrade gracefully when the warehouse has not been built.

THIS IS THE TEST NOT TO SKIP. A stranger clones the repo, runs ``just demo``, and opens the
dashboard -- and at that moment the raw zone is empty, no dbt model has ever run, and not one of
the marts exists. That is the first thing that happens to this app, every time. A traceback there
is the difference between "polished" and "works on the author's machine", so the no-data path is
tested at least as hard as the populated one.

It needs no warehouse, which is the point: it runs in the fast CI job on every push, where the
seeded tests can only skip.

Both no-data states are covered, because they are genuinely different and a page that confused
them would mislead:

* **The marts do not exist** -- the onboarding state, which must name the commands that fix it.
* **The marts exist and are empty for the selection** -- a built warehouse with nothing in the
  chosen window, which must NOT tell the reader to go and build the warehouse they already built.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import chrome, warehouse

from .conftest import APP, PAGES, page_path


def _render(path: str) -> AppTest:
    return AppTest.from_file(path, default_timeout=90).run()


@pytest.mark.postgres
@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_without_a_warehouse(page: str, empty_marts_schema: str) -> None:
    """No page may raise when its marts are absent."""
    app = _render(page_path(page))
    assert not app.exception, [str(item.value) for item in app.exception]


@pytest.mark.postgres
def test_the_entrypoint_renders_without_a_warehouse(empty_marts_schema: str) -> None:
    """The navigation shell too, not only the pages reached through it."""
    app = _render(str(APP))
    assert not app.exception, [str(item.value) for item in app.exception]


@pytest.mark.postgres
@pytest.mark.parametrize("page", PAGES)
def test_every_page_says_how_to_fix_it(page: str, empty_marts_schema: str) -> None:
    """A dead end is not graceful. Each page must name the commands that produce data."""
    app = _render(page_path(page))
    body = " ".join(item.value for item in app.markdown) + " ".join(
        item.value for item in app.subheader
    )
    assert "No data yet" in body
    code_blocks = " ".join(item.value for item in app.code)
    assert "just dbt-seed" in code_blocks
    assert "just warehouse" in code_blocks


@pytest.mark.postgres
def test_the_status_probe_reports_missing_marts_rather_than_raising(
    empty_marts_schema: str,
) -> None:
    """The probe distinguishes "not built" from "unreachable", and never throws for either."""
    status = warehouse.warehouse_status()
    assert status.reachable
    assert not status.is_ready
    assert set(status.missing) == set(warehouse.MARTS)
    assert status.present == frozenset()


@pytest.mark.postgres
def test_the_banner_does_not_claim_a_data_mode_it_cannot_read(empty_marts_schema: str) -> None:
    """With no marts, the banner says so instead of guessing synthetic or real.

    A hardcoded default would be indistinguishable from a correct reading right up until it was
    wrong about a real house.
    """
    app = _render(page_path("overview"))
    banner_text = " ".join(item.value for item in app.info) + " ".join(
        item.value for item in app.caption
    )
    assert "Demo mode" not in banner_text
    assert "Real mode" not in banner_text


def test_an_unreachable_database_is_a_reported_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres being down is a message, not a stack trace -- it is the state during `just demo`.

    No database needed for this one: it points the app at a port nothing is listening on.
    """
    monkeypatch.setenv("ENERGY_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("ENERGY_PG_PORT", "1")
    status = warehouse.warehouse_status()
    assert not status.reachable
    assert not status.is_ready
    assert status.error

    app = _render(page_path("overview"))
    assert not app.exception, [str(item.value) for item in app.exception]
    assert any("No warehouse connection" in item.value for item in app.subheader)


def test_the_seed_guidance_names_both_commands_in_order() -> None:
    """`just warehouse` alone fails on a fresh clone -- it needs a seeded raw zone first."""
    assert chrome.SEED_COMMANDS.index("just dbt-seed") < chrome.SEED_COMMANDS.index(
        "just warehouse"
    )
