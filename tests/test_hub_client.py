"""The shared filter arguments produce the hub's filters dict."""

import argparse

from scriptorium.hub_client import add_filter_args, filters_from


def _parse(argv):
    ap = argparse.ArgumentParser()
    add_filter_args(ap)
    return filters_from(ap.parse_args(argv))


def test_no_filters_is_empty():
    assert _parse([]) == {}


def test_every_filter_maps_to_its_key():
    assert _parse(["--creator", "Watts", "--creator", "Duncan", "--year", "2017",
                   "--year-from", "2010", "--year-to", "2020", "--venue", "Nature"]) == {
        "creators": ["Watts", "Duncan"], "years": [2017],
        "year_from": 2010, "year_to": 2020, "venue": "Nature"}
