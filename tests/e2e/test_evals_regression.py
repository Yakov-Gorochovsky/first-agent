"""
tests/e2e/test_evals_regression.py - End-to-end regression evaluation test suite executing all 11 evaluation benchmarks.
"""

from typing import Callable

import pytest

from evals import DETERMINISTIC_SUITE


@pytest.mark.e2e
@pytest.mark.parametrize(
    "eval_name, eval_fn",
    DETERMINISTIC_SUITE,
    ids=[name for name, _ in DETERMINISTIC_SUITE],
)
def test_deterministic_eval_case(eval_name: str, eval_fn: Callable[[], str]) -> None:
    """Each evaluation in the deterministic suite must pass without raising exceptions."""
    result = eval_fn()
    assert result is not None
    assert isinstance(result, str)
    assert len(result) > 0
