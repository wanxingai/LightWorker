import pytest
from importer import parse_number


def test_empty_value_is_missing():
    assert parse_number("") is None


def test_invalid_non_empty_value_still_fails():
    with pytest.raises(ValueError):
        parse_number("not-a-number")
