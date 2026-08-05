#!/usr/bin/env bash
# Point auradefi.info at the GitHub Pages site, in the order that keeps the
# docs reachable throughout.
#
#     CLOUDFLARE_API_TOKEN=... bash scripts/setup_custom_domain.sh
#
# The token needs Zone:DNS:Edit on auradefi.info and nothing else. Create one
# at dash.cloudflare.com/profile/api-tokens using the "Edit zone DNS" template.
# The read-only token wired into the Cloudflare MCP integration cannot do this.
#
# ORDER MATTERS. Setting the custom domain on GitHub first would redirect
# auracarehq.github.io/auradefi to a domain that does not resolve yet, taking
# the published docs offline until DNS propagates. So: DNS, wait for it to
# resolve, then GitHub.
#
# Records are DNS-only (not proxied through Cloudflare) on purpose. GitHub
# needs to see the apex pointing at its own addresses to issue the
# Let's Encrypt certificate that serves https://auradefi.info. Turn the orange
# cloud on afterwards if you want Cloudflare caching, and set SSL mode to
# Full (strict) when you do; Flexible causes a redirect loop.

set -euo pipefail

ZONE_ID="fcd53f43568f3be65c1f169a5c1f1395"     # auradefi.info
DOMAIN="auradefi.info"
REPO="auracarehq/auradefi"
PAGES_HOST="auracarehq.github.io"

# Verified by resolving auracarehq.github.io, not copied from documentation.
APEX_V4=(185.199.108.153 185.199.109.153 185.199.110.153 185.199.111.153)
APEX_V6=(2606:50c0:8000::153 2606:50c0:8001::153 2606:50c0:8002::153 2606:50c0:8003::153)

: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN (needs Zone:DNS:Edit)}"

api() {
  curl -sS -X "$1" \
    "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records${2}" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    -H "Content-Type: application/json" \
    ${3+--data "$3"}
}

existing() {
  api GET "?type=$1&name=$2&per_page=100" \
    | python3 -c 'import json,sys; print(" ".join(r["content"] for r in json.load(sys.stdin)["result"]))'
}

add_record() {
  local type="$1" name="$2" content="$3"
  if [[ " $(existing "$type" "$name") " == *" ${content} "* ]]; then
    echo "    ${type} ${name} -> ${content}  already present"
    return
  fi
  local body
  body=$(python3 -c '
import json, sys
print(json.dumps({"type": sys.argv[1], "name": sys.argv[2], "content": sys.argv[3],
                  "ttl": 1, "proxied": False, "comment": "GitHub Pages"}))' \
    "$type" "$name" "$content")
  if api POST "" "$body" | grep -q '"success":true'; then
    echo "    ${type} ${name} -> ${content}  created"
  else
    echo "    ${type} ${name} -> ${content}  FAILED" >&2
    api POST "" "$body" >&2
    exit 1
  fi
}

echo "==> 1/4  apex A and AAAA records on ${DOMAIN}"
for ip in "${APEX_V4[@]}"; do add_record A "$DOMAIN" "$ip"; done
for ip in "${APEX_V6[@]}"; do add_record AAAA "$DOMAIN" "$ip"; done

echo "==> 2/4  www CNAME"
add_record CNAME "www.${DOMAIN}" "${PAGES_HOST}"

echo "==> 3/4  waiting for ${DOMAIN} to resolve"
for attempt in $(seq 1 40); do
  if python3 -c "import socket,sys; socket.getaddrinfo('${DOMAIN}', None, socket.AF_INET)" 2>/dev/null; then
    echo "    resolves after ${attempt} check(s)"
    break
  fi
  if [[ "$attempt" == 40 ]]; then
    echo "    still not resolving. DNS is in place; re-run step 4 later:" >&2
    echo "    gh api -X PUT repos/${REPO}/pages -f cname=${DOMAIN}" >&2
    exit 1
  fi
  sleep 15
done

echo "==> 4/4  telling GitHub Pages about the domain"
gh api -X PUT "repos/${REPO}/pages" -f "cname=${DOMAIN}" -F "https_enforced=true" \
  || { echo "    GitHub rejected it; check the domain is not claimed elsewhere" >&2; exit 1; }

echo
echo "Done. GitHub now provisions a certificate, which usually takes a few"
echo "minutes and can take up to an hour. Check with:"
echo "  gh api repos/${REPO}/pages --jq '{cname,https_enforced,status}'"
echo "  curl -sI https://${DOMAIN} | head -1"
echo
echo "Then update the absolute links that still point at the old address:"
echo "  README.md (10 of them) and pyproject.toml (1)."
