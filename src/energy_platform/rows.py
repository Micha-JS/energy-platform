"""Narrowing helpers for psycopg row values.

psycopg returns ``object`` for a row element, and the numeric type it hands back depends on the
column's declared type rather than on anything the caller controls: ``double precision`` arrives as
``float``, ``numeric`` as ``Decimal``. Every reader that pulls money or energy out of the warehouse
therefore has to narrow, and each one used to carry its own copy of this function -- four of them,
all with the same bug: a guard that excluded the ``Decimal`` its own docstring named.

One definition, shared by the source and the tests, so a cast introduced upstream cannot make three
of the four copies right and the fourth wrong.
"""

from __future__ import annotations

from decimal import Decimal


def as_float(value: object) -> float | None:
    """Narrow a psycopg value to ``float | None`` -- numeric columns arrive as Decimal.

    The assertion carries the offending value because the failure it guards against is remote from
    its cause: adding a ``::numeric`` cast to a dbt model is what breaks this, and a bare
    ``AssertionError`` three layers away is a much worse clue than the repr of what arrived.
    """
    if value is None:
        return None
    assert isinstance(value, int | float | str | Decimal), f"not a numeric row value: {value!r}"
    return float(value)


def as_required_float(value: object) -> float:
    """``as_float`` for a NOT NULL column, where ``None`` is itself the defect."""
    narrowed = as_float(value)
    assert narrowed is not None, "expected a value from a NOT NULL column, got NULL"
    return narrowed
