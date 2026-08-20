"""
Post-deployment check: confirms directories/files meant to survive a
container restart are actually configured to live inside the mounted
`/data` volume -- not just that the container started without error.

Real incident, found 2026-08-19/20: `NEWS_CACHE_DIR` was unset on the
running container, so `news_cache.py` fell back to its relative default
(`news_cache`), which resolved to `/app/news_cache` -- the container's
own filesystem, not the `myfirstagent-data` volume mounted at `/data`.
Every redeploy silently reset the article cache to empty, with nothing
in `docker logs` to show for it (an unset optional env var isn't an
error) -- 2202+ articles survived one deploy cycle purely because the
container hadn't been restarted in three days. `docker inspect` showing
the env var is *set* only proves intent; this script also confirms the
directory it points at is reachable at that path inside the running
container and actually holds data, which is the thing that actually
would have caught the incident.

This is the same class of silent regression `check_telemetry.py` exists
for (an unset optional env var that fails silently, not loudly) --
same shape of check, applied to volume-backed paths instead of tracing.

Checks, for each `--dir-env NAME` given:
  1. The env var is set on the running container (`docker inspect`).
  2. Its value is a path under `--volume-mount-prefix` (default `/data`)
     -- catches the exact regression above even if some future default
     changes to look less obviously wrong than a bare relative path.
  3. The directory exists inside the container and is non-empty (a
     directory that exists but has zero entries right after a restart
     that was supposed to carry existing data forward is itself
     suspicious, not just "technically fine because nothing crashed").

Exits 0 and prints "OK" with each directory's entry count if every
`--dir-env` passes all three checks; exits 1 with which check failed
for which var otherwise. `--allow-empty NAME` opts a specific var out
of check 3 (e.g. a brand-new archive directory that's legitimately
empty until the first `cleanup_expired` run actually retires something).

Usage:
    python tools/check_data_persistence.py \\
        --bot-vm ubuntu@<bot-vm-ip> --bot-key <path-to-bot-vm-key> \\
        --container myfirstagent-bot \\
        --dir-env NEWS_CACHE_DIR --dir-env NEWS_ARCHIVE_DIR \\
        --allow-empty NEWS_ARCHIVE_DIR
"""

import argparse
import json
import subprocess
import sys


def _ssh_run(host: str, key: str, remote_command: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", key, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", host, remote_command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def get_container_env(bot_vm: str, bot_key: str, container: str, timeout: int) -> dict[str, str] | None:
    remote_command = (
        f"sudo docker inspect {container} --format '{{{{json .Config.Env}}}}'"
    )
    result = _ssh_run(bot_vm, bot_key, remote_command, timeout)
    if result.returncode != 0 or not result.stdout.strip():
        print(f"  could not inspect container {container!r}.\n  stderr: {result.stderr.strip()}")
        return None
    try:
        pairs = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  unexpected docker inspect output: {result.stdout.strip()[:300]}")
        return None
    env = {}
    for pair in pairs:
        if "=" in pair:
            k, v = pair.split("=", 1)
            env[k] = v
    return env


def count_dir_entries(
    bot_vm: str, bot_key: str, container: str, path: str, timeout: int
) -> int | None:
    """Returns the number of entries in `path` inside the running
    container, 0 if the path doesn't exist yet (some dirs, e.g.
    news_cache.py's per-day archive subdirectories, are created lazily
    on first write -- not existing yet right after a restart is not
    itself a failure, same as "exists but empty"), or None only if the
    path is unreachable for a reason that ISN'T "doesn't exist" (e.g.
    the container itself can't be reached at all)."""
    remote_command = f"sudo docker exec {container} sh -c 'ls -A {path} 2>&1'"
    result = _ssh_run(bot_vm, bot_key, remote_command, timeout)
    if result.returncode != 0:
        if "No such file or directory" in result.stdout:
            return 0
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bot-vm", required=True, help="e.g. ubuntu@<bot-vm-ip>")
    parser.add_argument("--bot-key", required=True, help="path to the bot VM's SSH key")
    parser.add_argument("--container", default="myfirstagent-bot")
    parser.add_argument(
        "--dir-env",
        action="append",
        required=True,
        help="name of an env var that should point at a directory inside the mounted volume; repeatable",
    )
    parser.add_argument(
        "--allow-empty",
        action="append",
        default=[],
        help="a --dir-env NAME that's allowed to be empty right now (e.g. a fresh archive dir)",
    )
    parser.add_argument("--volume-mount-prefix", default="/data")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    print(f"1. Reading env vars from container {args.container!r}...")
    env = get_container_env(args.bot_vm, args.bot_key, args.container, args.timeout)
    if env is None:
        print("\nFAIL: could not read the container's environment at all.")
        return 1

    failures: list[str] = []
    for name in args.dir_env:
        print(f"2. Checking {name}...")
        value = env.get(name)
        if not value:
            print(f"  FAIL: {name} is not set on the running container.")
            failures.append(f"{name}: unset")
            continue
        if not value.startswith(args.volume_mount_prefix.rstrip("/") + "/"):
            print(
                f"  FAIL: {name}={value!r} is not under {args.volume_mount_prefix!r} -- "
                "this is the exact silent-reset regression: an unset or misdirected var "
                "resolves onto the container's own (non-persistent) filesystem instead of the volume."
            )
            failures.append(f"{name}: {value!r} not under {args.volume_mount_prefix!r}")
            continue
        count = count_dir_entries(args.bot_vm, args.bot_key, args.container, value, args.timeout)
        if count is None:
            print(f"  FAIL: {name}={value!r} isn't reachable inside the container (docker exec itself failed).")
            failures.append(f"{name}: path {value!r} unreachable")
            continue
        if count == 0 and name not in args.allow_empty:
            print(
                f"  FAIL: {name}={value!r} is empty or doesn't exist yet. If this is expected right after "
                f"this deploy (e.g. a directory created lazily on first write), pass --allow-empty {name}."
            )
            failures.append(f"{name}: {value!r} is empty")
            continue
        print(f"  OK: {name}={value!r}, {count} entrie(s).")

    if failures:
        print(f"\nFAIL: {len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nOK: all directories are set, inside the mounted volume, and reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
