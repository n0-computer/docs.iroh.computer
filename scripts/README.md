# Diagram generators

Python generators for the animated SVGs on
[`/what-is-iroh`](../what-is-iroh.mdx). Each `*.gen.py` writes one SVG into
`../images/how-iroh-works/`.

Regenerate one:

```sh
python3 connect-by-key.gen.py
```

Regenerate all:

```sh
for f in *.gen.py; do python3 "$f"; done
```

Scripts are self-contained (stdlib only). `endpoint-startup.gen.py` additionally
reads `endpoint-startup.land.txt` (world-map path data).

## SMIL only — no CSS animations

The SVGs are embedded via `<img src=...>`. In production, Mintlify serves image
assets with the HTTP header `Content-Security-Policy: default-src 'none'`. With
no `style-src 'unsafe-inline'`, the browser **blocks the SVG's inline `<style>`
element**, so any CSS `@keyframes` animation silently fails to run. The local
`mint dev` server sends no such header, so CSS animations work in preview but
not on the deployed site.

SMIL animation elements (`<animate>`, `<animateTransform>`, `<set>`, `<animateMotion>`)
are **not** governed by `style-src`, so they keep working in production. Drive
all animation through SMIL; do not rely on CSS keyframes or `<style>`-based
animation.

(The one `<style>` block used purely for `background-color: transparent` /
`color-scheme` is fine — it's static, not animation.)

## Testing under production conditions

`mint dev` sends no CSP, so a CSS animation works in local preview but breaks on
the deployed site — preview alone won't catch a regression. `csp_preview.py`
reproduces the production header: it serves the diagrams with
`Content-Security-Policy: default-src 'none'` and embeds them via `<img>`.

```sh
python3 scripts/csp_preview.py        # then open http://localhost:8000/
python3 scripts/csp_preview.py 8080   # override the port if 8000 is busy
```

If a diagram animates there, it will animate in production. If it freezes there
but moves in `mint dev`, it's still relying on CSS/`<style>` animation — convert
it to SMIL.

## Not generated

`images/how-iroh-works/embedding-phone.svg` is static and has no generator
script; edit it by hand.
