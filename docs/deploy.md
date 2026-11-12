# Deploying the Scriptorium server

The server runs as a set of Docker containers described in `compose.yaml`: PostgreSQL with pgvector (`db`), the web service (`server`), the job worker (`worker`), GROBID (`grobid`, which parses PDFs) and the admin command (`cli`). `server`, `worker` and `cli` run one Scriptorium image; `db` and `grobid` run their projects' own images. Everything that must outlive a container is kept in one folder on the host, so containers can be rebuilt or replaced without loss.

## Requirements

- Linux with Docker and Docker Compose.
- A folder for the data (`/srv/scriptorium` below). Plan for about 175 GB at 500,000 items with one PDF in five.
- A GPU is used for embedding when present.

## Set up

```sh
git clone https://github.com/Rptkiddle/scriptorium.git /opt/scriptorium
cd /opt/scriptorium

sudo mkdir -p /srv/scriptorium/postgres /srv/scriptorium/data /srv/scriptorium/settings
sudo chown "$(id -u):$(id -g)" /srv/scriptorium/data /srv/scriptorium/settings

cp .env.example .env && chmod 600 .env
```

Fill in `.env`: `POSTGRES_PASSWORD` (`openssl rand -hex 24` makes one), `SCRIPTORIUM_UID` and `SCRIPTORIUM_GID` (`id -u`, `id -g`), `GIT_COMMIT` (`git rev-parse HEAD`). Then:

```sh
docker compose build cli
docker compose up -d db
docker compose run --rm cli migrate
docker compose run --rm cli model      # fetches the embedding model, once
docker compose up -d                   # starts the web service, the worker and GROBID
```

The web service listens on `127.0.0.1:8765` only. Put the machine's web server in front of it with HTTPS, admitting only the addresses that should reach it. With Caddy:

```
scriptorium.example.org {
    @outside not remote_ip 192.0.2.0/24
    respond @outside 403
    reverse_proxy 127.0.0.1:8765
}
```

Nothing is served without a member's API key: `curl -H "Authorization: Bearer <key>" http://127.0.0.1:8765/v1/health` shows whether the service answers and how large the collection is.

On a btrfs disk, switch copy-on-write off for the database's folder while it is still empty: `sudo chattr +C /srv/scriptorium/postgres`.

The folder on the host holds:

| Folder | Contents | Inside the containers |
|---|---|---|
| `postgres/` | the database's files | `/var/lib/postgresql/data` |
| `data/` | PDFs (`pdfs/`), parsed full texts (`tei/`), run folders (`runs/`), backups, the embedding model | `/srv/scriptorium` |
| `settings/` | `settings.toml` | `/etc/scriptorium` |

## Members

```sh
docker compose run --rm cli members add <name>
```

prints the member's API key once. The member installs the client (`docs/client.md`), runs `scriptorium-client setup` and enters the server's address and that key.

## Administer

Every admin command runs in a container of its own:

```sh
docker compose run --rm cli members add <name>     # prints the member's API key once
docker compose run --rm cli members list
docker compose run --rm cli venues add "<name>" --adapter crossref --issn 1234-5678
docker compose run --rm cli jobs list
docker compose run --rm cli jobs retry <id>        # run a failed job again
docker compose run --rm cli settings show
docker compose run --rm cli settings set <name> <value>
```

The database's address and password come from `.env`; every other setting is in `settings/settings.toml`, shown and changed with the `settings` commands.

## Start from an existing personal library

A member who has used the personal version of Scriptorium can bring their library as the starting collection.

On the member's machine, with Zotero running:

```sh
scriptorium seed-export records.jsonl
```

This reads Zotero's local API and writes each item's bibliographic record, without tags, collections or notes. Copy `records.jsonl` and the personal version's `index.sqlite` to `/srv/scriptorium/data/seed/` on the server. Inside the container that folder is `/srv/scriptorium/seed/`:

```sh
docker compose run --rm cli seed /srv/scriptorium/seed/index.sqlite \
    --member <name> --records /srv/scriptorium/seed/records.jsonl
```

The import can be run again: items already present are skipped. Its report is in `/srv/scriptorium/data/runs/seed/`.

The PDFs come next. Copy the member's PDF folder (subfolders included) to `/srv/scriptorium/data/seed/pdfs/` and run

```sh
docker compose run --rm cli seed-files /srv/scriptorium/seed/pdfs
```

Each PDF whose full text the import brought is recognised by its content and stored with its annotations and file metadata removed; other files are left alone, and nothing is parsed again. It can be stopped and run again. The personal version's parsed texts (its `tei/` folder) can be copied to `/srv/scriptorium/data/tei/`, so passages can later be cut again without GROBID.

## Clean passages

`docker compose run --rm cli passages clean-stamps` reports which stored passages hold a publisher's download stamp ("Downloaded from … by … on <date>"); with `--apply` it removes the stamps, deletes passages that held nothing else, and makes new vectors for the rest. Every change is recorded in its run folder (`runs/clean-stamps/`), which therefore holds the removed stamps. New full texts are cut without stamps.

## Update

```sh
git pull
docker compose build cli
docker compose up -d
```

`docker compose up -d` also starts any container added since the last update (for instance `worker` and `grobid`).

The web service brings the schema up to date when it starts.
