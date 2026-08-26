# Stays on conda-forge via micromamba (same as local dev and CI) rather than
# introducing a separate pip requirements file — one dependency list
# (environment.yml), no drift risk between local/CI/container installs.
FROM mambaorg/micromamba:latest

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml && \
    micromamba clean --all --yes

# Bakes the embedding model's weights (~30 MB) into this layer at build
# time, with real network access -- so the running container never needs
# to reach huggingface.co. news_embed.py sets HF_HUB_OFFLINE=1 at import
# time specifically so a 1-OCPU VM with no guaranteed outbound access to
# that host gets a loud ValueError on any unexpected download attempt at
# runtime, never a silent hang or a slow ingestion cycle. Placed before
# the app-code COPY below so an ordinary commit doesn't invalidate this
# layer -- same reasoning as the micromamba install layer above it.
RUN micromamba run -n base python -c \
    "from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-base-8M')"

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER agent.py news_sources.py news_cache.py news_classify.py news_embed.py news_ingest.py news_push.py bot.py admin_bot.py combined_bot.py telemetry_monitor.py healthcheck.py guardrails.py users_db.py test_api.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Real incident, 2026-08-09: `docker logs` returned zero lines for this
# container's entire uptime -- not even the "Both bots ready (polling)"
# startup print(), despite the process clearly running. Python
# block-buffers stdout when it isn't a TTY (true for any `docker run -d`
# container), so print() output -- including news_push.py's per-cycle
# outcome logging, added specifically for production visibility into the
# push scheduler -- was silently never reaching the log stream at all.
# PYTHONUNBUFFERED forces unbuffered stdout/stderr regardless of TTY
# status; standard fix for exactly this class of Docker logging gap.
ENV PYTHONUNBUFFERED=1

# DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, and ADMIN_BOT_TOKEN
# are never baked into the image. On OCI, docker-entrypoint.sh fetches them
# from OCI Vault at startup via Instance Principal auth -- pass the
# *_SECRET_OCID vars (not secrets themselves, just resource identifiers)
# instead of the real values. Elsewhere (local/Docker Desktop testing),
# pass the plain env vars directly via `docker run -e` as before -- the
# entrypoint only touches Vault when a *_SECRET_OCID var is actually set.
# See docs/security-plan.md finding 2 and docs/deployment-plan.md.
# PHOENIX_ENABLED / PHOENIX_ENDPOINT / SUBSCRIBERS_DB_FILE are optional,
# same reasoning as always.
#
# CMD runs combined_bot.py -- both Telegram bots (info + admin) in one
# process, one container, so LangChain/python-telegram-bot are only loaded
# once. Matters on small-RAM Always Free shapes (e.g. Oracle's
# VM.Standard.E2.1.Micro, 1GB). The two-bot-token security design is
# unchanged -- see docs/bot-features-plan.md item 1. bot.py and
# admin_bot.py can still be run standalone/as separate containers if ever
# wanted (`docker run ... myfirstagent-bot python bot.py`), e.g. if this
# ever moves to a shape with enough RAM that splitting them back out is
# preferable for isolation.
# Which commit is this image? Until 2026-08-22 nothing could answer that:
# `docker inspect` showed only the base image's labels, so a running
# container could be compared to another image by id but never traced back
# to source. tools/deploy.sh sets this and refuses a rebuild when nothing
# in the COPY list above changed since the deployed commit -- a restart is
# not free, it re-runs the push job at first=10s.
#
# Last so a new commit does not invalidate the expensive micromamba layer.
ARG GIT_COMMIT=unknown
LABEL commit=$GIT_COMMIT

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "combined_bot.py"]
