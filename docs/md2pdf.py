#!/usr/bin/env python3
"""Minimal Markdown -> styled HTML for the XAMS design dossier.
Handles: headings, paragraphs, ul/ol, tables, fenced code, hr,
inline code/bold/italic. Deliberately small; we control the input."""
import html, re, sys, pathlib

CSS = """
@page { size: A4; margin: 18mm 16mm 16mm 16mm; }
html { font-size: 10.5pt; }
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; color:#1a1a1a; line-height:1.45; margin:0; }
h1 { font-size:20pt; margin:0 0 3mm 0; letter-spacing:-0.3pt; }
h2 { font-size:13pt; margin:8mm 0 2.5mm 0; padding-bottom:1.2mm; border-bottom:1.2pt solid #1a1a1a; page-break-after:avoid; }
h3 { font-size:11pt; margin:5mm 0 1.5mm 0; page-break-after:avoid; }
p { margin:0 0 2.5mm 0; }
ul,ol { margin:0 0 3mm 0; padding-left:5mm; }
li { margin-bottom:1.2mm; }
table { border-collapse:collapse; width:100%; font-size:9pt; margin:0 0 4mm 0; }
th,td { border:0.5pt solid #b8b8b8; padding:1.6mm 2mm; text-align:left; vertical-align:top; }
th { background:#ececec; font-weight:600; }
thead { display:table-header-group; }
tr { page-break-inside:avoid; }
code { font-family:"SF Mono",Menlo,monospace; font-size:8.8pt; background:#f2f2f2; padding:0.3mm 1mm; border-radius:1.5pt; }
pre { font-family:"SF Mono",Menlo,monospace; font-size:8.3pt; background:#f5f5f5; border-left:2.5pt solid #999;
      padding:2.5mm 3mm; margin:0 0 4mm 0; line-height:1.35; page-break-inside:avoid; }
pre code { background:none; padding:0; font-size:8.3pt; }
hr { border:none; border-top:0.5pt solid #bbb; margin:6mm 0; }
strong { font-weight:600; }
"""

def inline(t):
    t = html.escape(t)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    t = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'(?<![\w*])\*([^*\n]+)\*(?![\w*])', r'<em>\1</em>', t)
    return t

def convert(md):
    out, lines, i = [], md.split('\n'), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith('```'):
            i += 1; buf = []
            while i < len(lines) and not lines[i].startswith('```'):
                buf.append(html.escape(lines[i])); i += 1
            i += 1
            out.append('<pre><code>' + '\n'.join(buf) + '</code></pre>')
            continue
        if re.match(r'^\s*---\s*$', ln):
            out.append('<hr>'); i += 1; continue
        m = re.match(r'^(#{1,4})\s+(.*)$', ln)
        if m:
            lvl = len(m.group(1))
            out.append(f'<h{lvl}>{inline(m.group(2))}</h{lvl}>'); i += 1; continue
        # table
        if '|' in ln and i + 1 < len(lines) and re.match(r'^\s*\|?[\s:|-]+\|[\s:|-]*$', lines[i+1]):
            def cells(r):
                r = r.strip()
                if r.startswith('|'): r = r[1:]
                if r.endswith('|'): r = r[:-1]
                return [c.strip() for c in r.split('|')]
            hdr = cells(ln); i += 2; rows = []
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                rows.append(cells(lines[i])); i += 1
            t = ['<table><thead><tr>'] + [f'<th>{inline(c)}</th>' for c in hdr] + ['</tr></thead><tbody>']
            for r in rows:
                t.append('<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in r) + '</tr>')
            t.append('</tbody></table>')
            out.append(''.join(t)); continue
        m = re.match(r'^(\s*)([-*])\s+(.*)$', ln)
        if m:
            items = []
            while i < len(lines):
                mm = re.match(r'^(\s*)([-*])\s+(.*)$', lines[i])
                if not mm: break
                items.append(mm.group(3)); i += 1
                while i < len(lines) and lines[i].startswith('  ') and lines[i].strip() \
                      and not re.match(r'^(\s*)([-*]|\d+\.)\s+', lines[i]):
                    items[-1] += ' ' + lines[i].strip(); i += 1
            out.append('<ul>' + ''.join(f'<li>{inline(x)}</li>' for x in items) + '</ul>'); continue
        m = re.match(r'^(\s*)(\d+)\.\s+(.*)$', ln)
        if m:
            items = []
            while i < len(lines):
                mm = re.match(r'^(\s*)(\d+)\.\s+(.*)$', lines[i])
                if not mm: break
                items.append(mm.group(3)); i += 1
            out.append('<ol>' + ''.join(f'<li>{inline(x)}</li>' for x in items) + '</ol>'); continue
        if not ln.strip():
            i += 1; continue
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(r'^(#{1,4}\s|```|\s*[-*]\s|\s*\d+\.\s)', lines[i]) \
              and not ('|' in lines[i] and i+1 < len(lines) and re.match(r'^\s*\|?[\s:|-]+\|[\s:|-]*$', lines[i+1])):
            buf.append(lines[i]); i += 1
        if buf:
            out.append('<p>' + inline(' '.join(buf)) + '</p>')
    return '\n'.join(out)

src = pathlib.Path(sys.argv[1]); dst = pathlib.Path(sys.argv[2])
title = src.stem
body = convert(src.read_text(encoding='utf-8'))
dst.write_text(f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
               f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body>\n{body}\n</body></html>',
               encoding='utf-8')
print(f'{dst} written')
