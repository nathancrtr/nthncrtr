#!/usr/bin/env bash
# Reproduce the caddy binary running on natto.
#
# Produces ./caddy matching the binary at /usr/local/bin/caddy on natto:
#   caddy v2.11.2 + caddy-dns/cloudflare v0.2.4
#   linux/arm64, CGO disabled, trimpath, tags: nobadger,nomysql,nopgx
#
# Requirements: go 1.25+ and xcaddy (github.com/caddyserver/xcaddy).
# Install xcaddy with: go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
#
# Run from anywhere; the binary is written to the current directory.

set -euo pipefail

CADDY_VERSION="v2.11.2"
CLOUDFLARE_VERSION="v0.2.4"

export CGO_ENABLED=0
export GOOS=linux
export GOARCH=arm64
export GOARM64=v8.0

xcaddy build "${CADDY_VERSION}" \
	--with "github.com/caddy-dns/cloudflare@${CLOUDFLARE_VERSION}" \
	--with-build-flag="-trimpath" \
	--with-build-flag="-tags=nobadger,nomysql,nopgx"

echo
echo "Built ./caddy. Verify with: ./caddy version"
echo "Deploy: scp caddy natto:/tmp/ && ssh natto 'sudo install -m 0755 /tmp/caddy /usr/local/bin/caddy && sudo systemctl restart caddy'"
