"""
Regenerate the shareable HTML showcase from docs/current/system-overview.md.

The artifact is *derived* from the markdown and never hand-edited --
otherwise the two drift apart, which happened once already. Run this after
changing the overview, then republish the output.

    python tools/build_showcase.py [output.html]

Two things need special handling:
  - Mermaid fences are stashed before rendering, because markdown-it would
    HTML-escape them and break the diagram. They're reinserted afterwards
    as native <pre class="mermaid"> blocks.
  - Images are inlined as data URIs, because the artifact CSP blocks
    requests to any external host.
"""
import base64
import io
import os
import re
import sys

from markdown_it import MarkdownIt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs", "system-overview.md")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "showcase.html")

IMAGES = ("digest-briefing", "digest-sources")

CSS = """
:root{
  --paper:#F6F7F9; --raised:#FFFFFF; --ink:#16202B; --muted:#5C6B7A;
  --rule:#D8DDE4; --accent:#1B4F8F; --accent-soft:#E8EFF7;
  --signal:#9A5B00; --signal-soft:#FBF0DC;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","Roboto Mono",Menlo,Consolas,monospace;
  --serif:ui-serif,Georgia,"Iowan Old Style","Source Serif Pro","Times New Roman",serif;
  --measure:68ch;
}
@media (prefers-color-scheme:dark){
  :root{--paper:#10161D;--raised:#161E27;--ink:#E6EBF0;--muted:#93A2B2;
        --rule:#263442;--accent:#7FB3F0;--accent-soft:#17293D;
        --signal:#E0A040;--signal-soft:#2A2113;}
}
:root[data-theme="dark"]{--paper:#10161D;--raised:#161E27;--ink:#E6EBF0;--muted:#93A2B2;
  --rule:#263442;--accent:#7FB3F0;--accent-soft:#17293D;--signal:#E0A040;--signal-soft:#2A2113;}
:root[data-theme="light"]{--paper:#F6F7F9;--raised:#FFFFFF;--ink:#16202B;--muted:#5C6B7A;
  --rule:#D8DDE4;--accent:#1B4F8F;--accent-soft:#E8EFF7;--signal:#9A5B00;--signal-soft:#FBF0DC;}

body{background:var(--paper);color:var(--ink);font-family:var(--serif);
  font-size:clamp(1rem,.97rem + .15vw,1.075rem);line-height:1.65;-webkit-font-smoothing:antialiased;}
main{max-width:78rem;margin:0 auto;padding:clamp(2rem,5vw,4.5rem) clamp(1.1rem,4vw,3rem) 6rem;}

h1.doc-title{font-size:clamp(2rem,1.7rem + 1.5vw,3rem);line-height:1.08;font-weight:600;
  letter-spacing:-.02em;text-wrap:balance;max-width:22ch;border:0;margin:0;padding:0;}
h1.doc-title + h3{margin:.7rem 0 0;font-family:var(--mono);font-weight:500;
  font-size:clamp(.78rem,.76rem + .1vw,.84rem);text-transform:uppercase;
  letter-spacing:.16em;color:var(--muted);}
h1.doc-title + h3 + p{margin-top:1.3rem;font-size:clamp(1.2rem,1.13rem + .35vw,1.4rem);
  line-height:1.45;max-width:46ch;padding-bottom:2rem;border-bottom:2px solid var(--ink);}

h1:not(.doc-title){font-family:var(--mono);font-size:clamp(1.1rem,1rem + .3vw,1.3rem);
  text-transform:uppercase;letter-spacing:.12em;font-weight:600;color:var(--accent);
  margin:4.5rem 0 0;padding-top:1.1rem;border-top:2px solid var(--ink);text-wrap:balance;}
h2{font-size:clamp(1.5rem,1.36rem + .7vw,1.9rem);line-height:1.15;font-weight:600;
  letter-spacing:-.015em;margin:2.8rem 0 1rem;text-wrap:balance;max-width:32ch;}
h3{font-family:var(--mono);font-size:clamp(.78rem,.76rem + .1vw,.84rem);text-transform:uppercase;
  letter-spacing:.13em;font-weight:600;color:var(--accent);margin:2.2rem 0 .8rem;}

p,ul,ol{max-width:var(--measure);}
p{margin:1.05rem 0;text-wrap:pretty;}
ul,ol{padding-left:1.15rem;margin:1.05rem 0;}
li+li{margin-top:.5rem;}
li::marker{color:var(--accent);}
strong{font-weight:600;}
hr{border:0;border-top:1px solid var(--rule);margin:2.5rem 0;max-width:var(--measure);}
a{color:var(--accent);text-underline-offset:.15em;}
a:focus-visible{outline:2px solid var(--accent);outline-offset:3px;}

code{font-family:var(--mono);font-size:.88em;background:var(--accent-soft);padding:.1em .34em;}
pre{font-family:var(--mono);font-size:clamp(.78rem,.76rem + .1vw,.84rem);line-height:1.6;
  background:var(--raised);border:1px solid var(--rule);border-left:3px solid var(--accent);
  padding:1.1rem 1.25rem;overflow-x:auto;margin:1.3rem 0;}
pre code{background:none;padding:0;font-size:1em;}

blockquote{margin:1.6rem 0;padding:1.1rem 1.4rem;border-left:3px solid var(--signal);
  background:var(--signal-soft);max-width:60ch;}
blockquote p{margin:.5rem 0;}
blockquote p:first-child{margin-top:0;}
blockquote p:last-child{margin-bottom:0;}

.table-scroll{overflow-x:auto;margin:1.5rem 0;border-top:2px solid var(--ink);
  border-bottom:1px solid var(--rule);}
table{width:100%;border-collapse:collapse;font-size:clamp(.78rem,.76rem + .1vw,.84rem);
  line-height:1.5;font-variant-numeric:tabular-nums;}
thead th{font-family:var(--mono);font-size:.76rem;text-transform:uppercase;letter-spacing:.09em;
  font-weight:600;color:var(--muted);text-align:left;padding:.7rem 1rem .7rem 0;
  border-bottom:1px solid var(--rule);vertical-align:bottom;}
tbody td{padding:.85rem 1rem .85rem 0;border-bottom:1px solid var(--rule);vertical-align:top;}
tbody tr:last-child td{border-bottom:none;}
td:last-child,th:last-child{padding-right:0;}
td:first-child strong{font-family:var(--mono);font-size:.82rem;letter-spacing:.02em;}

.diagram{margin:1.8rem 0;padding:1.5rem 1rem;background:var(--raised);
  border:1px solid var(--rule);overflow-x:auto;}
.diagram pre.mermaid{background:none;border:none;padding:0;text-align:center;margin:0;}

img{max-width:100%;height:auto;border:1px solid var(--rule);}
p[align="center"]{max-width:none;display:flex;gap:1.5rem;justify-content:center;flex-wrap:wrap;}

@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;}}
@media (max-width:40rem){p[align="center"]{gap:.75rem;}}
"""


def render_body(src):
    """Markdown -> HTML, with mermaid preserved and images inlined."""
    blocks = []

    def stash(match):
        blocks.append(match.group(1))
        return "\n\nMMDPLACEHOLDER" + str(len(blocks) - 1) + "\n\n"

    src = re.sub(r"```mermaid\n(.*?)```", stash, src, flags=re.S)

    for name in IMAGES:
        path = os.path.join(ROOT, "docs", "images", name + ".jpg")
        encoded = base64.b64encode(io.open(path, "rb").read()).decode()
        src = src.replace(
            'src="images/' + name + '.jpg"',
            'src="data:image/jpeg;base64,' + encoded + '"',
        )

    md = MarkdownIt("gfm-like", {"html": True, "linkify": False})
    md.disable("linkify")
    body = md.render(src)

    for i, block in enumerate(blocks):
        wrapped = '<div class="diagram"><pre class="mermaid">' + block + "</pre></div>"
        for pattern in ("<p>MMDPLACEHOLDER" + str(i) + "</p>", "MMDPLACEHOLDER" + str(i)):
            if pattern in body:
                body = body.replace(pattern, wrapped)
                break

    # the first h1 is the document title; the rest are part dividers
    body = body.replace("<h1>", '<h1 class="doc-title">', 1)
    # wide tables scroll in their own container so the page never scrolls sideways
    return re.sub(
        r"(<table>.*?</table>)", r'<div class="table-scroll">\1</div>', body, flags=re.S
    )


def main():
    body = render_body(io.open(SRC, encoding="utf-8").read())
    html = (
        "<title>Autonomous Technology-Trend Intelligence Agent</title>\n"
        "<style>" + CSS + "</style>\n"
        "<main>\n" + body + "\n</main>\n"
    )
    io.open(OUT, "w", encoding="utf-8").write(html)
    print("wrote " + OUT + " (" + str(round(len(html) / 1024)) + " KB)")


if __name__ == "__main__":
    main()
