# Stays on conda-forge via micromamba (same as local dev and CI) rather than
# introducing a separate pip requirements file — one dependency list
# (environment.yml), no drift risk between local/CI/container installs.
FROM mambaorg/micromamba:latest

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml && \
    micromamba clean --all --yes

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER agent.py news_sources.py bot.py admin_bot.py combined_bot.py users_db.py ./

# DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, and ADMIN_BOT_TOKEN
# are required at runtime, passed in via `docker run -e` / a Kubernetes
# Secret — never baked into the image. PHOENIX_ENABLED / PHOENIX_ENDPOINT /
# SUBSCRIBERS_DB_FILE are optional, same reasoning.
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
CMD ["python", "combined_bot.py"]
