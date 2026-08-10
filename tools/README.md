# tools/

Build-time and authoring utilities. **Not part of the running service** —
nothing in `agent.py`, `bot.py`, or the test suite imports from here.

## Which Python to use

These scripts run on the **conda `base` environment**, not the project's
`myfirstagent` environment:

```bash
# Windows / Git Bash
"/c/ProgramData/miniforge3/python.exe" tools/<script>.py
```

Their dependencies (`pillow`, `markdown-it-py`) are installed in `base`
deliberately, so they're available across projects and stay out of
`environment.yml`. The project environment should contain only what the
service needs to *run and be tested* — adding authoring tools there would
make the deployed image carry dependencies it never uses.

To set up on a new machine:

```bash
mamba install -n base -c conda-forge pillow markdown-it-py
```

## Scripts

### `build_showcase.py`

Regenerates the shareable HTML showcase from `docs/system-overview.md`.

```bash
"/c/ProgramData/miniforge3/python.exe" tools/build_showcase.py [output.html]
```

The published artifact is *derived* from the markdown and must never be
hand-edited — it drifted several revisions out of date once because it
had been authored separately. Run this after changing the overview, then
republish the output.

Handles two things a plain markdown render gets wrong: mermaid fences are
stashed before rendering (markdown-it would HTML-escape them and break the
diagrams) and reinserted as native blocks, and images are inlined as
base64 data URIs because the artifact CSP blocks every external host.

### `resize_image.py`

General-purpose image downscaling and re-encoding.

```bash
# cap the long edge, overwrite in place
"/c/ProgramData/miniforge3/python.exe" tools/resize_image.py docs/images/*.jpg --max 1800

# preview without writing
... tools/resize_image.py docs/images/*.jpg --max 1400 --dry-run
```

Phone screenshots arrive at full sensor resolution (~1170x2532). Embedded
as base64 they cost ~1.34x their file size, so the showcase page was
1 MB before downscaling and 683 KB after.

Only downscales, and skips writing when re-encoding wouldn't shrink the
file — so repeated runs are safe and won't degrade an image through
successive re-compression. Note it **overwrites in place** by default; the
current `docs/images/` originals are recoverable from git history
(`1e26089`) if a larger size is ever needed.

**Choosing `--max`:** the showcase displays these two screenshots side by
side at roughly 590 CSS px wide. 1800 keeps them crisp on high-DPI
displays; 1400 halves the weight again at the cost of some softness at 2x.
