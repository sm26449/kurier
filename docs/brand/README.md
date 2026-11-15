# Brand assets

Kurier shares its visual language with [Klopot](https://github.com/sm26449/klopot):
JetBrains Mono Bold (700) set lowercase and converted to outlines, the amber
`#FFB31A` signature, and a mark built from ring segments and detached
satellites.

Where Klopot's satellite *leaves* its ring, Kurier's mark is the **relay**: two
ring segments on one orbit, each led by a detached satellite — a message going
out and a message coming back, carried both ways. In the wordmark the satellite
is the dot of the `i`.

| File | Purpose |
|------|---------|
| `kurier-mark.svg` | App icon: the relay on an ink tile (≥ 24 px) |
| `kurier-favicon.svg` | Favicon variant (< 24 px): thickened and simplified. Served by the status page as `src/ui/favicon.svg` — keep the two identical |
| `kurier-symbol.svg` | The relay alone, amber on transparent |
| `kurier-mark-mono.svg` | The relay in `currentColor`, no tile (docs headers, print) |
| `kurier-wordmark.svg` / `kurier-wordmark-dark.svg` | `kurier` wordmark — light / dark |
| `social-preview.svg` | 1280×640 social card (symbol + wordmark + tagline on ink); rasterise to PNG for GitHub |

## Geometry

Mark, in a 512 box centred on (256, 256): orbit radius 112, stroke 45, clean
radial cuts. Satellites Ø68 sit on the orbit at 1:30 and 7:30 — Klopot's
axis. Each segment trails its satellite from 31° to 150° behind it, so the gap
ahead of a satellite matches the gap behind it. Wordmark, 1000-unit em: the
`i` keeps its stem and its dot becomes a Ø150 amber satellite.

## Palette

Amber `#FFB31A` · ink `#0C1016` · paper `#F5F6F8` · secondary `#ADB4BF` (on
dark) / `#46505C` (on light). Letters are paper on dark and ink on light; the
mark and the `i` dot are amber on every background. The amber is logo-only: in
the status page, amber-ish `--warn` keeps meaning "warning".

## Usage

- Keep both gaps open and both satellites detached; do not rotate, recolour,
  add shadows or close the ring.
- Clear space: one quarter of the mark's or wordmark's height.
- In a README, swap the wordmark by colour scheme:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/kurier-wordmark-dark.svg">
  <img src="docs/brand/kurier-wordmark.svg" alt="Kurier" height="48">
</picture>
```

No rasteriser is bundled: `rsvg-convert -w 1280 social-preview.svg >
social-preview.png` (the tagline uses Inter; install it or outline it first).
