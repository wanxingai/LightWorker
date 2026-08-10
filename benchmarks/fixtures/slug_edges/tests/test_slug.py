from slug import slugify


def test_simple_words():
    assert slugify("Hello World") == "hello-world"


def test_surrounding_and_repeated_whitespace():
    assert slugify("  Hello   World  ") == "hello-world"
