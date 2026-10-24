#!/usr/bin/env bash
# Build the offline Windows app: dist/Contract Note Extractor/Contract Note Extractor.exe
# (+ a zip of that folder). Needs Go, Node/npm and Python 3. Run from anywhere.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP="Contract Note Extractor"
WEB="$HERE/web"
DIST="$HERE/dist"
DEPS="$(mktemp -d)"
trap 'rm -rf "$DEPS"' EXIT

echo "• Fetching libraries (pdf.js, SheetJS, fonts)…"
(cd "$DEPS" && echo '{"name":"cne-deps","private":true}' > package.json &&
  npm install --silent --no-audit --no-fund \
    pdfjs-dist@3.11.174 xlsx@0.18.5 \
    @fontsource/inter@5 @fontsource/space-grotesk@5 @fontsource/ibm-plex-mono@5 >/dev/null)

echo "• Assembling web files…"
rm -rf "$WEB" && mkdir -p "$WEB/vendor/fonts"
cp "$DEPS/node_modules/pdfjs-dist/build/pdf.min.js"        "$WEB/vendor/"
cp "$DEPS/node_modules/pdfjs-dist/build/pdf.worker.min.js" "$WEB/vendor/"
cp "$DEPS/node_modules/xlsx/dist/xlsx.full.min.js"         "$WEB/vendor/"
FONTS_CSS="$WEB/vendor/fonts.css"; : > "$FONTS_CSS"
add_font () {  # package family weights...
  local pkg="$1" family="$2"; shift 2
  for w in "$@"; do
    cp "$DEPS/node_modules/@fontsource/$pkg/files/$pkg-latin-$w-normal.woff2" "$WEB/vendor/fonts/"
    printf '@font-face{font-family:"%s";font-style:normal;font-weight:%s;font-display:swap;src:url(fonts/%s-latin-%s-normal.woff2) format("woff2")}\n' \
      "$family" "$w" "$pkg" "$w" >> "$FONTS_CSS"
  done
}
add_font inter         "Inter"          400 500 600
add_font space-grotesk "Space Grotesk"  500 600 700
add_font ibm-plex-mono "IBM Plex Mono"  400 500 600

# Same index.html as the web version; only the CDN links are pointed at the
# bundled copies and the launcher keep-alive is added.
python3 - "$ROOT/index.html" "$WEB/index.html" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
h = open(src, encoding="utf-8").read()
def sub(pattern, repl, flags=0):
    global h
    h, n = re.subn(pattern, repl, h, flags=flags)
    if not n: sys.exit("build: pattern not found: " + pattern)
sub(r'\s*<link rel="preconnect"[^>]*>', '')
sub(r'<link href="https://fonts\.googleapis\.com/[^"]*" rel="stylesheet" />',
    '<link href="vendor/fonts.css" rel="stylesheet" />')
sub(r'https://cdnjs\.cloudflare\.com/ajax/libs/pdf\.js/3\.11\.174/pdf\.min\.js', 'vendor/pdf.min.js')
sub(r'https://cdnjs\.cloudflare\.com/ajax/libs/pdf\.js/3\.11\.174/pdf\.worker\.min\.js', 'vendor/pdf.worker.min.js')
sub(r'https://cdnjs\.cloudflare\.com/ajax/libs/xlsx/0\.18\.5/xlsx\.full\.min\.js', 'vendor/xlsx.full.min.js')
if re.search(r'https?://(cdnjs|fonts\.g)', h): sys.exit("build: an online link is left in index.html")
keepalive = '''<script>
/* Desktop app: tell the local launcher this window is open (it exits a few
   minutes after the last window closes). */
(function(){
  const beat = () => fetch("/__cne/beat", { method:"POST", cache:"no-store" }).catch(()=>{});
  beat(); setInterval(beat, 20000);
  addEventListener("pagehide", () => { try { navigator.sendBeacon("/__cne/bye"); } catch(e){} });
})();
</script>
</body>'''
sub(r'</body>', keepalive.replace('\\', '\\\\'))
open(dst, "w", encoding="utf-8").write(h)
PY

echo "• Compiling Windows executable…"
rm -rf "$DIST" && mkdir -p "$DIST/$APP"
(cd "$HERE" && CGO_ENABLED=0 GOOS=windows GOARCH=amd64 \
  go build -trimpath -ldflags "-s -w -H windowsgui" -o "$DIST/$APP/$APP.exe" .)
cp "$HERE/README.txt" "$DIST/$APP/README.txt"
(cd "$DIST" && rm -f "$APP.zip" && zip -qr "$APP.zip" "$APP")

echo "✓ Built:"
ls -lh "$DIST/$APP/$APP.exe" "$DIST/$APP.zip"
