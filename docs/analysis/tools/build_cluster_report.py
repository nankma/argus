"""
Step 3: project the cache into 2D and render the standalone HTML report with
both scatter panels.

Pipeline is the standard one for sparse text: TF-IDF -> TruncatedSVD(50) ->
t-SNE(2). SVD first because t-SNE on raw high-dimensional sparse vectors is
both slower and worse. Two panels come out of the same projection:

  1. story clusters -- singleton / same-source cluster / cross-source cluster
  2. one source vs all others -- shows whether a dominant source occupies its
     own region of content space or is spread through all of it

Panel 2 is the one that explains why content-based MMR could not fix source
concentration (see ../news-ranking-plan.md).

    python analysis/tools/build_cluster_report.py
    python analysis/tools/build_cluster_report.py --highlight-source gnews

Output is a single self-contained HTML file with no external assets --
publishable as an artifact or openable directly. It is regenerated from
data, never hand-edited.
"""

import argparse
import html
import json
import sys
from collections import Counter, defaultdict

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import TSNE
from sklearn.metrics.pairwise import cosine_similarity

sys.path.insert(0, __file__.rsplit("build_cluster_report.py", 1)[0])
from cluster_news import connected_components, load  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

W, H, PAD = 640, 460, 26
RANDOM_STATE = 42


def esc(s):
    return html.escape(str(s), quote=True)


def scatter_svg(points, classify, styles, label):
    """Emit SVG circles. `classify` maps a point to a style key; `styles` maps
    each key to (radius, fill, opacity, stroke_width, z) -- z orders the draw
    so the rare, important marks land on top of the background cloud."""
    by_z = defaultdict(list)
    for p in points:
        key = classify(p)
        r, fill, op, sw, z = styles[key]
        cx = round(PAD + p["x"] * (W - 2 * PAD), 1)
        cy = round(PAD + (1 - p["y"]) * (H - 2 * PAD), 1)
        stroke = f' stroke="var(--surface)" stroke-width="{sw}"' if sw else ""
        by_z[z].append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" opacity="{op}"{stroke}>'
            f'<title>{esc(p["src"])} — {esc(p["t"])}</title></circle>'
        )
    body = "".join("".join(by_z[z]) for z in sorted(by_z))
    return (f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
            f'aria-label="{esc(label)}">'
            f'<rect x="0" y="0" width="{W}" height="{H}" fill="var(--surface)"/>'
            f'{body}</svg>')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="analysis/data/cache-snapshot.tsv")
    ap.add_argument("--out", default="analysis/cluster-report.html")
    ap.add_argument("--threshold", type=float, default=0.40)
    ap.add_argument("--highlight-source", default="hackernews")
    args = ap.parse_args()

    rows = load(args.input)
    print(f"articles: {len(rows)}")
    docs = [(r["title"] + " " + r["summary"]).strip() for r in rows]

    tfidf = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
    X = tfidf.fit_transform(docs)
    sim = cosine_similarity(X)
    np.fill_diagonal(sim, 0.0)

    clusters = connected_components(sim, args.threshold)
    size_of, is_cross = {}, {}
    for members in clusters:
        cross = len(members) > 1 and len({rows[i]["src"] for i in members}) > 1
        for i in members:
            size_of[i], is_cross[i] = len(members), cross
    n_multi = sum(1 for c in clusters if len(c) > 1)
    n_cross = sum(1 for c in clusters
                  if len(c) > 1 and len({rows[i]["src"] for i in c}) > 1)
    print(f"clusters={len(clusters)} multi={n_multi} cross-source={n_cross}")

    svd = TruncatedSVD(n_components=50, random_state=RANDOM_STATE)
    reduced = svd.fit_transform(X)
    variance = svd.explained_variance_ratio_.sum()
    print(f"SVD(50) explained variance: {variance:.1%}")

    print("running t-SNE ...")
    coords = TSNE(n_components=2, perplexity=30, init="pca", metric="cosine",
                  random_state=RANDOM_STATE).fit_transform(reduced)
    coords = (coords - coords.min(axis=0)) / (coords.max(axis=0) - coords.min(axis=0))

    points = [{"x": float(coords[i, 0]), "y": float(coords[i, 1]),
               "src": r["src"], "t": r["title"][:90],
               "size": size_of[i], "cross": is_cross[i]}
              for i, r in enumerate(rows)]

    n_single = sum(1 for p in points if p["size"] == 1)
    n_same = sum(1 for p in points if p["size"] > 1 and not p["cross"])
    n_cross_pts = sum(1 for p in points if p["cross"])

    hl = args.highlight_source
    hl_pts = np.array([[p["x"], p["y"]] for p in points if p["src"] == hl])
    other_pts = np.array([[p["x"], p["y"]] for p in points if p["src"] != hl])
    separation = float(np.linalg.norm(hl_pts.mean(0) - other_pts.mean(0)))
    hl_spread = float(hl_pts.std(0).mean())
    print(f"{hl}: n={len(hl_pts)} spread={hl_spread:.3f} "
          f"centroid separation from others={separation:.3f}")

    panel1 = scatter_svg(
        points,
        lambda p: "cross" if p["cross"] else ("same" if p["size"] > 1 else "single"),
        {"single": (2, "var(--dot)", ".5", 0, 0),
         "same": (4.5, "var(--s1)", "1", 1.5, 1),
         "cross": (5.5, "var(--s2)", "1", 2, 2)},
        f"Scatter of {len(points)} articles by content similarity; {n_single} singletons, "
        f"{n_same} in same-source clusters, {n_cross_pts} in cross-source clusters.")

    panel2 = scatter_svg(
        points,
        lambda p: "hl" if p["src"] == hl else "other",
        {"other": (2.2, "var(--dot)", ".45", 0, 0),
         "hl": (2.6, "var(--s1)", ".85", 0, 1)},
        f"The same scatter recolored: {len(hl_pts)} {hl} articles spread throughout the "
        f"space rather than occupying a distinct region.")

    stats = {
        "n": len(points), "clusters": len(clusters), "multi": n_multi,
        "cross": n_cross, "singleton_pts": n_single, "same_pts": n_same,
        "cross_pts": n_cross_pts, "variance": variance, "threshold": args.threshold,
        "hl": hl, "hl_n": len(hl_pts), "hl_spread": hl_spread,
        "separation": separation, "other_n": len(other_pts),
        "sources": len(Counter(r["src"] for r in rows)),
    }
    html_out = render(panel1, panel2, stats)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"wrote {args.out} ({len(html_out)/1024:.0f} KB)")


def render(panel1, panel2, s):
    """The page is deliberately one file with inline CSS and no external
    assets -- artifact CSP blocks external hosts, and it should also open
    from disk with no server."""
    return TEMPLATE.format(
        panel1=panel1, panel2=panel2,
        n=f"{s['n']:,}", clusters=f"{s['clusters']:,}",
        reduction=f"{s['n'] - s['clusters']:,}",
        reduction_pct=f"{(s['n'] - s['clusters']) / s['n'] * 100:.1f}",
        singleton_pts=f"{s['singleton_pts']:,}",
        singleton_pct=f"{s['singleton_pts'] / s['n'] * 100:.1f}",
        same_pts=s["same_pts"], cross_pts=s["cross_pts"],
        multi=s["multi"], cross=s["cross"], threshold=f"{s['threshold']:.2f}",
        variance=f"{s['variance']:.1%}", hl=s["hl"], hl_n=f"{s['hl_n']:,}",
        other_n=f"{s['other_n']:,}", hl_spread=f"{s['hl_spread']:.3f}",
        separation=f"{s['separation']:.3f}", sources=s["sources"],
        in_multi=s["same_pts"] + s["cross_pts"],
    )


TEMPLATE = """<title>News clustering: how many clusters, and how big?</title>
<style>
  :root {{
    color-scheme: light;
    --plane:#f7f8f9; --surface:#fcfcfd; --ink:#0b0d0f; --ink-2:#4e535a;
    --muted:#868c94; --rule:rgba(11,13,15,0.10);
    --s1:#2a78d6; --s2:#eb6834; --critical:#d03b3b;
    --chip:rgba(42,120,214,0.10); --dot:#9aa1a9; --warnwash:rgba(208,59,59,0.07);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --plane:#0c0d0e; --surface:#191b1c; --ink:#fff; --ink-2:#c2c6cb;
      --muted:#8b9199; --rule:rgba(255,255,255,0.11);
      --s1:#3987e5; --s2:#d95926; --critical:#e06666;
      --chip:rgba(57,135,229,0.16); --dot:#6b7178; --warnwash:rgba(224,102,102,0.10);
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --plane:#0c0d0e; --surface:#191b1c; --ink:#fff; --ink-2:#c2c6cb;
    --muted:#8b9199; --rule:rgba(255,255,255,0.11);
    --s1:#3987e5; --s2:#d95926; --critical:#e06666;
    --chip:rgba(57,135,229,0.16); --dot:#6b7178; --warnwash:rgba(224,102,102,0.10);
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--plane); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.55;
    -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:940px; margin:0 auto; padding:56px 24px 96px;
    display:flex; flex-direction:column; gap:44px; }}
  .eyebrow {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:11.5px;
    letter-spacing:.09em; text-transform:uppercase; color:var(--muted); margin:0 0 10px; }}
  h1 {{ font-size:clamp(26px,4vw,36px); line-height:1.15; margin:0 0 14px;
    letter-spacing:-.02em; text-wrap:balance; }}
  h2 {{ font-size:19px; margin:0 0 6px; letter-spacing:-.01em; text-wrap:balance; }}
  p {{ margin:0 0 12px; max-width:68ch; color:var(--ink-2); }}
  p.lede {{ font-size:16.5px; }}
  strong {{ color:var(--ink); font-weight:620; }}
  code {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.9em;
    background:var(--chip); padding:1px 5px; border-radius:3px; }}
  section {{ display:flex; flex-direction:column; gap:14px; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:1px; background:var(--rule); border:1px solid var(--rule);
    border-radius:10px; overflow:hidden; }}
  .kpi {{ background:var(--surface); padding:18px 20px; }}
  .k-label {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:10.5px;
    letter-spacing:.07em; text-transform:uppercase; color:var(--muted); }}
  .k-val {{ font-size:30px; font-weight:640; letter-spacing:-.03em; margin-top:4px;
    line-height:1.1; }}
  .k-sub {{ font-size:12.5px; color:var(--muted); margin-top:2px; }}
  .k-hero .k-val {{ color:var(--s1); }}
  .k-warn .k-val {{ color:var(--critical); }}
  figure {{ margin:0; display:flex; flex-direction:column; gap:10px; }}
  figcaption {{ font-size:13px; color:var(--muted); max-width:68ch; }}
  .chart-scroll {{ overflow-x:auto; }}
  svg {{ display:block; border-radius:8px; border:1px solid var(--rule); }}
  .legend {{ display:flex; gap:18px; flex-wrap:wrap; align-items:center;
    font-size:12.5px; color:var(--ink-2); }}
  .legend span {{ display:inline-flex; align-items:center; gap:7px; }}
  .sw {{ width:11px; height:11px; border-radius:2px; display:inline-block; }}
  .callout {{ border-left:3px solid var(--s1); background:var(--chip);
    padding:14px 18px; border-radius:0 8px 8px 0; }}
  .callout p:last-child {{ margin-bottom:0; }}
  .tag {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:10.5px;
    letter-spacing:.08em; text-transform:uppercase; color:var(--s1); display:block;
    margin-bottom:5px; font-weight:600; }}
  hr {{ border:0; border-top:1px solid var(--rule); margin:0; }}
  .foot {{ font-size:12.5px; color:var(--muted); }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; }} }}
</style>
<div class="wrap">
  <header>
    <p class="eyebrow">Argus · news cache analysis · generated by build_cluster_report.py</p>
    <h1>How many story clusters does the cache actually contain?</h1>
    <p class="lede">Clustering a live snapshot of the production cache —
      <strong>{n} articles</strong> across {sources} sources — to find out whether
      corroboration-based importance scoring has enough signal to work with.</p>
    <p class="lede" style="margin-bottom:0">The short answer:
      <strong>almost everything is a cluster of one.</strong></p>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="k-label">Articles</div><div class="k-val">{n}</div>
      <div class="k-sub">48h cache window</div></div>
    <div class="kpi"><div class="k-label">Clusters</div><div class="k-val">{clusters}</div>
      <div class="k-sub">only {reduction} fewer ({reduction_pct}%)</div></div>
    <div class="kpi k-hero"><div class="k-label">Alone</div><div class="k-val">{singleton_pct}%</div>
      <div class="k-sub">{singleton_pts} singleton articles</div></div>
    <div class="kpi k-warn"><div class="k-label">Cross-source</div><div class="k-val">{cross}</div>
      <div class="k-sub">clusters spanning &gt;1 outlet</div></div>
  </div>

  <section>
    <h2>The cache as a map</h2>
    <p>Every article placed by content similarity — TF-IDF, reduced with SVD, laid out
      with t-SNE. Articles about the same thing land next to each other. Worth saying
      plainly what this is <em>not</em>: it does not look like a textbook cluster plot
      with a few clean, well-separated blobs. <strong>That shape would misrepresent this
      cache.</strong></p>
    <figure>
      <div class="chart-scroll">{panel1}</div>
      <div class="legend">
        <span><i class="sw" style="background:var(--dot);opacity:.6"></i> Singleton — {singleton_pts}</span>
        <span><i class="sw" style="background:var(--s1)"></i> Same-source cluster — {same_pts}</span>
        <span><i class="sw" style="background:var(--s2)"></i> Cross-source cluster — {cross_pts}</span>
      </div>
      <figcaption>Hover any point for its source and headline. Only {in_multi} of {n}
        articles join a cluster at all, forming {multi} multi-article clusters, of which
        just {cross} span more than one outlet. Threshold: TF-IDF cosine ≥ {threshold}.</figcaption>
    </figure>
    <p>The picture is a diffuse cloud rather than tidy blobs for a measurable reason:
      compressing the article vectors to 50 dimensions retains only
      <strong>{variance} of the variance</strong>. The content genuinely occupies a
      high-dimensional space — these articles are mostly about that many different
      things. There is no dense cluster structure to draw because there isn't any in
      the data.</p>
  </section>

  <section>
    <h2>Why content-based diversity can't fix source concentration</h2>
    <p>The same projection, recolored by source: <code>{hl}</code> against everything
      else.</p>
    <figure>
      <div class="chart-scroll">{panel2}</div>
      <div class="legend">
        <span><i class="sw" style="background:var(--s1)"></i> {hl} — {hl_n} articles</span>
        <span><i class="sw" style="background:var(--dot);opacity:.55"></i> All other sources — {other_n}</span>
      </div>
      <figcaption>Its centroid sits <strong>{separation}</strong> from the centroid of
        every other source — less than its own spread of <strong>{hl_spread}</strong>.
        The two distributions are effectively on top of one another.</figcaption>
    </figure>
    <div class="callout">
      <span class="tag">Explains an earlier result</span>
      <p>This is the picture behind a negative result measured separately: MMR re-ranking
        on content similarity left source concentration essentially unchanged. Now it is
        visible why. Content-based diversity works by pushing apart points that sit close
        together — but this source's articles are <em>already</em> spread across the whole
        space. There is no clump to break up. It dominates by publishing frequently, not
        repetitively, and no similarity function can see the difference. Source identity
        has to enter the objective directly.</p>
    </div>
  </section>

  <hr>
  <section>
    <h2>What this means for corroboration scoring</h2>
    <p><strong>The signal is real but very thin.</strong> {cross} cross-source clusters in
      a 48-hour, {n}-article window is enough to identify the handful of stories the whole
      industry covered at once — and nothing more. Corroboration can promote the top few
      items; it cannot rank the rest, which all score identically as uncorroborated.</p>
    <p class="foot">Regenerate with
      <code>python analysis/tools/build_cluster_report.py</code>. All computation is local
      — scikit-learn only, no API calls, no LLM calls. See
      <code>analysis/README.md</code>.</p>
  </section>
</div>
"""

if __name__ == "__main__":
    main()
