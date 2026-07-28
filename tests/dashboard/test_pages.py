"""Every page renders against the seeded warehouse, and says the honest things while doing it.

Skips without a built warehouse so ``just test`` stays green on a fresh clone; CI sets
``ENERGY_REQUIRE_DBT=1`` after ``just warehouse``, which turns the skip into a failure -- a smoke
test that silently did not run is indistinguishable from one that passed. Same policy, and the
same helper, as the reconciliation guards in ``tests/dbt``.

Beyond "it did not crash", these pin the claims the milestone is actually making: the banner comes
from the warehouse rather than a literal, the captured-value share is reported unclamped even when
it is deeply negative, and the forecast oracle is labelled rather than ranked.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import warehouse
from tests.dbt.warehouse_guard import skip_or_fail

from .conftest import APP, PAGES, page_path

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _require_a_built_warehouse() -> None:
    status = warehouse.warehouse_status()
    if not status.reachable:
        skip_or_fail(f"no warehouse connection ({status.error})")
    if status.missing:
        skip_or_fail(f"warehouse is missing marts: {', '.join(status.missing)}")


def _render(path: str) -> AppTest:
    return AppTest.from_file(path, default_timeout=90).run()


def _text(app: AppTest, *kinds: str) -> str:
    """All rendered text of the given element kinds, joined.

    ``AppTest``'s ElementList does not support ``+``, and a page states a thing in whichever
    element reads best -- a caption here, an info box there -- so a test that pinned the element
    type would fail on a rewording that changed nothing about what the page says.
    """
    return " ".join(item.value for kind in kinds for item in getattr(app, kind))


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page: str) -> None:
    app = _render(page_path(page))
    assert not app.exception, [str(item.value) for item in app.exception]


def test_entrypoint_renders() -> None:
    app = _render(str(APP))
    assert not app.exception, [str(item.value) for item in app.exception]


@pytest.mark.parametrize("page", PAGES)
def test_every_page_carries_the_data_mode_banner(page: str) -> None:
    """Persistent means every page, not just the one navigation opens first."""
    app = _render(page_path(page))
    text = _text(app, "caption")
    assert "Demo mode" in text or "Real mode" in text


def test_the_banner_matches_what_the_warehouse_says() -> None:
    """The banner is read from mart_data_quality.data_mode, not hardcoded.

    Asserted by comparing the rendered words against the mart's own value, so a literal string in
    the app would fail here the moment a real warehouse said something different.
    """
    modes = {row["data_mode"] for row in warehouse.fetch("data_mode")}
    assert modes, "the seeded warehouse should classify its telemetry"

    app = _render(page_path("overview"))
    text = _text(app, "caption")
    expected = "Demo mode" if modes == {"demo_synthetic"} else "Real mode"
    assert expected in text


def test_overview_reports_partial_coverage() -> None:
    """The demo's windows are short, and the page has to say so somewhere visible."""
    app = _render(page_path("overview"))
    text = _text(app, "caption", "markdown")
    assert "Partial" in text or "partial" in text


def test_dispatch_reports_the_captured_share_without_clamping_it() -> None:
    """The headline is negative on seeded data, and the page must show that rather than hide it.

    ``captured_value_share`` is bounded above by 1 and deliberately unbounded below. A UI that
    clipped it to [0, 1] would suppress the most interesting result in the repo.
    """
    rows = [row for row in warehouse.fetch("dispatch_regret") if row["is_simulated"]]
    if not rows:
        pytest.skip("no simulated windows in this warehouse")

    app = _render(page_path("dispatch"))
    assert not app.exception, [str(item.value) for item in app.exception]

    shares = [
        row["captured_value_share"] for row in rows if row["captured_value_share"] is not None
    ]
    if any(share < 0 for share in shares):
        rendered = _text(app, "metric", "warning")
        assert "-" in rendered, "a negative captured share must reach the screen with its sign"


def test_dispatch_shows_the_attainable_share_beside_the_captured_one() -> None:
    """The pair is the finding. The share alone invites blaming the forecasts."""
    app = _render(page_path("dispatch"))
    labels = [item.label for item in app.metric]
    assert any("Captured value share" in label for label in labels)
    assert any("Attainable" in label for label in labels)


def test_forecasts_label_the_oracle_rather_than_ranking_it() -> None:
    """The flat-plate model generated the labels, so beating the field is a tautology.

    Driven to the target the oracle actually belongs to. Only PV has one -- M3 generated PV from
    irradiance with that model, and nothing generated the load the same way -- so asserting on
    whichever target the selector happens to open on would pass or fail for the wrong reason.
    """
    rows = warehouse.fetch("forecast_eval")
    oracle_targets = {row["target"] for row in rows if row["role"] == "oracle"}
    if not oracle_targets:
        pytest.skip("no oracle rows in this warehouse")

    app = _render(page_path("forecasts"))
    app.selectbox[0].select(sorted(oracle_targets)[0]).run()

    text = _text(app, "markdown", "subheader", "info")
    assert "Oracle" in text
    assert "tautology" in text.lower()
    assert "excluded from any ranking" in text


def test_economics_carries_the_battery_reset_caveat() -> None:
    """battery_unreturned_kwh is the generator's midnight reset leaking into cost."""
    app = _render(page_path("economics"))
    text = _text(app, "caption")
    assert "battery_unreturned_kwh" in text
