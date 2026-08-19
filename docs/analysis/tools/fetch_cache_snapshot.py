"""
Step 1 of the clustering analysis: pull a snapshot of the live news cache
off the production VM into a local TSV.

Everything downstream (cluster_news.py, project_news.py) reads that TSV, so
the expensive/fragile part -- reaching the VM over SSH -- happens exactly
once and the analysis itself can be re-run offline as many times as needed.

The bot VM has no Python available to `docker exec` into (confirmed:
"python: executable file not found in $PATH" -- the image runs its own
entrypoint, and there's no interpreter on the host), so this shells out to
plain `grep`/`cut` inside the container rather than running a script there.

Connection details (VM IP, SSH key path) live in local-infra/infrastructure.yaml,
which is gitignored -- pass them as arguments rather than hardcoding.

    python docs/analysis/tools/fetch_cache_snapshot.py \
        --host ubuntu@<bot-vm-ip> \
        --key "C:/path/to/ssh-key.pri.key" \
        --out docs/analysis/data/cache-snapshot.tsv
"""

import argparse
import os
import subprocess
import sys
import tempfile

# Runs inside the container. Emits one tab-separated line per cached article:
#   source_key <TAB> published_dt <TAB> fetched_at <TAB> categories <TAB> title <TAB> summary
# Tabs are stripped from the fields themselves so the format can't be broken
# by an article whose own text contains one.
REMOTE_SCRIPT = r"""
cd /app/news_cache || exit 1
for f in *.yaml; do
  s=$(grep -m1 '^source_key:' "$f" | cut -d' ' -f2)
  d=$(grep -m1 '^published_dt:' "$f" | cut -d' ' -f2 | tr -d "'")
  a=$(grep -m1 '^fetched_at:' "$f" | cut -d' ' -f2 | tr -d "'")
  # `categories` is a YAML list and always the last key, written either
  # inline ("categories: []") or as a block of "- Name" lines. Take
  # everything from that key to EOF, join it onto one line, and reduce it
  # to a comma-separated list.
  c=$(sed -n '/^categories:/,$p' "$f" | tr '\n' ' ' \
      | sed "s/^categories: *//; s/\[//g; s/\]//g; s/- */,/g; s/  */ /g; s/^ *//; s/ *$//; s/^,//")
  t=$(grep -m1 '^title:' "$f" | sed 's/^title: //' | tr -d '\t')
  u=$(grep -m1 '^summary:' "$f" | sed 's/^summary: //' | cut -c1-300 | tr -d '\t')
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$s" "$d" "$a" "$c" "$t" "$u"
done
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="ssh target, e.g. ubuntu@1.2.3.4")
    ap.add_argument("--key", required=True, help="path to the SSH private key")
    ap.add_argument("--container", default="myfirstagent-bot")
    ap.add_argument("--out", default="docs/analysis/data/cache-snapshot.tsv")
    args = ap.parse_args()

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as tmp:
        tmp.write(REMOTE_SCRIPT)
        local_script = tmp.name

    try:
        print(f"[1/3] copying collector to {args.host} ...")
        subprocess.run(["scp", "-i", args.key, "-o", "StrictHostKeyChecking=no",
                        local_script, f"{args.host}:/tmp/_collect.sh"],
                       check=True, capture_output=True)

        print(f"[2/3] running it inside {args.container} ...")
        remote = (f"sudo docker cp /tmp/_collect.sh {args.container}:/tmp/_collect.sh "
                  f"&& sudo docker exec {args.container} sh /tmp/_collect.sh")
        result = subprocess.run(["ssh", "-i", args.key, "-o", "ConnectTimeout=20",
                                 "-o", "StrictHostKeyChecking=no", args.host, remote],
                                check=True, capture_output=True)

        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "wb") as f:
            f.write(result.stdout)

        n = result.stdout.decode("utf-8", "replace").count("\n")
        print(f"[3/3] wrote {args.out} -- {n} articles")
        if n == 0:
            print("WARNING: zero articles. Is the cache empty, or the container name wrong?",
                  file=sys.stderr)
            sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"FAILED: {exc}\nstderr: {exc.stderr.decode('utf-8', 'replace')[:500]}",
              file=sys.stderr)
        sys.exit(1)
    finally:
        os.unlink(local_script)


if __name__ == "__main__":
    main()
