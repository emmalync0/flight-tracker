#!/bin/bash
# Daily flight price check + push to GitHub Pages
DIR="$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH"

"$DIR/.venv/bin/python" "$DIR/check_prices.py" >> "$DIR/check.log" 2>&1

git -C "$DIR" add data.js price_history.json
git -C "$DIR" commit -m "daily price update"
git -C "$DIR" push >> "$DIR/check.log" 2>&1
