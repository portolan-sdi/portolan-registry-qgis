"""Hypothesis profiles for the property tests."""

from __future__ import annotations

import os

from hypothesis import settings

settings.register_profile("default", max_examples=20, deadline=None)
settings.register_profile("ci", max_examples=20, deadline=None, derandomize=True)
settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE", "ci" if os.environ.get("CI") else "default")
)
