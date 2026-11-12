"""The client against a fake server: the requests it sends match the
contract, large results go to a file, sets round-trip, and failures reach
the agent as one clear sentence."""

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from scriptorium_client import __version__, results, settings
from scriptorium_client.http import Server, ServerError
from scriptorium_client.tools import TOOL_NAMES, Tools, build_server

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "api" / "openapi.json").read_text())


def many(n):
    return [{"key": f"K{i:04d}", "title": f"Paper {i} " + "words " * 20, "year": 2020 + i % 5}
            for i in range(n)]


class FakeServer:
    """Answers like the server and records what it was sent."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.status, self.error = 200, None
        self.replies = {
            "/v1/health": {"status": "ok", "server": "0.1.0", "api": 1, "min_client": "0.1.0",
                           "items": 25355, "fulltexts": 14839},
            "/v1/items": {"total": 300, "items": many(300)},
            "/v1/search/items": {"items": many(3)},
            "/v1/search/passages": {"passages": [{"item": "K0001", "passage": 4, "text": "t"}]},
            "/v1/lookup": {"results": []}, "/v1/facets": {"field": "year", "values": [], "more": False},
            "/v1/bibtex": {"format": "biblatex", "entries": 2, "bibtex": "@article{a,\n}\n", "missing": []},
            "/v1/items/K0001": {"key": "K0001"},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, dict(request.headers)))
        if self.error:
            raise self.error
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "the server's own words"})
        return httpx.Response(200, json=self.replies[request.url.path])


@pytest.fixture
def fake():
    return FakeServer()


@pytest.fixture
def tools(fake, tmp_path):
    config = {"server": "https://scriptorium.test", "api_key": "scr_key", "reply_chars": 4000,
              "results_dir": str(tmp_path / "results"), "pdf_dir": str(tmp_path / "pdfs")}
    server = Server(config, agent="test-agent", transport=httpx.MockTransport(fake))
    return Tools(config, server, zotero=lambda: {"zotero_running": True, "bridge_plugin": False})


def test_each_tool_sends_the_request_in_the_contract(tools, fake):
    filters = {"authors": ["Trilling"], "year_from": 2021}
    tools.lookup(["10.1000/a"])
    tools.list_items(filters, count=5, order="year", fields=["key", "title"])
    tools.search_items("echo chambers", filters, count=3, phrase="filter bubble")
    tools.search_passages("echo chambers", count=2)
    tools.item("K0001")
    tools.facets("year", filters, contains="20")
    tools.bibtex(["K0001"], "bibtex", True)
    tools.status()
    paths = CONTRACT["paths"]
    schemas = CONTRACT["components"]["schemas"]
    for method, path, body, headers in fake.calls:
        template = "/v1/items/{key}" if path.startswith("/v1/items/") else path
        operation = paths[template][method.lower()]  # the address and method exist
        if body is not None:
            ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            allowed = schemas[ref.rsplit("/", 1)[1]]["properties"]
            assert set(body) <= set(allowed), (path, body)
            if "filters" in body:
                assert set(body["filters"]) <= set(schemas["Filters"]["properties"])
        assert headers["authorization"] == "Bearer scr_key"
        assert headers["x-scriptorium-client"] == __version__
        assert headers["x-scriptorium-agent"] == "test-agent"
    assert len(fake.calls) == 8 and len({c[1] for c in fake.calls}) == 8  # one request per tool
    assert fake.calls[1][2] == {"filters": filters, "count": 5, "offset": 0, "order": "year",
                                "fields": ["key", "title"]}  # same inputs, empty ones left out


def test_every_tool_is_registered_with_a_description():
    async def listed():
        async with Client(build_server(Tools({"server": "", "api_key": "", "reply_chars": 1,
                                              "results_dir": "", "pdf_dir": ""}))) as client:
            return await client.list_tools()

    found = {tool.name: tool.description for tool in asyncio.run(listed())}
    assert set(found) == set(TOOL_NAMES)
    assert all(description and len(description) > 40 for description in found.values())


def test_large_result_goes_to_a_file(tools, fake):
    small = tools.search_items("echo chambers")
    assert small == fake.replies["/v1/search/items"]  # fits: returned whole
    large = tools.list_items(count=300)
    saved = Path(large["saved_to"])
    assert large["count"] == 300 and large["total"] == 300 and 0 < len(large["items"]) < 300
    assert len(json.dumps(large)) < 4000 + 600  # the reply stays near the budget
    stored = json.loads(saved.read_text())
    assert len(stored["items"]) == 300 and stored["request"]["count"] == 300
    assert saved.parent == Path(tools.settings["results_dir"]) and "set_read" in large["note"]


def test_set_file_round_trip(tools, fake):
    path = tools.list_items(count=300)["saved_to"]
    page = tools.set_read(path, page=2, page_size=50)
    assert (page["count"], page["pages"], page["items"][0]["key"]) == (300, 6, "K0050")
    assert page["items"][0]["mark"] is None
    tools.set_mark(path, ["K0002"], included=False)
    assert len(results.set_keys(path)) == 299  # all but the excluded one
    marked = tools.set_mark(path, ["K0000", "K0001", "NOPE"], included=True)
    assert (marked["marked"], marked["not_in_set"], marked["included"], marked["excluded"]) == (
        2, ["NOPE"], 2, 1)
    assert results.set_keys(path) == ["K0000", "K0001"]  # now the included ones
    assert tools.set_read(path)["items"][2]["mark"] == "excluded"

    tools.search_items("more on this", within_set=path)  # the set as a filter
    assert fake.calls[-1][2]["filters"] == {"item_keys": ["K0000", "K0001"]}
    exported = tools.set_export_bibtex(path)
    assert fake.calls[-1][1:3] == ("/v1/bibtex", {"keys": ["K0000", "K0001"], "format": "biblatex",
                                                  "abstracts": False})
    assert Path(exported["saved_to"]).read_text().startswith("@article{a") and exported["entries"] == 2


def test_old_client_gets_one_clear_message(tools, fake):
    """Review focus 4: a client older than the server's API."""
    fake.status = 426
    calls = [lambda: tools.lookup(["x"]), lambda: tools.list_items(), lambda: tools.search_items("q"),
             lambda: tools.search_passages("q"), lambda: tools.item("K0001"),
             lambda: tools.facets("year"), lambda: tools.bibtex(["K0001"])]
    messages = set()
    for call in calls:
        with pytest.raises(ServerError) as failure:
            call()
        messages.add(str(failure.value))
    assert messages == {"This Scriptorium client is too old for the server: the server's own"
                        " words. Update the client, then restart the agent."}


def test_other_failures_are_plain_sentences(tools, fake):
    fake.status = 401
    with pytest.raises(ServerError, match="does not accept this API key. Run `scriptorium-client setup`"):
        tools.list_items()
    fake.status = 429
    with pytest.raises(ServerError, match=r"refused the request \(429\): the server's own words"):
        tools.list_items()
    fake.status, fake.error = 200, httpx.ConnectError("no route")
    with pytest.raises(ServerError, match="cannot be reached .* connect the university VPN"):
        tools.list_items()
    with pytest.raises(ServerError, match="No server is set up yet"):
        Tools({"server": "", "api_key": "", "reply_chars": 1, "results_dir": "", "pdf_dir": ""}).item("K")


def test_settings_file_is_private(tmp_path):
    path = settings.save({"server": "https://scriptorium.test", "api_key": 'scr_"key"',
                          "reply_chars": 24000}, tmp_path / "config" / "client.toml")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = settings.load(path)
    assert (loaded["server"], loaded["api_key"], loaded["reply_chars"]) == (
        "https://scriptorium.test", 'scr_"key"', 24000)
    assert loaded["results_dir"].endswith("results") and "reply_chars" not in path.read_text()
    assert settings.load(tmp_path / "missing.toml")["server"] == ""


def test_status_reports_all_four_facts(tools, fake):
    status = tools.status()
    assert status["server_reachable"] is True and status["items"] == 25355
    assert status["client_version"] == __version__
    assert status["zotero_running"] is True and status["bridge_plugin"] is False
    fake.error = httpx.ConnectError("no route")
    down = tools.status()
    assert down["server_reachable"] is False and "cannot be reached" in down["server_problem"]
    assert down["zotero_running"] is True  # the local facts are still reported


async def agree(message, response_type, params, context):
    return {"value": "yes"}


def test_the_agents_name_reaches_the_server(tools, fake):
    tools.server.agent = ""

    async def call():
        async with Client(build_server(tools), elicitation_handler=agree) as client:
            return await client.call_tool("item", {"key": "K0001"})

    reply = asyncio.run(call())
    assert reply.data == {"key": "K0001"}
    assert fake.calls[-1][3].get("x-scriptorium-agent")  # the MCP session's client name



def test_each_session_asks_the_member_once_before_any_request(tools, fake):
    asked = []

    async def answer_with(word):
        async def handler(message, response_type, params, context):
            asked.append(message)
            return {"value": word}
        return handler

    async def session(word, calls=2):
        async with Client(build_server(tools), elicitation_handler=await answer_with(word)) as client:
            return [await client.call_tool("item", {"key": "K0001"}, raise_on_error=False)
                    for _ in range(calls)]

    replies = asyncio.run(session("yes"))
    assert [r.data for r in replies] == [{"key": "K0001"}] * 2
    assert len(asked) == 1 and asked[0].startswith("Welcome to Scriptorium!")
    assert asked[0].rstrip().endswith('Type "yes" to agree and continue:')
    sent = len(fake.calls)
    replies = asyncio.run(session("no thanks"))  # a new session asks again
    assert len(asked) == 3  # it asked at both calls, since the member did not agree
    assert all(r.is_error and "agrees to its notice" in str(r.content) for r in replies)
    assert len(fake.calls) == sent  # nothing reached the server


def test_an_agent_app_that_cannot_ask_gets_no_results(tools, fake):
    async def call():
        async with Client(build_server(tools)) as client:  # no elicitation support
            return await client.call_tool("status", {}, raise_on_error=False)

    try:  # the agent app itself reports that it cannot show the question
        reply = asyncio.run(call())
        assert reply.is_error
    except Exception as exc:  # noqa: BLE001
        assert "not supported" in str(exc).lower()
    assert fake.calls == []  # nothing reached the server



def test_calls_at_once_ask_once(tools, fake):
    asked = []

    async def handler(message, response_type, params, context):
        asked.append(1)
        await asyncio.sleep(0.05)
        return {"value": "yes"}

    async def session():
        async with Client(build_server(tools), elicitation_handler=handler) as client:
            return await asyncio.gather(*[client.call_tool("item", {"key": "K0001"}) for _ in range(3)])

    replies = asyncio.run(session())
    assert [r.data for r in replies] == [{"key": "K0001"}] * 3 and len(asked) == 1


@pytest.mark.parametrize("word, agreed", [("yes", True), ("no", False)])
def test_an_older_connection_asks_the_member_directly(tools, fake, word, agreed):
    asked = []

    async def handler(message, response_type, params, context):
        asked.append(message)
        return {"value": word}

    async def session():
        async with Client(build_server(tools), elicitation_handler=handler, mode="legacy") as client:
            return await client.call_tool("item", {"key": "K0001"}, raise_on_error=False)

    reply = asyncio.run(session())
    assert len(asked) == 1 and asked[0].startswith("Welcome to Scriptorium!")
    assert (not reply.is_error) == agreed
