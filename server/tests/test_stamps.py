"""Download stamps are removed from passage text; text that only says where
data came from stays. The examples follow the shapes found in real papers,
with made-up institutions, names and addresses."""

import pytest

from scriptorium_server.stamps import strip_stamps

REMOVED = [
    # site, downloader, date, network address
    "Downloaded from https://www.pnas.org by EXAMPLE UNIVERSITY; MEDICAL LIBRARY on"
    " August 24, 2026 from IP address 192.0.2.10.",
    "Downloaded from https://www.science.org at Example University on October 10, 2026",
    "Downloaded from https://www.science.org at Example University onOctober 10, 2026",
    "Downloaded from academic.oup.com/joc/article/75/6/423/8246330 by guest on 12 August 2026",
    "Downloaded from academic.oup.com/hcr/article/46/2-3/343/5812646 by Example University,"
    " Science Library user on 25 October 2026",
    "Downloaded from www. mitpressjournals. org/ doi/ pdf/ 10. 1162/ coli. a. 648 by Example"
    " University user on 15 October 2026",
    "Downloaded from https://onlinelibrary.wiley.com/doi/10.1002/poi3.238 by Jane Doe -EXAMPLE"
    " UNIVERSITY , Wiley Online Library on [24/08/2026]. See the Terms and Conditions"
    " (https://onlinelibrary.wiley.com/terms-and-conditions) on Wiley Online Library for rules"
    " of use; OA articles are governed by the applicable Creative Commons License",
    "Authorized licensed use limited to: Example University. Downloaded on August 23,2026 at"
    " 16:48:54 UTC from IEEE Xplore. Restrictions apply.",
    "Downloaded from https://www.cambridge.org/core. IP address: 192.0.2.85, on 12 Aug 2026 at"
    " 08:44:47, subject to the Cambridge Core terms of use, available at"
    " https://www.cambridge.org/core/terms.",
    "Downloaded from www.annualreviews.org. Guest (guest) IP: 192.0.2.85 On: Wed, 12 Aug 2026"
    " 08:52:37",
    "Downloaded from www.annualreviews.org Access provided by Example University on 10/19/20."
    " For personal use only.",
    # the ends of stamps whose opening GROBID put in another sentence
    "Guest (guest) IP: 192.0.2.25 On: Sun, 25 Oct 2026 18:20:50",
    "Oct 2026 at 15:59:29, subject to the Cambridge Core terms of use, available at"
    " https://www. cambridge.org/core/terms.",
]


@pytest.mark.parametrize("stamp", REMOVED)
def test_a_stamp_alone_leaves_nothing(stamp):
    assert strip_stamps(stamp) == ""


def test_a_stamp_inside_a_paragraph_is_cut_out_and_the_rest_kept():
    text = ("We extend the framework of earlier work (16) by Downloaded from"
            " academic.oup.com/pnasnexus/article/3/6/pgae223/7688434 by guest on 12 August 2026"
            " allowing links to be rewired.")
    assert strip_stamps(text) == ("We extend the framework of earlier work (16) by allowing"
                                  " links to be rewired.")


def test_a_stamp_printed_twice_goes_twice():
    stamp = "Downloaded from academic.oup.com/ct/article/36/2/106/8195864 by guest on 12 August 2026"
    assert strip_stamps(f"Sadler {stamp} {stamp} Public sphere") == "Sadler Public sphere"


KEPT = [
    "Downloaded from https://github.com/example/moral_stories",
    "Downloaded from https://dumps.wikimedia.org/enwiki/.",
    "Tweets were downloaded from Twitter on 3 May 2021 using the academic API.",
    "Downloaded on 21 June 2023. https://www.example.org/ukraine-support-tracker",
    "Annotations and metadata are shared, subject to the terms of use of the original sources.",
    "The corpus is used subject to the Common Crawl's terms of use.",
    "A domain name (like www.example.com) maps to an IP address (like 192.0.2.1).",
]


@pytest.mark.parametrize("text", KEPT)
def test_text_naming_no_downloader_stays(text):
    assert strip_stamps(text) == text
