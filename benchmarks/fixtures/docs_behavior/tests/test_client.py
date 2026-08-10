from client import Client


def test_documented_default_timeout():
    assert Client().timeout == 10


def test_explicit_timeout():
    assert Client(timeout=3).timeout == 3
