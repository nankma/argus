---
name: use-mamba-not-conda
description: Use when installing, updating, removing, or creating packages/environments for this project's myfirstagent Miniforge environment — mamba solves dependency trees far faster than conda's classic solver, which has repeatedly taken 10+ minutes or appeared to hang on this project's stack.
---

# Use mamba, not conda, for package operations in this repo

**Rule:** any `conda install` / `conda remove` / `conda update` / `conda create` / `conda env create` / `conda env update` command for the `myfirstagent` environment should be run as the equivalent `mamba` command instead. Read-only commands (`conda activate`, `conda list`, `conda search`, `conda info`) are unaffected — mamba doesn't change environment activation, and either binary is fine for inspection.

**Why:** conda's classic solver has repeatedly taken 10+ minutes or appeared to hang on this project's dependency tree — `langchain` + `langgraph` + `langsmith` already pull in a large, interlocking set of pinned versions, and adding `arize-phoenix` (OpenTelemetry stack, gRPC, protobuf, SQLAlchemy, Strawberry GraphQL, Starlette/Uvicorn, pandas, pyarrow) made a single `conda install` slow enough that it was genuinely unclear whether it was working or stuck. `mamba` is a drop-in, much faster solver for exactly this case, and it's already bundled with this Miniforge install — no separate install needed.

## Command mapping

| Instead of | Use |
|---|---|
| `conda install -n myfirstagent -c conda-forge <pkgs>` | `mamba install -n myfirstagent -c conda-forge <pkgs>` |
| `conda remove -n myfirstagent <pkg>` | `mamba remove -n myfirstagent <pkg>` |
| `conda env create -f environment.yml` | `mamba env create -f environment.yml` |
| `conda env update -n myfirstagent -f environment.yml` | `mamba env update -n myfirstagent -f environment.yml` |

If `mamba` isn't recognized in a given shell (not initialized for that shell), call it via its full path instead — same pattern as `conda`:

```powershell
& "C:\ProgramData\miniforge3\condabin\mamba.bat" install -n myfirstagent -c conda-forge --yes <pkgs>
```

## Related, separate issue: environment write permissions

`C:\ProgramData\miniforge3\envs\myfirstagent` has repeatedly lost write permission for the local user mid-session (not just between reboots), unrelated to conda vs. mamba — both hit the same `EnvironmentNotWritableError` when this happens. If an install fails with that error, it needs an elevated (Run as Administrator) PowerShell, same as any other install command, regardless of which solver is used. See `CLAUDE.md` for the current state of this.
