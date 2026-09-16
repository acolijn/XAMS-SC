#!/bin/bash
# Regenerate both PDFs from their sources.
#   DESIGN.md      -> XAMS-SC-design.pdf          (via md2pdf.py)
#   sc-options.html-> XAMS-slow-control-options.pdf
# Chrome renders, Ghostscript normalises: without the gs pass the output has been
# seen to print blank after page 1 on macOS.
set -euo pipefail
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
GSOPTS="-sDEVICE=pdfwrite -dCompatibilityLevel=1.4 -dPDFSETTINGS=/prepress -dEmbedAllFonts=true -dNOPAUSE -dBATCH -dQUIET"

python3 md2pdf.py DESIGN.md design.html
"$CHROME" --headless --disable-gpu --no-pdf-header-footer \
    --print-to-pdf="/tmp/_design_raw.pdf" "file://$PWD/design.html" 2>/dev/null
gs $GSOPTS -sOutputFile=XAMS-SC-design.pdf /tmp/_design_raw.pdf

"$CHROME" --headless --disable-gpu --no-pdf-header-footer \
    --print-to-pdf="/tmp/_options_raw.pdf" "file://$PWD/sc-options.html" 2>/dev/null
gs $GSOPTS -sOutputFile=XAMS-slow-control-options.pdf /tmp/_options_raw.pdf

python3 md2pdf.py EPICS.md epics.html
"$CHROME" --headless --disable-gpu --no-pdf-header-footer \
    --print-to-pdf="/tmp/_epics_raw.pdf" "file://$PWD/epics.html" 2>/dev/null
gs $GSOPTS -sOutputFile=XAMS-SC-epics.pdf /tmp/_epics_raw.pdf

echo "rendered:"
ls -la XAMS-SC-design.pdf XAMS-SC-epics.pdf XAMS-slow-control-options.pdf
