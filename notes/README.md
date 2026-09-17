# Notes

**Working material, not the product.** Nothing here is part of the manual, and
nothing here is read by the running system. If you are looking for
documentation, it is in [`../docs/`](../docs/) — or, on the lab PC, at
<http://127.0.0.1:8000/manual>.

## The PDF toolchain

`DESIGN.md` and `OPTIONS.md` are manual pages and live in `docs/`; `EPICS.md`
lives here. All three are *also* published as PDFs, because a PDF is what gets
sent to people who are not going to clone a repository. This is where that
happens:

```bash
cd notes && ./render.sh
```

macOS only — it needs Chrome and Ghostscript, neither of which is on the lab
PC. Chrome renders the HTML, then Ghostscript rewrites the PDF. **The
Ghostscript pass is not cosmetic:** without it, Chrome's output has been seen
to print blank after page 1 on macOS while displaying correctly on screen.

| File | |
|---|---|
| `md2pdf.py` | minimal Markdown → styled HTML. Deliberately small; we control the input |
| `render.sh` | regenerates all three PDFs — two sourced from `../docs/`, one from here |
| `design.html`, `options.html`, `epics.html` | intermediates, git-ignored |
| `XAMS-SC-design.pdf` | generated from `../docs/DESIGN.md` |
| `XAMS-slow-control-options.pdf` | generated from `../docs/OPTIONS.md` |
| `XAMS-SC-epics.pdf` | generated from `EPICS.md`, below |

The PDFs are generated but committed: they are what gets sent to people. If one
is ever out of date with its source, **the source wins**.

`md2pdf.py` adds two things to ordinary Markdown — `:::box` / `:::warn` /
`:::note` callout blocks, and `{+}` / `{-}` coloured table cells. MkDocs does
not understand either, so `tools/mkdocs_hooks.py` translates them for the
manual. One syntax, two renderers; if this toolchain is ever retired, delete
that hook and rewrite the blocks in the sources.

## The road not taken

`EPICS.md` specifies the same system built on EPICS, in full, as a genuine
alternative rather than a foil. **The decision went the other way** on 16
September 2026: the CompactDAQ carries 20 of the 30 connected channels, has no
maintained EPICS device support, and so needs a Python soft IOC in either
design — EPICS would have meant maintaining two paradigms for one small system.

It is here rather than in the manual because it describes a system that was
never built. It exists so that the recommendation in `docs/OPTIONS.md` can be
judged rather than taken on trust, and it records the conditions under which
the question should be reopened. If that ever happens, this is the document to
start from.

## Source material

| File | |
|---|---|
| `xams_piping_and_instrumentation.pdf` | the P&ID (Sarfemijn and Sluitman, 17 May 2024). Authoritative for instrument tag names, and the drawing `tools/build_mimic.py` turns into the web UI's live mimic |
| `labviewPC/` | exported material from the LabVIEW PC. Reference only, git-ignored, never part of the build |
