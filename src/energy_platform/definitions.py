"""Dagster code location entrypoint.

M0 ships an empty ``Definitions`` so the stack boots with a healthy, empty code
location. Assets, schedules, and sensors will move into an
``energy_platform.orchestration`` subpackage starting at M1.
"""

from dagster import Definitions

defs = Definitions()
