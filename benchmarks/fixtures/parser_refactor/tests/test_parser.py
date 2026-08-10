import pytest
from parser import parse_pair


def test_pair_is_trimmed():
    assert parse_pair(" key = value ") == ("key", "value")


@pytest.mark.parametrize("value", ["missing", "=value", "key="])
def test_invalid_pairs(value):
    with pytest.raises(ValueError):
        parse_pair(value)
