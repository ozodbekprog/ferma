#!/usr/bin/env bash
# ferma-all.pyz — src/ papkasidan bitta faylli paket yig'adi (zipapp).
set -euo pipefail
cd "$(dirname "$0")"
python3 -m zipapp src -o ferma-all.pyz -p "/usr/bin/env python3" -c
chmod +x ferma-all.pyz
echo "✅ ferma-all.pyz tayyor: $(wc -c < ferma-all.pyz) bayt"
