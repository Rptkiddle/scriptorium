"""Citation keys: collisions get a suffix."""

from scriptorium_server.citekeys import generate_citekey, unique_key


def test_citekey_collision_suffixes():
    data = {"creators": [{"creatorType": "author", "lastName": "Hopper"}],
            "title": "Compiling Very Fast Programs", "date": "2026"}
    taken: set = set()
    first = generate_citekey(data, taken)
    taken.add(first)
    second = generate_citekey(data, taken)
    assert first != second
    assert second.startswith(first)


def test_unique_key_goes_past_z():
    taken = {"k"} | {"k" + c for c in "abcdefghijklmnopqrstuvwxyz"}
    assert unique_key("k", taken) == "kaa"
