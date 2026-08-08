# Stays on conda-forge via micromamba (same as local dev and CI) rather than
# introducing a separate pip requirements file — one dependency list
# (environment.yml), no drift risk between local/CI/container installs.
FROM mambaorg/micromamba:latest

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml && \
    micromamba clean --all --yes

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER agent.py news_sources.py bot.py admin_bot.py users_db.py ./

# DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, and ADMIN_BOT_TOKEN
# are required at runtime, passed in via `docker run -e` / a Kubernetes
# Secret — never baked into the image. PHOENIX_ENABLED / PHOENIX_ENDPOINT /
# SUBSCRIBERS_DB_FILE are optional, same reasoning.
#
# This image serves both bots — CMD runs the public info bot by default;
# override the command (`docker run ... myfirstagent-bot python admin_bot.py`)
# to run the admin bot instead. The two must share a SUBSCRIBERS_DB_FILE
# (e.g. both pointed at the same mounted volume) since users_db.py is how
# they agree on who's approved — see docs/bot-features-plan.md item 1.
CMD ["python", "bot.py"]
