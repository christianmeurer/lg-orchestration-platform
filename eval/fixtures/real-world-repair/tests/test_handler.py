import pytest
from handler import compute_rate, format_report


def test_compute_rate_normal():
    assert compute_rate(100, 10.0) == 10.0


def test_compute_rate_zero_duration():
    with pytest.raises(ZeroDivisionError):
        compute_rate(100, 0.0)  # This currently raises — fix should return 0.0 or raise ValueError
