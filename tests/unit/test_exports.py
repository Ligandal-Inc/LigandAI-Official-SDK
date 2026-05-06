# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Top-level package exports."""

from __future__ import annotations

from ligandai import LigandAIJobError, LigandAITimeoutError
from ligandai.errors import LigandAIJobError as ErrorsLigandAIJobError
from ligandai.errors import LigandAITimeoutError as ErrorsLigandAITimeoutError


def test_job_errors_are_top_level_exports() -> None:
    assert LigandAIJobError is ErrorsLigandAIJobError
    assert LigandAITimeoutError is ErrorsLigandAITimeoutError
