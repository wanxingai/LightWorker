import pytest
from pagination import offset


def test_valid_offset():
    assert offset(3, 20) == 40


@pytest.mark.parametrize("page,page_size", [(0, 10), (-1, 10), (1, 0), (1, -5)])
def test_requires_positive_values(page, page_size):
    with pytest.raises(ValueError):
        offset(page, page_size)
