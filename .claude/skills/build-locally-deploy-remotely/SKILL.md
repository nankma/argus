---
name: build-locally-deploy-remotely
description: Use when building or updating the Docker image that runs on the Oracle Cloud VM (or any other cloud host in this project) — build the image on the local dev machine, then transfer the finished image to the remote host, rather than running `docker build` on the remote host itself.
---

# Build locally, deploy remotely — don't build on the cloud VM

**Rule:** for `myfirstagent-bot`'s Docker image, always run `docker build` on
the local dev machine. Never run `docker build` directly on the deployed
cloud VM (the Oracle `VM.Standard.E2.1.Micro` instance, or any future
replacement). Transfer the already-built image instead.

**Why:** the deployed VM is a tiny, free-tier shape (1/8 OCPU, 1GB RAM).
Building there directly was tried and was a real problem, not a
theoretical one — a plain `docker build` repeatedly took 5+ minutes and
had to be moved to a background task, and one such build was left in an
uncertain, possibly-corrupted state after being interrupted (a stray
`pkill` sent while investigating an unrelated slow-SSH issue arrived right
as the build was finishing). Building locally and transferring instead
took a fraction of the time and produced a known-good, already-verified
image.

## How to do it

1. Build and verify the image locally, same as always:
   ```
   docker build -t myfirstagent-bot .
   ```
   Test it locally first if the change is nontrivial (see `CLAUDE.md`'s
   Docker section) — cheaper to catch a broken image before it's on the
   only machine actually serving the bot.

2. Transfer the image directly over SSH — no container registry needed
   for a single personal VM:
   ```bash
   KEY="/path/to/ssh-key.pri.key"
   docker save myfirstagent-bot:latest | ssh -i "$KEY" ubuntu@<vm-ip> "sudo docker load"
   ```
   `docker save` streams the image as a tar over stdout; piping straight
   into `ssh ... docker load` on the other end avoids writing a large
   intermediate file on either machine.

3. Recreate/restart the container on the VM to pick up the new image
   (`docker stop`/`docker rm` the old one, `docker run` again with the
   same flags — see `docs/deployment-plan.md` for the current `docker run`
   command). `docker load` replaces the `myfirstagent-bot:latest` tag but
   doesn't restart anything using the old image automatically.

## When this doesn't apply

- Source-only changes that don't need a new image (e.g. editing docs) —
  nothing to build or transfer.
- If this project ever moves to a proper CI/CD pipeline that builds in
  GitHub Actions and pushes to a registry, this skill becomes obsolete —
  see `docs/deployment-plan.md`'s "CD" open question, not yet decided.
