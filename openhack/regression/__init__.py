"""
Security regression test generation for verified vulnerabilities.

When enabled (``regression_tests_enabled``), OpenHack turns each eligible
sandbox-verified finding into a test written in the target repository's own
framework, and accepts it only after proving in the Docker sandbox that it
fails on the vulnerable code and passes on the remediated code.

Entry point: ``openhack.regression.pipeline.RegressionTestPipeline``.
See docs/REGRESSION_TESTS.md.
"""

from .models import RegressionTestResult, RegressionTestStatus, SecurityInvariant

__all__ = ["RegressionTestResult", "RegressionTestStatus", "SecurityInvariant"]
