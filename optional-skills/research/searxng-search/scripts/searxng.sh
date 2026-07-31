#!/bin/bash
# Usage: ./searxng.sh <query> [max_results] [engines]
# Example: ./searxng.sh "python async" 10 "google,bing"
#
# SEARXNG_URL may include query params (e.g. ?p_token=abc for reverse-proxy
# auth).  This script preserves them and appends the search-specific params.

QUERY="${1:-}"
MAX="${2:-5}"
ENGINES="${3:-google,bing}"

if [ -z "$SEARXNG_URL" ]; then
    echo "Error: SEARXNG_URL is not set"
    exit 1
fi

if [ -z "$QUERY" ]; then
    echo "Usage: $0 <query> [max_results] [engines]"
    exit 1
fi

ENCODED_QUERY=$(echo "$QUERY" | sed 's/ /+/g')

# Split SEARXNG_URL into base and existing query string
BASE_URL="${SEARXNG_URL%%\?*}"
EXISTING_QUERY="${SEARXNG_URL#*\?}"
# If no '?' in URL, EXISTING_QUERY equals the full URL — reset to empty
[ "$EXISTING_QUERY" = "$SEARXNG_URL" ] && EXISTING_QUERY=""

SEARCH_PARAMS="q=${ENCODED_QUERY}&format=json&limit=${MAX}&engines=${ENGINES}"

if [ -n "$EXISTING_QUERY" ]; then
    FULL_URL="${BASE_URL}/search?${EXISTING_QUERY}&${SEARCH_PARAMS}"
else
    FULL_URL="${BASE_URL}/search?${SEARCH_PARAMS}"
fi

curl -s --max-time 10 "$FULL_URL"
