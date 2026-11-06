#!/usr/bin/env bash
# Chrome entry point. The work lives in install.sh so there is one installer to
# keep correct; this pins the browser and passes everything else straight
# through. Brave and Chromium users can run install.sh --browser brave|chromium
# instead, or ./install.sh with no flags to install for every browser found.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT/install.sh" --browser chrome "$@"
