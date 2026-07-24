"""Dagster orchestration: partitions, resources, assets, schedules, and the raw zone.

Both the Dagster assets and the backfill CLI drive the same idempotent ingestion core
(:func:`energy_platform.orchestration.ingest.ingest_partition`) against the append-only
Postgres raw zone.
"""
