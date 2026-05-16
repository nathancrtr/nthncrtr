#!/usr/bin/env bash
# Reproduce the caddy binary running on natto.
#
# Produces ./caddy matching the binary at /usr/local/bin/caddy on natto:
#   caddy v2.11.2 + caddy-dns/cloudflare v0.2.4
#   linux/<host arch>, CGO disabled, trimpath, tags: nobadger,nomysql,nopgx
#
# Targets the host architecture (amd64 on the Beelink, arm64 on the Pi) so
# `bootstrap/natto.sh` produces a natively-runnable binary on either. Do NOT
# re-hardcode GOARCH: a cross-compiled caddy silently fails to exec on the
# other arch and takes every *.nthncrtr.com URL down.
#
# Requirements: go 1.25+ and xcaddy (github.com/caddyserver/xcaddy).
# Install xcaddy with: go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
#
# Run from anywhere; the binary is written to the current directory.

set -euo pipefail

CADDY_VERSION="v2.11.2"
CLOUDFLARE_VERSION="v0.2.4"

ARCH=$(dpkg --print-architecture 2>/dev/null || go env GOARCH)   # amd64 | arm64
export CGO_ENABLED=0
export GOOS=linux
export GOARCH="$ARCH"
[ "$ARCH" = arm64 ] && export GOARM64=v8.0

# xcaddy has no --with-build-flag (never did); extra `go build` flags are
# passed through this env var. -trimpath for reproducible paths; the -tags
# drop modules we don't compile in (matches the tags in the header).
export XCADDY_GO_BUILD_FLAGS="-trimpath -tags=nobadger,nomysql,nopgx"

xcaddy build "${CADDY_VERSION}" \
	--with "github.com/caddy-dns/cloudflare@${CLOUDFLARE_VERSION}"

echo
echo "Built ./caddy. Verify with: ./caddy version"
echo "Deploy: scp caddy natto:/tmp/ && ssh natto 'sudo install -m 0755 /tmp/caddy /usr/local/bin/caddy && sudo systemctl restart caddy'"
