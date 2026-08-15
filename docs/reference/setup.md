# Setup & Local Run

Needed once, when setting up this project fresh, or when running a
piece of it locally instead of on the deployed VM. Not everyday-coding
detail — see `CLAUDE.md` for what's actually needed on every task.

## Environment

Dedicated Miniforge/conda environment named `myfirstagent` (not system
Python or `base`). Dependencies are conda-forge packages in
`environment.yml` — no `requirements.txt`/pip path for this project.

```powershell
conda env create -f environment.yml   # first time only
conda activate myfirstagent
$env:DEEPSEEK_API_KEY = "<your-deepseek-key>"
python agent.py
```

If `conda` isn't recognized in a shell (not initialized for it), call it
via its full path: `& "C:\ProgramData\miniforge3\condabin\conda.bat"
activate myfirstagent`, or invoke the env's interpreter directly:
`& "C:\ProgramData\miniforge3\envs\myfirstagent\python.exe" agent.py`.

**Adding a dependency later: use `mamba`, not `conda`** — conda's
classic solver has repeatedly taken 10+ minutes or hung outright on this
project's dependency tree. See the `use-mamba-not-conda` skill:
`mamba install -n myfirstagent -c conda-forge <package>`, then add it to
`environment.yml` to keep the env reproducible.

No lint config or build step. Test suite is `pytest` — see `CLAUDE.md`'s
one-liner and `docs/plans/telemetry-and-testing-plan.md` for the rest.

## Running the Telegram bot locally

```powershell
conda activate myfirstagent
$env:DEEPSEEK_API_KEY = "<your-deepseek-key>"
$env:TELEGRAM_BOT_TOKEN = "<your-bot-token>"   # from @BotFather
$env:ADMIN_CHAT_ID = "<your-telegram-numeric-user-id>"
$env:ADMIN_BOT_TOKEN = "<second-bot-token-for-admin_bot.py>"
python bot.py
```

`admin_bot.py` (the approval companion), same env plus its own token:

```powershell
$env:ADMIN_BOT_TOKEN = "<second-bot-token-from-botfather>"
$env:ADMIN_CHAT_ID = "<your-telegram-numeric-user-id>"
$env:TELEGRAM_BOT_TOKEN = "<the-info-bot-token>"
python admin_bot.py
```

Or both in one process (matches what's actually deployed):

```powershell
$env:DEEPSEEK_API_KEY = "<your-deepseek-key>"
$env:TELEGRAM_BOT_TOKEN = "<info-bot-token>"
$env:ADMIN_BOT_TOKEN = "<admin-bot-token>"
$env:ADMIN_CHAT_ID = "<your-telegram-numeric-user-id>"
python combined_bot.py
```

Both processes need to see the same SQLite file — `SUBSCRIBERS_DB_FILE`
env var (defaults to `subscribers.db` in the working directory).

## Running Phoenix (telemetry) locally

```powershell
docker run -d --name phoenix -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest
$env:PHOENIX_ENABLED = "true"
python agent.py
```

Dashboard: `http://localhost:6006`. Unset `PHOENIX_ENABLED` (or leave it
unset, the default) for a no-op — every test/CI run relies on this.

## Docker (local build, not the live deploy)

```powershell
docker build -t myfirstagent-bot .
docker run -d --name myfirstagent-bot --restart unless-stopped `
  -e DEEPSEEK_API_KEY=$env:DEEPSEEK_API_KEY `
  -e TELEGRAM_BOT_TOKEN=$env:TELEGRAM_BOT_TOKEN `
  -e ADMIN_CHAT_ID=$env:ADMIN_CHAT_ID `
  -e ADMIN_BOT_TOKEN=$env:ADMIN_BOT_TOKEN `
  -e SUBSCRIBERS_DB_FILE=/data/subscribers.db `
  -v myfirstagent-data:/data `
  myfirstagent-bot
```

`CMD` runs `combined_bot.py` by default; override
(`docker run ... myfirstagent-bot python bot.py`) to run one bot
standalone in its own container instead.

**This is for local testing only.** Building and deploying to the live
VM is the `deploy-engineer` subagent's job — see `CLAUDE.md`. Don't run
`docker build` on the VM itself; see the `build-locally-deploy-remotely`
skill for why.
