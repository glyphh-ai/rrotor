#!/bin/sh
# The dataset: "Web robot detection - Server logs" (Iliou et al., Aristotle University of Thessaloniki), Zenodo
# 3477932, CC BY 4.0. The two feature files only (20 MB); the raw month of logs (3.2 GB, public_v2.json) is not needed.
set -e
cd "$(dirname "$0")" && mkdir -p data && cd data
for f in simple_features.csv semantic_features.csv; do
  [ -f "$f" ] || curl -sL -o "$f" "https://zenodo.org/api/records/3477932/files/$f/content"
done
wc -l simple_features.csv semantic_features.csv
