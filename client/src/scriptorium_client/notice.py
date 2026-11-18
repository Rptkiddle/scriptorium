"""The notice a member agrees to before Scriptorium answers their agent, once
in every agent session: what Scriptorium receives and records.

The client asks for it itself, through the agent app's own dialog (MCP
elicitation), so the answer comes from the member, not from the agent.
Agent apps show only the first lines of such a dialog, so the dialog holds
a three-line DIALOG with Accept and Decline; the full NOTICE is printed in
the terminal by `scriptorium-client notice` and by `setup`. A session in
which the member has not accepted gets no Scriptorium results; the next
request asks again.

Some agent apps cannot show such a dialog. For those the member accepts once
in a terminal (`scriptorium-client notice --accept`); the client's settings
file then records the date and which text was accepted, and the client
answers in those apps without asking. A changed notice text makes the
record void, so the member accepts the new text.
"""

import hashlib

DIALOG = """\
Welcome to Scriptorium! Your agent conversations are yours alone and never sent to us.
We do log your agent's requests (e.g., search terms) and our responses (e.g., records), for improvement.
PDFs you donate are stripped of metadata (e.g., highlights) and kept only if they match a published work (e.g., by DOI)."""

NOTICE = """\
Welcome to Scriptorium!

Your conversations with your agent are yours and yours alone. They are
never transmitted to Scriptorium.

What Scriptorium does receive and record:

  • Requests your agent sends to Scriptorium (such as search terms) and
    our response, with the time, your user name and your agent's
    identity. Your agent may use your own words in a search. Your
    network address is not recorded.

  • Only the server's administrators can see these records. We use them
    to understand how Scriptorium is used and to fix problems.

  • If you donate a PDF, we receive only the file and its DOI or arXiv
    ID. We remove highlights, notes and file metadata before storing it.
    Donated PDFs are shared with all members, so please only donate
    items that are appropriate to share (do not donate non-public
    records). If Scriptorium is unable to reconcile a donated item with
    a public identifier, it will be discarded.

Agree by choosing Accept when Scriptorium asks, at the start of each
agent session."""

NOT_AGREED = ("Scriptorium answers only after the member accepts its notice, once per session,"
              " and they have not accepted it in this session. The next Scriptorium request"
              " asks them again; `scriptorium-client notice` shows the text.")
CANNOT_ASK = ("Scriptorium answers only after the member accepts its notice, and this agent app"
              " cannot show the member the question. The member can accept it once in a terminal"
              " instead: `scriptorium-client notice --accept` (the same command line that starts"
              " the client, with `notice --accept` in place of `serve`). The agent cannot accept"
              " it for them.")


def text_id() -> str:
    """A short fingerprint of the notice text, to tell which text was accepted."""
    return hashlib.sha256(NOTICE.encode()).hexdigest()[:12]


def record(day: str) -> str:
    """The settings value that records an acceptance of the current text on `day`."""
    return f"{day} {text_id()}"


def accepted(settings: dict) -> bool:
    """Whether the settings record an acceptance of the current notice text."""
    return str(settings.get("notice_accepted", "")).endswith(" " + text_id())
