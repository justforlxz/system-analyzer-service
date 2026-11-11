"""失败后去向的测试。"""

import pytest

from system_analyzer.jobs.models import JobStatus, status_after_failure


@pytest.mark.parametrize(
    ("attempts", "max_attempts", "expected"),
    [
        (1, 3, JobStatus.QUEUED),
        (2, 3, JobStatus.QUEUED),
        (3, 3, JobStatus.FAILED),
        (4, 3, JobStatus.FAILED),
    ],
)
def test_status_after_failure(attempts: int, max_attempts: int, expected: JobStatus) -> None:
    assert status_after_failure(attempts, max_attempts) is expected
