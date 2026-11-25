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
from scriptorium_client.tools import ADMIN_TOOL_NAMES, TOOL_NAMES, Tools, build_server

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
                           "pdf_download": True, "pdf_upload": False,
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


@pytest.fixture(autouse=True)
def own_settings_file(tmp_path, monkeypatch):
    """Every test reads and writes a settings file of its own, never the user's."""
    monkeypatch.setenv("SCRIPTORIUM_CLIENT_SETTINGS", str(tmp_path / "client.toml"))
    for name in settings.DEFAULTS:
        monkeypatch.delenv(settings.variable(name), raising=False)


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
    filters = {"authors": ["Terpstra"], "year_from": 2021}
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
    with pytest.raises(ServerError, match=r"does not accept this API key \(the server's own words\). Run `scriptorium-client setup`"):
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
    assert status["pdf_download"] is True and status["pdf_upload"] is False
    assert status["zotero_running"] is True and status["bridge_plugin"] is False
    fake.error = httpx.ConnectError("no route")
    down = tools.status()
    assert down["server_reachable"] is False and "cannot be reached" in down["server_problem"]
    assert down["zotero_running"] is True  # the local facts are still reported


async def agree(message, response_type, params, context):
    return {}


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
        from fastmcp.client.elicitation import ElicitResult

        async def handler(message, response_type, params, context):
            asked.append(message)
            return {} if word == "yes" else ElicitResult(action="decline")
        return handler

    async def session(word, calls=2):
        async with Client(build_server(tools), elicitation_handler=await answer_with(word)) as client:
            return [await client.call_tool("item", {"key": "K0001"}, raise_on_error=False)
                    for _ in range(calls)]

    replies = asyncio.run(session("yes"))
    assert [r.data for r in replies] == [{"key": "K0001"}] * 2
    assert len(asked) == 1 and asked[0].startswith("Welcome to Scriptorium!")
    assert len(asked[0].splitlines()) == 3 and "match a published work" in asked[0]
    sent = len(fake.calls)
    replies = asyncio.run(session("decline"))  # a new session asks again
    assert len(asked) == 3  # it asked at both calls, since the member did not agree
    assert all(r.is_error and "accepts its notice" in str(r.content) for r in replies)
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


def test_an_agent_app_that_cannot_ask_is_served_after_acceptance_in_a_terminal(tools, fake):
    from scriptorium_client import notice

    async def call(mode):
        async with Client(build_server(tools), mode=mode) as client:  # no elicitation support
            return await client.call_tool("item", {"key": "K0001"}, raise_on_error=False)

    refused = asyncio.run(call("legacy"))
    assert refused.is_error and "notice --accept" in str(refused.content) and fake.calls == []
    settings.save({"notice_accepted": notice.record("2026-11-18")})
    for mode in ("legacy", "auto"):
        assert asyncio.run(call(mode)).data == {"key": "K0001"}
    settings.save({"notice_accepted": "2026-11-18:anearliertext"})  # the notice text has changed since
    assert asyncio.run(call("legacy")).is_error


def test_a_recorded_acceptance_does_not_replace_the_question_where_the_app_can_ask(tools, fake):
    from scriptorium_client import notice

    settings.save({"notice_accepted": notice.record("2026-11-18")})
    asked = []

    async def handler(message, response_type, params, context):
        asked.append(message)
        return {}

    async def call():
        async with Client(build_server(tools), elicitation_handler=handler) as client:
            return await client.call_tool("item", {"key": "K0001"})

    assert asyncio.run(call()).data == {"key": "K0001"} and len(asked) == 1


def test_calls_at_once_ask_once(tools, fake):
    asked = []

    async def handler(message, response_type, params, context):
        asked.append(1)
        await asyncio.sleep(0.05)
        return {}

    async def session():
        async with Client(build_server(tools), elicitation_handler=handler) as client:
            return await asyncio.gather(*[client.call_tool("item", {"key": "K0001"}) for _ in range(3)])

    replies = asyncio.run(session())
    assert [r.data for r in replies] == [{"key": "K0001"}] * 3 and len(asked) == 1


@pytest.mark.parametrize("agreed", [True, False])
def test_an_older_connection_asks_the_member_directly(tools, fake, agreed):
    from fastmcp.client.elicitation import ElicitResult

    asked = []

    async def handler(message, response_type, params, context):
        asked.append(message)
        return {} if agreed else ElicitResult(action="decline")

    async def session():
        async with Client(build_server(tools), elicitation_handler=handler, mode="legacy") as client:
            return await client.call_tool("item", {"key": "K0001"}, raise_on_error=False)

    reply = asyncio.run(session())
    assert len(asked) == 1 and asked[0].startswith("Welcome to Scriptorium!")
    assert (not reply.is_error) == agreed



def test_the_notice_command_prints_the_full_text():
    from typer.testing import CliRunner

    from scriptorium_client.cli import app

    out = CliRunner().invoke(app, ["notice"]).output
    assert out.startswith("Welcome to Scriptorium!") and "non-public" in out and "Accept" in out


def test_the_notice_is_accepted_only_by_the_member_at_a_terminal(monkeypatch):
    import sys

    from typer.testing import CliRunner

    from scriptorium_client import notice
    from scriptorium_client.cli import app

    settings.save({"server": "https://scriptorium.test", "api_key": "scr_key"})
    piped = CliRunner().invoke(app, ["notice", "--accept"], input="y\n")  # a program answering
    assert piped.exit_code == 1 and not notice.accepted(settings.load())

    class Terminal:
        def __init__(self, stream):
            self._stream = stream

        def isatty(self):
            return True

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def at_terminal(answer):
        class Runner(CliRunner):
            def isolation(self, *args, **kwargs):
                manager = super().isolation(*args, **kwargs)

                class Wrapped:
                    def __enter__(inner):
                        out = manager.__enter__()
                        monkeypatch.setattr(sys, "stdin", Terminal(sys.stdin))
                        return out

                    def __exit__(inner, *exc):
                        return manager.__exit__(*exc)
                return Wrapped()
        return Runner().invoke(app, ["notice", "--accept"], input=answer)

    declined = at_terminal("n\n")
    assert declined.exit_code == 1 and not notice.accepted(settings.load())
    agreed = at_terminal("y\n")
    assert agreed.exit_code == 0 and "Welcome to Scriptorium!" in agreed.output
    stored = settings.load()
    assert notice.accepted(stored) and stored["api_key"] == "scr_key"  # the key is kept
    assert f"SCRIPTORIUM_NOTICE_ACCEPTED={stored['notice_accepted']}" in agreed.output
    assert " " not in stored["notice_accepted"]  # one word, so it fits a KEY=value line


def test_settings_can_come_from_the_environment(monkeypatch):
    from scriptorium_client import notice

    settings.save({"server": "https://file.test", "api_key": "scr_file", "reply_chars": 9000})
    monkeypatch.setenv("SCRIPTORIUM_SERVER", "https://env.test")
    monkeypatch.setenv("SCRIPTORIUM_REPLY_CHARS", "5000")
    monkeypatch.setenv("SCRIPTORIUM_NOTICE_ACCEPTED", notice.record("2026-11-18"))
    values = settings.load()
    assert (values["server"], values["api_key"], values["reply_chars"]) == ("https://env.test", "scr_file", 5000)
    assert notice.accepted(values)
    assert settings.load(environment=False)["server"] == "https://file.test"  # what the file holds
    assert notice.accepted({"notice_accepted": "2026-11-18 " + notice.text_id()})  # the earlier form


def test_a_settings_file_that_cannot_be_read_counts_as_absent(tmp_path, monkeypatch):
    folder = tmp_path / "closed"
    folder.mkdir()
    (folder / "client.toml").write_text('server = "https://file.test"\n')
    (folder / "client.toml").chmod(0)
    monkeypatch.setenv("SCRIPTORIUM_CLIENT_SETTINGS", str(folder / "client.toml"))
    monkeypatch.setenv("SCRIPTORIUM_SERVER", "https://env.test")
    assert settings.load()["server"] == "https://env.test"


# --- the admin menu -------------------------------------------------------------


def served(tools):
    server = build_server(tools)

    async def listed():
        async with Client(server) as client:
            return await client.list_tools()

    return asyncio.run(listed()), server.instructions


def test_the_admin_menu_is_shown_once_the_server_has_said_so(tools, fake, tmp_path):
    listed, instructions = served(tools)  # nothing known yet: no menu, and nothing asked
    assert {t.name for t in listed} == set(TOOL_NAMES) and "admin_overview" not in instructions
    assert fake.calls == [] and not tools.admin_known() and "admin" not in tools.status()
    assert not tools.learn_admin() and not tools.admin_known()  # a plain member
    fake.replies["/v1/health"]["admin"] = True
    assert tools.learn_admin() and tools.admin_known()  # noted for the next session
    assert json.loads((tmp_path / "admin.json").read_text()) == {
        "server": "https://scriptorium.test", "admin": True}
    listed, instructions = served(tools)
    found = {t.name: t.description for t in listed}
    assert set(found) == set(TOOL_NAMES) | set(ADMIN_TOOL_NAMES)
    assert all(found[name].startswith("ADMIN.") for name in ADMIN_TOOL_NAMES)
    assert "confirm=true" in instructions and tools.status()["admin"] is True
    fake.replies["/v1/health"]["admin"] = False  # the mark was taken away
    assert not tools.learn_admin() and not tools.admin_known()


def test_the_note_counts_for_its_server_only_and_the_setting_shows_the_menu(tools, fake):
    fake.replies["/v1/health"]["admin"] = True
    tools.learn_admin()
    tools.server.address = "https://another.test"
    assert not tools.admin_known()
    tools.settings["admin"] = "yes"
    assert tools.admin_known()
    fake.error = httpx.ConnectError("no route")
    assert not tools.learn_admin()  # a server that cannot be asked changes nothing


def test_a_new_admin_gets_the_menu_with_the_first_request_after_the_notice(tools, fake):
    fake.replies["/v1/health"]["admin"] = True

    async def session():
        async def accept(message, response_type, params, context):
            return {}
        async with Client(build_server(tools), elicitation_handler=accept) as client:
            before = {t.name for t in await client.list_tools()}
            await client.call_tool("item", {"key": "K0001"})
            return before, {t.name for t in await client.list_tools()}

    before, after = asyncio.run(session())
    assert before == set(TOOL_NAMES) and after == set(TOOL_NAMES) | set(ADMIN_TOOL_NAMES)
    assert [c[1] for c in fake.calls] == ["/v1/health", "/v1/items/K0001"]  # asked once


def test_each_admin_tool_sends_the_request_in_the_contract(tools, fake):
    for menu in ("settings", "members", "venues", "runs", "jobs"):
        fake.replies[f"/v1/admin/{menu}"] = {"menu": menu}
    fake.replies["/v1/admin/overview"] = {"collection": {}}
    assert tools.admin_overview() == {"collection": {}}
    tools.admin_settings()
    tools.admin_settings("limits.per_request", "500", confirm=True)
    tools.admin_members("add", "hedy")
    tools.admin_venues("add", "Political Communication", source_id="S2", needs_filter=True,
                       confirm=True)
    tools.admin_venues(contains="press", offset=50)
    tools.admin_runs("start", "surveillance", dry_run=True)
    tools.admin_jobs("retry", job_id=7, confirm=True)
    paths, schemas = CONTRACT["paths"], CONTRACT["components"]["schemas"]
    for method, path, body, headers in fake.calls:
        operation = paths[path][method.lower()]
        if body is not None:
            ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            assert set(body) <= set(schemas[ref.rsplit("/", 1)[1]]["properties"]), (path, body)
        assert headers["authorization"] == "Bearer scr_key"
    bodies = [c[2] for c in fake.calls]
    assert bodies[1] == {}  # inputs left at their defaults are not sent
    assert bodies[2] == {"name": "limits.per_request", "value": "500", "confirm": True}
    assert bodies[3] == {"action": "add", "name": "hedy"}  # no confirm: a preview
    assert bodies[4] == {"action": "add", "name": "Political Communication", "source_id": "S2",
                         "needs_filter": True, "count": 50, "confirm": True}
    assert bodies[5] == {"action": "list", "contains": "press", "count": 50, "offset": 50}
    assert bodies[6] == {"action": "start", "kind": "surveillance", "dry_run": True, "count": 10}
    assert bodies[7] == {"action": "retry", "job_id": 7, "count": 20, "confirm": True}


# --- collections ---------------------------------------------------------------


def collection_reply(number=7, **more):
    return {"collection": number, "kind": "active", "name": "Reading", "item_count": 3,
            "new_collection": True, "added": 0, "already_in_it": 0, "not_held": [], **more}


def test_each_collection_tool_sends_the_request_in_the_contract(tools, fake):
    for name in ("list", "show", "add", "record", "remove", "edit"):
        fake.replies[f"/v1/collections/{name}"] = collection_reply(items=many(2))
    tools.collections(kind="history", contains="echo")
    tools.collection("Reading", count=10, fields=["key", "title"])
    tools.collection_add("Reading", ["K0001"], from_collection="12")
    tools.history_record("search", "echo chambers", ["K0002", "K0001"], {"query": "echo chambers"})
    tools.collection_remove(7, ["K0001"])
    tools.collection_edit("12", name="Echo chambers", keep=True)
    tools.collection_edit("Reading", delete=True)
    tools.list_items({"collections": ["Reading"]}, count=0)
    paths, schemas = CONTRACT["paths"], CONTRACT["components"]["schemas"]
    for method, path, body, headers in fake.calls:
        ref = paths[path][method.lower()]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert set(body) <= set(schemas[ref.rsplit("/", 1)[1]]["properties"]), (path, body)
    bodies = [c[2] for c in fake.calls]
    assert bodies[0] == {"kind": "history", "contains": "echo", "count": 50}
    assert bodies[1] == {"collection": "Reading", "count": 10, "fields": ["key", "title"]}
    assert bodies[2] == {"collection": "Reading", "keys": ["K0001"], "from_collection": "12"}
    assert bodies[3] == {"workflow": "search", "name": "echo chambers",
                         "request": {"query": "echo chambers"}, "keys": ["K0002", "K0001"]}
    assert bodies[4] == {"collection": "7", "keys": ["K0001"]}
    assert bodies[5] == {"collection": "12", "name": "Echo chambers", "keep": True}
    assert bodies[6] == {"collection": "Reading", "delete": True}
    assert "collections" in schemas["Filters"]["properties"]
    assert bodies[7]["filters"] == {"collections": ["Reading"]}


def test_a_saved_result_goes_to_a_collection_in_portions(tools, fake, monkeypatch):
    from scriptorium_client import tools as tools_module

    monkeypatch.setattr(tools_module, "COLLECTION_BATCH", 120)
    path = tools.list_items(count=300)["saved_to"]  # a result of 300 items, saved as a set
    fake.calls.clear()
    sent = []

    def reply(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json=collection_reply(
            added=len(body["keys"]), not_held=body["keys"][:1] if len(sent) == 2 else []))
    tools.server._http = httpx.Client(base_url="https://scriptorium.test",
                                      transport=httpx.MockTransport(reply))
    done = tools.collection_add("Reading", from_set=path)
    assert [len(b["keys"]) for b in sent] == [120, 120, 60]
    assert sent[0]["collection"] == "Reading" and sent[1]["collection"] == "7"  # then by number
    assert done["added"] == 300 and done["new_collection"] and len(done["not_held"]) == 1
    sent.clear()
    recorded = tools.history_record("discovery", "a long run", from_set=path,
                                    request={"question": "q"})
    assert [len(b["keys"]) for b in sent] == [120, 120, 60] and recorded["added"] == 300
    assert sent[0]["workflow"] == "discovery" and "workflow" not in sent[1]
    assert sent[1] == {"collection": "7", "keys": sent[1]["keys"]}
    assert [k for b in sent for k in b["keys"]][:2] == ["K0000", "K0001"]  # the result's order


def test_a_large_collection_is_saved_to_a_file_like_any_large_result(tools, fake):
    fake.replies["/v1/collections/show"] = collection_reply(item_count=300, items=many(300))
    shown = tools.collection("Reading", count=300)
    assert shown["count"] == 300 and Path(shown["saved_to"]).exists() and len(shown["items"]) < 300
