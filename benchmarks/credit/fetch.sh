#!/usr/bin/env bash
# The German Credit data (UCI Statlog, CC BY 4.0): 1,000 loan applications with the bank's real good/bad decision.
set -euo pipefail
cd "$(dirname "$0")"
curl -sL -o german.zip "https://archive.ics.uci.edu/static/public/144/statlog+german+credit+data.zip"
unzip -o -q german.zip german.data german.doc
rm german.zip
echo "german.data: $(wc -l < german.data) rows"
