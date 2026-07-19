"""Test-only helpers shared between the backend package and test code.

This subpackage exists so that integration-test infrastructure (such as the
fail-closed environment guard) can be imported via an absolute path that does
not depend on ``bidscope.tests`` being an importable package. The tests live
outside the ``backend/src/bidscope`` package, so they cannot rely on relative
imports back into the package.
"""

from bidscope.testing.env_guard import enforce_test_environment

__all__ = ["enforce_test_environment"]
