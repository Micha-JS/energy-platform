"""When a day's dispatch is decided, and what was knowable at that instant.

M6 optimises with hindsight: every price, every kWh of PV and every kWh of load is known before the
solver starts. M8 does not get that. A household planning day D commits to a battery schedule at a
particular moment, and the whole point of the milestone is that the moment is *before* D happens.

**The decision time, stated once.**

    Berlin day D is decided at D 00:00 Europe/Berlin.

That is the same instant :func:`energy_platform.forecasting.vintage.issue_time_ms` already calls a
prediction's issue time, and it is deliberately not restated here -- both read
``berlin_day_window``, the helper the hourly spine and the coverage windows also size themselves
from, so DST is handled in one place and the two cannot drift. ``test_forward_lookahead`` pins the
equality rather than leaving it to this docstring.

M7 chose that instant *for* this consumer (see ``vintage.py``), and the choice constrains M8 rather
than the other way round: the synthetic vintage generator stamps its issues at 18:00 Berlin the
evening before, which is admissible under M7's rule but would not be knowable at, say, D-1 14:00.
Moving the decision earlier would invalidate every vintage the platform has stored.

**The information set, and the asymmetry that makes the milestone interesting.**

At D 00:00 Berlin:

* **day-ahead prices for every hour of D are actuals.** The single day-ahead auction clears and
  publishes around 12:45 CET on D-1, so by the time D begins its prices are settled fact, not a
  forecast. Nothing in this milestone predicts a price.
* **PV and load for D are forecasts**, reached only through M7's vintage rule.

Modelling that asymmetry correctly is the realism claim. A day-ahead battery decision is not a
stochastic program over prices; it is a deterministic program over *known* prices with uncertain
physics, and conflating the two would make the problem both harder and less true.

**Why the publication rule lives here rather than in a column.**

Day-ahead prices reach the warehouse through the settled-data path -- ``raw.observations_current``,
which collapses re-fetches to the latest ingestion. The only temporal provenance a price row
carries is ``raw.ingestion.fetched_at``, which records when *this platform* fetched the number, not
when the exchange published it; ``stg_prices`` does not even propagate it. So there is no stored
instant to compare a decision time against, and inventing one per row would be fabricating
provenance the source never supplied.

Instead the market's own timetable is written down as a rule, exactly as M7 writes down its vintage
rule: one function, one id, persisted on every run so a stored result names the rule that produced
it. :func:`assert_prices_published` is what turns it from a comment into a refusal -- planning a day
whose auction has not cleared yet raises rather than quietly reading a price out of the warehouse
that a real household could not have seen.

The rule is a property of the market, not of the data, so it holds equally on real and synthetic
windows. What it does *not* claim is that the warehouse actually holds those prices: a published
price that was never ingested is a gap, and gaps are handled the way they always are -- the hour is
unpriced, the battery idles in it, and the coverage ratio falls.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Final

from energy_platform.orchestration.ingest import BERLIN, berlin_day_window

# Persisted on every forward-dispatch run, so a stored row names the rules it was produced under
# rather than relying on this module having said the same thing at the time.
DECISION_RULE_ID: Final = "berlin_midnight_before_target_day"
PRICE_PUBLICATION_RULE_ID: Final = "day_ahead_auction_d_minus_1_1245_berlin"

# When the single day-ahead auction for delivery day D publishes its results: about 12:45 CET on
# D-1, after gate closure at 12:00. Local wall clock rather than UTC, because the exchange's
# timetable is stated in local time and follows the DST switch with it.
DAY_AHEAD_PUBLICATION_HOUR_LOCAL: Final = 12
DAY_AHEAD_PUBLICATION_MINUTE_LOCAL: Final = 45


class DecisionTimeError(RuntimeError):
    """Raised when a plan would use information that did not exist at its decision time."""


def decision_time_ms(target_day: date) -> int:
    """The instant Berlin day ``target_day`` is decided: midnight starting that day.

    Delegates to ``berlin_day_window`` rather than combining a date with a time here, which is what
    keeps it identical to M7's issue instant on the two days a year when the arithmetic could
    differ.
    """
    return berlin_day_window(target_day).start_ms


def price_publication_ms(target_day: date) -> int:
    """When the day-ahead prices for ``target_day`` became public: D-1 12:45 Europe/Berlin.

    Always a real wall clock -- the spring-forward gap is at 02:00, so 12:45 exists on every date --
    and always earlier than :func:`decision_time_ms` for the same day, which is the fact that lets
    a decision at Berlin midnight treat the coming day's prices as settled.
    """
    published_local = datetime.combine(
        target_day - timedelta(days=1),
        time(hour=DAY_AHEAD_PUBLICATION_HOUR_LOCAL, minute=DAY_AHEAD_PUBLICATION_MINUTE_LOCAL),
        tzinfo=BERLIN,
    )
    return int(published_local.timestamp() * 1000)


def prices_are_published(target_day: date, decision_ms: int) -> bool:
    """Whether ``target_day``'s day-ahead prices had cleared by ``decision_ms``."""
    return price_publication_ms(target_day) <= decision_ms


def assert_prices_published(target_day: date, decision_ms: int) -> None:
    """Refuse to plan a day whose day-ahead auction had not cleared at the decision time.

    The guard that matters is the horizon one. Planning day D at D 00:00 is fine -- D's prices
    published the previous lunchtime. Extending the same plan to D+1 is not: that auction does not
    clear until 12:45 on D, half a day *after* the decision, so a two-day horizon built from one
    decision instant would be reading prices out of the future. This is the price-side twin of
    ``select_vintage`` refusing a forecast issued after the prediction.
    """
    published = price_publication_ms(target_day)
    if published > decision_ms:
        raise DecisionTimeError(
            f"day-ahead prices for {target_day.isoformat()} publish at "
            f"{_iso(published)} but the decision is made at {_iso(decision_ms)}: "
            f"planning that day from this decision time would read an unpublished auction "
            f"({PRICE_PUBLICATION_RULE_ID})"
        )


def _iso(ts_utc_ms: int) -> str:
    """A Berlin-local ISO stamp, so an error reads in the timezone the rules are stated in."""
    return datetime.fromtimestamp(ts_utc_ms / 1000, BERLIN).isoformat()
