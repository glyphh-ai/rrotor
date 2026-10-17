#!/usr/bin/env sh
# Banking77 (PolyAI, CC BY 4.0): the set's own train and test splits.
set -e
cd "$(dirname "$0")"
curl -sL -o train.csv https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/train.csv
curl -sL -o test.csv https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/test.csv
wc -l train.csv test.csv
