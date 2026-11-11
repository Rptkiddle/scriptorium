# Using Scriptorium from your agent

The Scriptorium client is a small program on your own machine. Your agent talks to it, and it talks to the Scriptorium server. Any agent that supports MCP can use it. You need the server's address and your API key, both from the server's administrator.

## 1. Install uv

uv runs the client without a separate installation step. See <https://docs.astral.sh/uv/getting-started/installation/>; on macOS and Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Enter the server's address and your key

```sh
uvx --from "git+https://github.com/Rptkiddle/scriptorium@collaborative#subdirectory=client" scriptorium-client setup
```

The command asks for both, checks them against the server and saves them in a settings file that only you can read. Off campus, connect the university VPN first.

## 3. Add the client to your agent

The agent starts the client itself with this command:

```sh
uvx --from "git+https://github.com/Rptkiddle/scriptorium@collaborative#subdirectory=client" scriptorium-client serve
```

In Claude Code:

```sh
claude mcp add scriptorium -- uvx --from "git+https://github.com/Rptkiddle/scriptorium@collaborative#subdirectory=client" scriptorium-client serve
```

In an agent configured with a JSON file of MCP servers:

```json
{
  "mcpServers": {
    "scriptorium": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Rptkiddle/scriptorium@collaborative#subdirectory=client",
               "scriptorium-client", "serve"]
    }
  }
}
```

## 4. Check

Ask your agent to run Scriptorium's `status` tool. It reports whether the server is reachable, how large the collection is, the client's version, and whether Zotero and the Scriptorium Bridge plugin answer on your machine.

## What the tools do

| Tool | Use it for |
|---|---|
| `lookup` | Is this paper held? By DOI, arXiv ID or exact title. An identifier not held is fetched from the registry and added |
| `list_items` | Everything matching filters (an author, a venue, years), and how many |
| `search_items` | Papers about a topic |
| `search_passages` | What papers say: paragraphs of held full texts |
| `item` | One item in full |
| `facets` | The values a filter can take, with counts |
| `bibtex` | BibLaTeX or BibTeX entries for chosen items |
| `status` | Whether everything is in place |
| `set_read`, `set_mark`, `set_export_bibtex` | Work with a large result saved to a file |

A result too large for one reply is saved whole to a file on your machine. The agent gets the file's path, the count and the first rows, and can read on, mark items, export them, or search within them.

## Update

uvx keeps a copy of the client. To fetch the newest version, add `--refresh` once: `uvx --refresh --from "git+…" scriptorium-client version`, then restart your agent.
