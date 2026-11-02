"""The dataset source, executor, environment and artifact storage boundaries.

Business code only sees these platform types. Hub and Harbor formats stop at
the adapters; every boundary also has a fake in ``maestro.testing.fakes``.
"""
