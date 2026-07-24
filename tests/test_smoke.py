"""Smoke tests: the package imports and the Dagster code location loads."""

from energy_platform import __version__
from energy_platform.definitions import defs


def test_version_is_defined() -> None:
    assert __version__ == "0.0.0"


def test_definitions_loads() -> None:
    # An empty but valid code location must be constructable.
    assert defs is not None
