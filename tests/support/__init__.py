"""Shared, typed test helpers (fakes / builders) for the suite.

Kept separate from ``tests/conftest.py`` (which owns pytest fixtures and the
Redis skip helper) so the fakes can be imported explicitly by the tests that
want them without pytest fixture magic.
"""
