# Stays on conda-forge via micromamba (same as local dev and CI) rather than
# introducing a separate pip requirements file — one dependency list
# (environment.yml), no drift risk between local/CI/container installs.
FROM mambaorg/micromamba:latest

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml && \
    micromamba clean --all --yes

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER agent.py news_sources.py bot.py ./

# DEEPSEEK_API_KEY and TELEGRAM_BOT_TOKEN are required at runtime, passed in
# via `docker run -e` / a Kubernetes Secret — never baked into the image.
# PHOENIX_ENABLED / PHOENIX_ENDPOINT are optional, same reasoning.
CMD ["python", "bot.py"]
