# The Scriptorium server image: the admin command, and (as they are built)
# the API and the job worker. Build from the repository root:
#   docker compose build
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, so a change of code does not download them again. The
# download cache is kept out of the image, which would otherwise double in size.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# The commit the image was built from, recorded in every run folder.
ARG GIT_COMMIT=""
ENV SCRIPTORIUM_COMMIT=${GIT_COMMIT} \
    SCRIPTORIUM_SETTINGS=/etc/scriptorium/settings.toml \
    HF_HOME=/srv/scriptorium/models \
    HOME=/tmp \
    PATH=/app/.venv/bin:${PATH}

ENTRYPOINT ["scriptorium"]
