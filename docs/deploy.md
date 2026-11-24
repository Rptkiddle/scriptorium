# Deploying the Scriptorium server

The server runs as a set of Docker containers described in `compose.yaml`: PostgreSQL with pgvector (`db`), the web service (`server`), the job worker (`worker`), GROBID (`grobid`, which parses PDFs) and the admin command (`cli`). `server`, `worker` and `cli` run one Scriptorium image; `db` and `grobid` run their projects' own images. Everything that must outlive a container is kept in one folder on the host, so containers can be rebuilt or replaced without loss.

## Requirements

- Linux with Docker and Docker Compose.
- A folder for the data (`/srv/scriptorium` below). Plan for about 175 GB at 500,000 items with one PDF in five.
- Optional: an NVIDIA GPU (driver 560 or newer) with the NVIDIA Container Toolkit. Searching and donations work without one; embedding a large harvest needs one (see [GPU](#gpu)).

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

## GPU

Every item and passage is embedded (turned into the vector that searching compares), and so is every search query. Without a GPU this runs on the CPU, which is enough for searching and for donations arriving one by one. Embedding a harvest of hundreds of thousands of items on the CPU takes weeks.

To use the machine's NVIDIA GPU, remove the `#` before `COMPOSE_FILE=compose.yaml:compose.gpu.yaml` in `.env` and run `docker compose up -d`. The web service and the worker then embed on the GPU; `docker compose logs server` names the device in its first lines.

The worker embeds what has no vector yet whenever no job waits, a portion at a time. `settings set embedding.when_idle off` pauses that, for instance to leave a shared GPU to others, and `on` resumes it; `embedding.batch_size` sets how many texts the model takes at once. Both take effect within a minute, without a restart.

A container that loses the GPU while it runs goes on embedding on the CPU until it is restarted. On machines where this happens, `scripts/gpu-check.sh` restarts the web service and the worker when they have lost the GPU and no job is running. Run it every hour from the crontab of an account that may run Docker:

```
10 * * * * /path/to/scriptorium/scripts/gpu-check.sh >> /srv/scriptorium/data/logs/gpu-check.log 2>&1
```

## Members

```sh
docker compose run --rm cli members add <name>
```

prints the member's API key once. The member installs the client (`docs/client.md`), runs `scriptorium-client setup` and enters the server's address and that key.

## The admin menu

An admin runs the server from their own agent, without a terminal on the host. Make a member an admin with

```sh
docker compose run --rm cli members admin <name>      # --remove takes the mark away
```

Their client then shows six more tools, from the session after their next request on (they restart their agent, or reconnect the client, once):

| Tool | What it does |
|---|---|
| `admin_overview` | The state of the server on one page: collection, PDF uploads, the schedule and the last runs, members and requests, disk space, changed settings |
| `admin_settings` | Show every setting with its value, default and explanation; change one |
| `admin_members` | Members with their activity; add a member, revoke one |
| `admin_venues` | The subscribed venues with the items held from each; subscribe, unsubscribe |
| `admin_runs` | Harvests and backups: the next scheduled moment, the latest runs; start one now |
| `admin_jobs` | The worker's jobs and their failures; run a failed one again |

A request that would change something answers with a preview first and makes the change only when it is sent again with a confirmation, which the agent sends after the admin's yes. Every change is recorded with its value before and after (table `admin_log`); the overview shows the latest. The server refuses these requests to any member who is not an admin.

The menu holds what an admin does while the server runs. Four things stay with the admin command on the host: installing and repairing (`migrate`, `seed`, `load`, `restore`); the keys for outside sources, which would otherwise pass through an agent's conversation; the settings that say where the installation's parts are (`database.address`, `data.folder`, `backup.folder`, `grobid.address`); and the admin mark itself, so that a stolen admin key cannot make more admins. A new member's key appears once in the admin's conversation and is stored nowhere.

## Administer

Every admin command runs in a container of its own:

```sh
docker compose run --rm cli members add <name>     # prints the member's API key once
docker compose run --rm cli members list
docker compose run --rm cli members admin <name>   # let a member use the admin menu
docker compose run --rm cli venues add "<name>" --adapter crossref --issn 1234-5678
docker compose run --rm cli venues import /srv/scriptorium/<list>.csv   # a journal list, harvested through OpenAlex
docker compose run --rm cli backfill --all          # queue a harvest of every such journal since 2020
docker compose run --rm cli backfill --source-id S107737141   # or of named journals
docker compose run --rm cli fetch /srv/scriptorium/<list>.csv --out /srv/scriptorium/harvest   # or: fetch the journals' papers to files first,
docker compose run --rm cli load /srv/scriptorium/harvest --all     # then load the files in bulk
docker compose run --rm cli surveil                 # harvest the journals' recent papers now (see below)
docker compose run --rm cli jobs list
docker compose run --rm cli jobs retry <id>        # run a failed job again
docker compose run --rm cli settings show
docker compose run --rm cli settings set <name> <value>
```

Two settings decide what happens to PDFs: `files.pdf_download` (whether the server hands held PDFs to members) and `files.pdf_upload` (whether it accepts donated PDFs). Both are `on` by default and take effect at once when set to `off`, for instance where licences do not allow members to receive PDFs. Full texts stay searchable either way.

A journal list is a CSV file with the columns `openalex_id`, `journal`, `issns`, `publisher` and `coded_class`; journals of class `MIXED` are too broad to take whole (multidisciplinary journals), and only their papers whose primary topic OpenAlex places in its Social Sciences domain are admitted. The harvest needs an OpenAlex API key (`settings set sources.openalex_api_key <key>`). It admits research papers (OpenAlex types article, review, conference paper and software paper) in English that are not retracted, and records each paper's OpenAlex work, so running it again adds only what is new. Each journal's counts are in its job's run folder (`runs/backfill/`).

For a first harvest of many journals, `fetch` and `load` do the same in two steps, much faster: `fetch` saves every listed journal's papers to files without touching the database, and `load` admits them in bulk under the same rules (counts in `runs/load/`). `load --abstracts <file>` fills in abstracts found elsewhere for papers OpenAlex has none for; the file holds one `{"doi": ..., "abstract": ...}` object per line. Loaded items get their vectors from the worker, which needs a GPU for a harvest of this size (see [GPU](#gpu)).

### Weekly surveillance

The weekly surveillance keeps the collection current. The worker starts it itself, on the day and hour the settings `surveillance.day` and `surveillance.hour` name (Monday 03:00 by default; `surveillance.day off` switches the schedule off), and writes the backup directly after it. A worker that was not running at that moment starts both when it comes up, if no more than twelve hours have passed. `surveil` runs a harvest by hand, at once, and an admin can start one from the admin menu.

A harvest asks OpenAlex for every paper the listed journals published in the last 90 days (setting `surveillance.window_days`), and admits those that pass the same rules as the first harvest and are not yet held. The window is far wider than a week because OpenAlex lists some papers weeks after their publication date and fills in a paper's type, language and topic later still; a paper not admitted one week for a missing label is seen again the next. Papers harvested before are recognised by their OpenAlex work and skipped, so the wide window costs little: the journals are asked for together, 50 to a request, and a run over two thousand journals takes minutes.

Recent papers without an abstract take the one OpenAlex has gained since, and are otherwise looked up at Scopus, ScienceDirect (Elsevier's papers, which it has from the day of publication) and Springer Nature where their keys are set (`sources.elsevier_api_key`, `sources.springer_api_key`). New items, and items whose abstract arrived, get their vectors from the worker.

`surveil --dry-run` fetches and counts without writing to the collection. Each run's counts per journal and totals are in its run folder (`runs/surveillance/`); a run stops asking once its cost passes the setting `surveillance.max_cost` (1 US dollar by default), and journals it could not read are named in the totals and covered by the next run. While a harvest runs, uploaded PDFs wait their turn: the worker does one job at a time.

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

## Back up and restore

```sh
docker compose run --rm cli backup
```

writes a backup to the backup folder (setting `backup.folder`, by default `/srv/scriptorium/data/backups` on the host): every table of the database under `runs/<date-time>/`, and one copy of the held PDFs and the parsed full texts under `files/`, to which each backup adds what is new. The server keeps running meanwhile. Backups older than `backup.days_kept` days (14) are removed; the newest always stays. The worker writes a backup every week, directly after the harvest (see [Weekly surveillance](#weekly-surveillance)); the command above writes one by hand, and an admin can start one from the admin menu.

The backup holds what the database holds, the usage log and members' collections included, for as long as backups are kept. Not in it: donations still waiting to be processed, run folders, the embedding model, `.env` and the settings file. A PDF removed from the collection stays under `files/` until it is deleted there. The backup folder is on the same machine as the data; copy it elsewhere to survive the loss of the machine.

```sh
docker compose run --rm cli restore latest
```

fills an empty database and an empty data folder from the newest backup (or the one named, e.g. `2026-11-13-033000`), and checks every table's row count against what the backup recorded. It refuses a database that holds tables. To try a restore beside the running server, create a second database and restore into it and a spare folder:

```sh
docker compose exec db createdb -U scriptorium restore_test
docker compose run --rm cli restore latest --database-name restore_test --data-folder /srv/scriptorium/restore_test
```

## Update

```sh
git pull
docker compose build cli
docker compose up -d
```

`docker compose up -d` also starts any container added since the last update (for instance `worker` and `grobid`).

The web service brings the schema up to date when it starts.
