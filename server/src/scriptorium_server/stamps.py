"""Download stamps: the line a publisher prints on every page of a PDF when
someone downloads it, saying who downloaded it, when, and sometimes from
which network address. It is not the paper's text, and it can name the
person or place that downloaded the copy, so it is removed from passages.

A stamp is recognised by its shape, not by publisher:

- it opens with "Downloaded from <site>" or "Downloaded on", optionally
  preceded by "Authorized licensed use limited to: <institution>.";
- it names the downloader: "by <who>", "at <who>", an IP address,
  "Access provided by <who>", or a guest;
- it ends with the date of the download, optionally followed by a time
  and the publisher's terms-of-use sentence ("subject to ... terms of
  use", "See the Terms and Conditions ...", "Restrictions apply.").

An access line ("Guest (guest) IP: <address> On: <date>") whose opening
GROBID put elsewhere is removed too. A terms-of-use sentence whose opening
GROBID put elsewhere is removed as
well when its shape shows it belongs to a stamp: a download date and time
before it ("15 Oct 2026 at 16:20:01, subject to the ... terms of use"),
or "available at" after it.

Text that only says where data came from ("Downloaded from
https://github.com/...", "downloaded from Twitter on 3 May 2021") names no
downloader and stays.
"""

import re

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
          r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DATE = (rf"(?:{_MONTH}\s?\d{{1,2}},\s?\d{{4}}"        # August 24, 2026 / October 25,2026
         rf"|(?<![\d/])\d{{1,2}}\s{_MONTH}\s\d{{4}}"  # 12 August 2026 / 15 Oct 2026
         # [24/08/2026] / 10/19/20, standing alone (not part of a path such as 75/6/423)
         r"|(?<![\w/])\[?\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})\]?(?![\w/]))")
_TIME = r"(?:,?\s*(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:UTC|GMT))?)"
# a web address, also when the PDF broke it after "www." ("https://www. cambridge.org/...")
_URL = r"https?://(?:www\.\s)?\S*[^\s.,;)]"
_TERMS = (r"(?:"
          # ", subject to the Cambridge Core terms of use, available at <url>."
          rf",?\s*subject to the [^.;]{{0,60}}?terms of use(?:,\s*available(?: at)?(?:\s+{_URL})?)?\.?"
          # ". See the Terms and Conditions (<url>) on <site> for rules of use; OA articles are
          #  governed by the applicable Creative Commons License"
          rf"|\.?\s*See the Terms and Conditions\s*\({_URL}\)\s*on [^.;]{{0,60}}? for rules of use"
          r"(?:;\s*OA articles are governed by the applicable Creative Commons License\.?)?"
          r"|\s*from IP address \d{1,3}(?:\.\d{1,3}){3}\.?"
          r"|\s*from IEEE Xplore\.\s*Restrictions apply\.?"
          r"|\.?\s*For personal use only\.?)")
_WHO = r"(?:\s(?:by|at)\s|IP(?: address)?:?\s|Access provided by|\(guest\)|\bguest\b)"

STAMP = re.compile(
    r"(?:Authori[sz]ed licensed use limited to:[^.]{1,120}\.\s*)?"
    rf"Downloaded (?:from|on)\b(?P<mid>.{{0,320}}?){_DATE}(?:{_TIME})?(?:{_TERMS})*",
    re.DOTALL)
ORPHAN_TERMS = re.compile(
    # with the date (its day may be missing) and time of the download before it ...
    rf"\b(?:\d{{1,2}}\s)?{_MONTH}\s\d{{4}}{_TIME},?\s*subject to the [^.;]{{0,60}}?terms of use"
    rf"(?:,\s*available(?: at)?(?:\s+{_URL})?)?\.?"
    # ... or "available at" after it
    rf"|,?\s*subject to the [^.;]{{0,60}}?terms of use,\s*available(?: at)?(?:\s+{_URL})?\.?"
    rf"|See the Terms and Conditions\s*\({_URL}\)\s*on [^.;]{{0,60}}? for rules of use"
    r"(?:;\s*OA articles are governed by the applicable Creative Commons License\.?)?")
_LONE_LICENCE = re.compile(r"Authori[sz]ed licensed use limited to:[^.]{1,120}\.")
# An access line whose "Downloaded from <site>" GROBID put elsewhere:
# "Guest (guest) IP: 192.0.2.1 On: Sun, 25 Oct 2026 18:20:50".
_LONE_ACCESS = re.compile(rf"(?:\S+\s\([^)]{{1,40}}\)\s*)?IP:\s*\d{{1,3}}(?:\.\d{{1,3}}){{3}}"
                          rf"\s*On:\s*\w{{3}},\s*{_DATE}{_TIME}?")
_WHO_RE = re.compile(_WHO)


def _stamp(match: re.Match) -> str:
    """The replacement for a candidate: nothing when it names a downloader
    (or comes with a terms-of-use tail); otherwise the text unchanged."""
    whole = match.group(0)
    if _WHO_RE.search(match.group("mid")) or "Authori" in whole[:12] or re.search(
            r"(?:terms of use|Terms and Conditions|IEEE Xplore|IP address|personal use)", whole):
        return " "
    return whole


def strip_stamps(text: str) -> str:
    """The text without download stamps, whitespace tidied; "" when nothing
    else was left."""
    if not re.search(r"ownloaded|terms of use|Terms and Conditions|licensed use|IP: ", text):
        return text
    out = STAMP.sub(_stamp, text)
    out = ORPHAN_TERMS.sub(" ", out)
    out = _LONE_LICENCE.sub(" ", out)
    out = _LONE_ACCESS.sub(" ", out)
    return " ".join(out.split())
