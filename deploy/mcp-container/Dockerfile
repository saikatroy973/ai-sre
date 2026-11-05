# Isolated runtime image for the local MCP server.
#
# Contracts enforced here (and double-checked by verify_runtime.py):
# - the MCP process runs as a non-root uid with no write access to its own
#   code or site-packages;
# - every Linux capability is dropped at run time (--cap-drop ALL);
# - no network namespace (--network none): the evidence reader never talks
#   to a cluster or to the internet;
# - only two small tmpfs mounts exist (/tmp, /data) and they vanish with
#   the container.
#
# Build and probe: bash deploy/mcp-container/verify.sh

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INVESTIGATOR_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore .

# Non-root runtime identity (uid 10001), no supplementary groups, no shell,
# no home directory; only tmpfs paths need to be writable.
RUN groupadd -g 10001 mcp \
    && useradd -u 10001 -g 10001 -M -N -l -d /nonexistent -s /usr/sbin/nologin mcp

COPY deploy/mcp-container/verify_runtime.py /app/verify_runtime.py

USER 10001:10001
