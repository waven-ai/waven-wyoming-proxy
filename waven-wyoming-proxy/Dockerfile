# Dual-purpose: builds as a Home Assistant add-on (Supervisor passes BUILD_FROM
# per arch via build.yaml) and as a plain multi-arch Docker image.
ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    WAVEN_URI=tcp://0.0.0.0:10300

WORKDIR /app

# gosu drops privileges by exec'ing (no extra PID, signals pass through), which
# is what the entrypoint needs and what `init: false` in config.yaml assumes.
# hadolint ignore=DL3008: version-pinning apt packages against a base image that
# is itself a moving `python:3.12-slim` tag buys no determinism and breaks the
# build on every base refresh (the stale-apt-pin trap noted in CLAUDE.md).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# The proxy needs no privileges: it opens one TCP listener above 1024, talks
# HTTPS out, and writes one small JSON file. Running the whole thing as root
# was purely inherited from the base image.
RUN groupadd -r waven && useradd -r -g waven -d /app -s /sbin/nologin waven

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data \
    && chown -R waven:waven /app /data

EXPOSE 10300

# Starts as root only long enough to chown the runtime-mounted /data, then
# exec's the proxy as `waven`.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# proxy.py reads /data/options.json (HA add-on) and/or WAVEN_* env vars.
CMD ["python", "proxy.py"]
