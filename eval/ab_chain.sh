#!/bin/bash
# Full A/B of the page-review request variants (see eval/ab_review.py). ~3 h on one local server slot.
cd "$(dirname "$0")/.."
P=103,117,121,124,137,158,167,198,101,102,113,116,133,142,146,166,170,186,190,196
run() { uv run --quiet python eval/ab_review.py run "$@" --pages "$P" || exit 1; }
run baseline-1
run lines --set review_payload=lines
run baseline-2
run edit --set review_edit_op=true
run lean-images --set review_renders=when-useful --set review_crops=lean
run baseline-3
run all --set review_payload=lines --set review_edit_op=true \
  --set review_renders=when-useful --set review_crops=lean
echo CHAIN-DONE
