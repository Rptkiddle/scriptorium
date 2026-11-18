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

## The notice

At the first request of each session your agent app shows Scriptorium's notice (what Scriptorium receives and records), and you choose Accept. `scriptorium-client notice` prints the full text.

Some agent apps cannot show such a question. For those, accept the notice once yourself in a terminal: run the command from step 3 with `notice --accept` in place of `serve`. The client records the date in its settings file and then answers in those apps. If the notice text changes, it asks you to accept the new text in the same way.

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
| `pdf_download` | Save the PDF of an item on your computer, where the server holds it and hands PDFs out |
| `pdf_upload` | Donate a PDF, or a folder of PDFs, to the collection; only on your own request |
| `pdf_upload_progress` | What became of the PDFs you donated |
| `zotero_add_items` | Add chosen items to your Zotero library, with their PDFs where held (needs the plugin below) |
| `zotero_pdf_upload` | Donate PDFs from your Zotero library (needs the plugin below) |

## Zotero (optional)

With the [scriptorium-bridge plugin](../scriptorium-bridge/README.md) installed in Zotero, your agent can add items from Scriptorium to your library and you can donate PDFs from it. Added items land in a collection named "Scriptorium"; publications you already hold are skipped. A donation sends only the PDF and its DOI or arXiv ID, after you have been told how many PDFs would go and have agreed. Nothing in your library is changed or removed. Zotero must be running.

A result too large for one reply is saved whole to a file on your machine. The agent gets the file's path, the count and the first rows, and can read on, mark items, export them, or search within them.

## Update

uvx keeps a copy of the client. To fetch the newest version, add `--refresh` once: `uvx --refresh --from "git+…" scriptorium-client version`, then restart your agent.
