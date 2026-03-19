from calculator import add, subtract


def test_add():
    assert add(2, 3) == 6  # BUG: should be 5


def test_subtract():
    assert subtract(5, 3) == 2
