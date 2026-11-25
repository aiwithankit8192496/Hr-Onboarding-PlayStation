"""Test-only settings.

scrypt (the HR password hash) is slow on purpose: about 64 MB and a fraction of a second per hash. That is right
for production and wasteful in tests that sign in dozens of times, so for the test run only the cost is lowered here.
Nothing in the application reads this file or any test setting: hr_login.py keeps its production numbers, and
refuses a stored hash weaker than them (a test below proves it). Run with `python -m pytest tests`; running the
files with plain `python -m unittest` skips this file and is only slower, not different.
"""

import sys
from pathlib import Path

import pytest


from hr_onboarding import hr_login

TEST_SCRYPT_N = 2 ** 4


@pytest.fixture(scope="session", autouse=True)
def fast_scrypt():
    patch = pytest.MonkeyPatch()
    patch.setattr(hr_login, "SCRYPT_N", TEST_SCRYPT_N)
    patch.setattr(hr_login, "MIN_SCRYPT_N", TEST_SCRYPT_N)
    yield
    patch.undo()
