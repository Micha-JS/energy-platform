"""Shared skip/fail policy for the guards that need a built warehouse.

Both tests in this package are meaningless without ``dbt build`` artefacts, and both skip when
those are absent so ``just test`` stays green without a warehouse. CI sets ``ENERGY_REQUIRE_DBT=1``
after its ``dbt build`` step, which turns the skip into a failure: a guard that silently did not
run is indistinguishable from one that passed.

The policy lives here rather than in each test so the two cannot drift -- one honouring the flag
and the other skipping under it is exactly the hole the flag exists to close.
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest

REQUIRE_DBT_ENV = "ENERGY_REQUIRE_DBT"


def require_dbt() -> bool:
    """Whether a missing warehouse must fail rather than skip."""
    return bool(os.environ.get(REQUIRE_DBT_ENV))


def skip_or_fail(reason: str) -> NoReturn:
    """Skip for a missing warehouse -- unless ``ENERGY_REQUIRE_DBT`` says it must be there."""
    if require_dbt():
        pytest.fail(f"{REQUIRE_DBT_ENV} is set but {reason}")
    pytest.skip(reason)
