# Networking primer

This is a walk through the networking ideas that shape this homelab, written for a programmer who's comfortable with code and abstractions but hasn't run their own infrastructure. Each section gives a tight conceptual explanation and then anchors it in something concrete from this repo. The homelab isn't decoration — it's the worked example, and once you see how the pieces fit you should be able to recognize the same pieces anywhere else.

The architecture diagram lives in the top-level [`README.md`](../README.md). Refer back to it whenever the wiring is more interesting than the prose.

## DNS: how names become addresses

A DNS lookup turns a name like `music.nthncrtr.com` into an IP address. There are two kinds of servers involved. **Authoritative** servers hold the actual records for a zone — for us, that's Cloudflare's nameservers, which own `nthncrtr.com`. **Recursive** servers (sometimes called "resolvers") are what your computer talks to; they walk the tree on your behalf, caching answers along the way. Your laptop almost never talks to an authoritative server directly.

An **A record** is the simplest mapping: name → IPv4 address. There are others (AAAA for IPv6, CNAME for aliasing, MX for mail) but A records are what matter here.

**In this homelab:** every `*.nthncrtr.com` name is its own explicit A record in Cloudflare, all pointing at natto's Tailscale IP `100.122.71.33`. There is deliberately **no** `*.nthncrtr.com` wildcard. The reasoning is operational: a wildcard makes it impossible to tell from the DNS zone alone which subdomains are real, and adding a record by hand once per service forces a moment of attention. A consequence: a brand-new subdomain doesn't resolve at all until someone adds the A record in the Cloudflare dashboard. Caddy will still try to serve it; the name just doesn't go anywhere.

**Negative caching and TTLs.** Resolvers cache positive answers, and they also cache *negative* answers — "this name does not exist." How long depends on the zone's SOA minimum TTL. For `nthncrtr.com` that's 1800 seconds, 30 minutes. So if you query a name *before* its A record exists, the answer "NXDOMAIN" sticks for half an hour on whatever resolver you asked. The classic failure mode: you ask Pi-hole for `newservice.nthncrtr.com` while diagnosing why it's not up, the record is added five minutes later, and you spend the next 25 minutes convinced you broke something. The fix is to wait — restarting Pi-hole to clear the cache would knock out DNS for everyone in the house, which is a worse trade than 30 minutes of patience.

## Public vs private IP addresses

Not all IPs are equal. Three ranges are **private** by convention (RFC 1918): `10.0.0.0/8`, `172.16.0.0/12`, and `192.168.0.0/16`. Anything inside those ranges is meaningful only on the local network it lives on. Your laptop's `192.168.1.42` is a different machine from someone else's `192.168.1.42` on their network; the addresses are scoped to the LAN.

A fourth range, `100.64.0.0/10`, is **CGNAT** — "carrier-grade NAT" space, used by ISPs to put many customers behind shared public IPs. Same idea, different layer: not routable on the public internet.

Public IPs are the opposite: globally unique, addressable from anywhere.

**In this homelab:** natto has three IPs that matter. Its LAN IP `192.168.1.50` (private, reachable only inside the house). Its Tailscale IP `100.122.71.33` (CGNAT-shaped, reachable only inside the tailnet — more on that later). And no public IP of its own at all, because the ISP is GFiber CGNAT.

## NAT and why "just expose a port" doesn't work

Your laptop with its `192.168.1.42` address reaches Google fine. Google can't reach your laptop unsolicited. The asymmetry is **NAT** — network address translation — done by your home router. When your laptop opens a connection outbound, the router rewrites the source IP from `192.168.1.42` to its own public IP and remembers the mapping; replies come back to the router and get translated back. But there's no entry in that table for inbound traffic that didn't start as a reply to something you sent.

The traditional workaround is **port forwarding**: you tell the router "TCP port 443 on the public side maps to `192.168.1.50:443` inside," which adds a permanent entry. That's how people used to put a web server in their basement.

**In this homelab:** we don't port-forward at all, which sets up the next section.

## CGNAT: when port-forwarding stops being possible

CGNAT puts your router behind *another* NAT operated by the ISP. Now there are two layers of translation between your machine and the public internet, and you don't control the outer one. Even if you configure port-forwarding on your own router, the public IP it's getting from the ISP isn't actually yours — it's shared with other customers, and there's no way to tell the ISP "send TCP/443 traffic for this shared address to my line."

GFiber is CGNAT. This means accepting an inbound connection from the public internet to natto is not a thing that can happen at the IP layer, period. No router setting will fix it. Two routes around the impasse are realistic: (a) put services on something with a real public IP — a cloud VM, a friend's server — or (b) originate an outbound connection to a service that *does* have a public IP and have it tunnel traffic back. We do (b), for one service. The Jellyfin README documents the dead-end attempts at (a) — reserved ports, phantom-device routing, DMZ-all — none worked, and they're not worth retrying.

## TLS and certificates

HTTPS does two things: it encrypts the connection, and it authenticates the server. Authentication is the interesting part. When your browser connects to `music.nthncrtr.com`, the server presents a certificate signed by a Certificate Authority the browser already trusts; that certificate says "the holder of this key is the legitimate operator of `music.nthncrtr.com`." Browsers will not accept a self-signed certificate, and they especially will not accept one whose name doesn't match.

To get a free, browser-trusted cert from Let's Encrypt (or a similar ACME-protocol CA), you have to prove you control the name. There are two main proofs.

**HTTP-01** asks: serve a specific token on `http://<name>/.well-known/acme-challenge/<...>`. Easy, but requires an inbound port 80 listener that the CA can reach. We don't have one.

**DNS-01** asks: put a specific TXT record into the DNS zone for `<name>`. The CA queries DNS and sees the token. This works for us because we can write to Cloudflare DNS via API, and crucially it works even when the host has no inbound internet reachability at all.

**In this homelab:** Caddy is configured with `acme_dns cloudflare {env.CF_API_TOKEN}` (see `services/caddy/Caddyfile` line 3). The Cloudflare API token lives in `/etc/caddy/caddy.env`, loaded by the systemd unit, never committed. Caddy uses it to write challenge records, satisfies DNS-01 for every site listed in the Caddyfile, and gets real certs that work everywhere — including for names that aren't actually reachable from the public internet, because the CA only needed to see a DNS record, not connect inbound.

## Reverse proxy

A reverse proxy is one process that accepts inbound connections and forwards them to backend services based on some routing rule — usually the hostname in the HTTP request. From the client's point of view it looks like a single web server hosting many sites; behind it, each "site" is actually a separate process listening on its own local port.

This solves several problems at once. You get one TLS termination point (so only one process needs the certificate-renewal logic). You get one place to add cross-cutting concerns like authentication. And your backends don't need to know anything about TLS, certificates, or which hostname their traffic arrived on.

**In this homelab:** Caddy is the reverse proxy. The Caddyfile reads almost like a routing table:

```caddy
music.nthncrtr.com {
    reverse_proxy 127.0.0.1:4533
}
torrent.nthncrtr.com {
    import authelia
    reverse_proxy 127.0.0.1:8080
}
```

Each block: incoming hostname on the left, where to forward on the right. Caddy listens on `:443`, terminates TLS, and connects to the local port. A loud consequence: Caddy addresses backends by IP (`127.0.0.1`), never by tailnet name. Caddy is a Go program with a pure-Go DNS resolver that reads `/etc/resolv.conf` directly and bypasses systemd-resolved, and systemd-resolved is where the Tailscale split-DNS lives. So tailnet name lookups silently fail from inside Caddy even though they work for `curl` and `apt`. The one remote upstream — `starmaya.nthncrtr.com` → `100.65.46.92:8080` on `kvass` — uses kvass's stable Tailscale IP for exactly this reason.

The final block, `:443 { abort }`, is a catch-all that refuses any unrecognized hostname. Explicit host blocks are more specific and win, so the named sites still work; everything else gets dropped.

## Tailscale and overlay networks

Tailscale builds an **overlay network**: a virtual network laid on top of whatever physical networks the participating devices actually live on. The data plane is WireGuard. Each device gets an IP in the `100.x` CGNAT range that's only meaningful inside your tailnet — your "tailnet IP." Devices can reach each other by tailnet IP regardless of which Wi-Fi, LTE, or wired network they're on, with NAT traversal handled by Tailscale's coordination server.

The mental model is "a private network that follows your devices." It looks like CGNAT, but the public internet has no route to those addresses; only other members of your tailnet do.

**In this homelab:** natto, workhorse, and kvass are all on the same tailnet (`tailaf7ea6.ts.net`). natto's tailnet IP is `100.122.71.33`. Every `*.nthncrtr.com` A record points at that address. From a programmer's perspective this is elegant: there's no separate ACL system, no firewall rule list, no per-service "is this user allowed in?" — there is simply no public IP route to `100.122.71.33`, so a non-member of the tailnet *cannot reach the service at all*. They can resolve the name fine, get back `100.122.71.33`, and then their packets go nowhere. The name being public is harmless because the destination is private. (One exception: `play.nthncrtr.com`, the Jellyfin name, also has a Cloudflare-proxied path — see Cloudflare Tunnel below.)

## Cloudflare Tunnel: when you can only originate outbound

Now combine the constraints. natto needs to host one service that's reachable from the public internet (Jellyfin, for trusted users on devices that may not run Tailscale). Port-forwarding is impossible because GFiber is CGNAT. The classic solution stack doesn't apply.

A **Cloudflare Tunnel** flips the direction. A small daemon (`cloudflared`) on natto opens a persistent **outbound** connection to Cloudflare's edge. Cloudflare's edge, which does have a real public IP and is happy to terminate TLS on `play.nthncrtr.com`, accepts incoming requests for that name and hands them back through the existing outbound connection to natto. From natto's network's perspective there is no inbound connection at all — just a long-lived outbound HTTPS-ish session.

This is the inverse of port-forwarding. Instead of "open a hole in my firewall to let the world in," it's "I'll dial out to a known endpoint that will relay traffic to me."

**In this homelab:** `services/cloudflared/config.yml` defines exactly one ingress mapping:

```yaml
ingress:
  - hostname: play.nthncrtr.com
    service: http://localhost:8096
  - service: http_status:404
```

That's it. Nothing else on natto is reachable via the tunnel — the fallback `http_status: 404` is the scoping guarantee. The tunnel does **not** carry any other hostname. Adding more services here would re-expose the rest of the homelab to the public internet, which is exactly what the tailnet-only design avoids; the architecture only works because this list stays at one entry.

## Split-horizon DNS

The same name can resolve to different addresses depending on *which* DNS server you ask. This is "split-horizon" or "split-view" DNS. It's useful when a name has a fast local path and a slower remote path, and you want clients to pick the local path when they're close enough to use it.

**In this homelab:** there are two names with local overrides — `music.nthncrtr.com` and `play.nthncrtr.com` — both configured in Pi-hole to resolve to natto's **LAN** IP `192.168.1.50` for any client that uses Pi-hole as its resolver (i.e. every device inside the house). For everything else, Pi-hole forwards the query upstream and the client gets natto's **Tailscale** IP `100.122.71.33`.

Why these two names. They're the heavy media paths: Navidrome streaming music to phones and laptops in the house, Jellyfin streaming video to the TV. Skipping the Tailscale hop on the LAN cuts latency and lets the bytes flow over the local switch instead of through the userspace WireGuard implementation on natto. For everyone else outside the house (or for the admin tier, where latency doesn't matter), the tailnet path is the right one.

The other half of split-horizon: clients outside the LAN — say, a phone on cell data — get the normal answer from Cloudflare DNS, which is the Tailscale IP. They reach the service over Tailscale (or, for Jellyfin specifically, over the Cloudflare Tunnel via `play.nthncrtr.com`'s Cloudflare-proxied "orange-cloud" record). Same name; different answer; different path; whichever is right for where the client is.

## Forward-auth and SSO

A reverse proxy can do more than route. It can also gate. **`forward_auth`** is a pattern where, before serving a request, the proxy makes an internal HTTP call to an authentication service, asking "is this request authenticated?" If the auth service says yes (200), the proxy continues to the backend. If it says no (401), the proxy redirects the user to a login page. After login, the auth service sets a cookie scoped to the parent domain, so subsequent requests for any gated service are short-circuited.

The clean part: each backend service stays oblivious. It doesn't know about the auth system, doesn't validate cookies, doesn't render a login page. The reverse proxy intercepts and decides.

**In this homelab:** Authelia is the auth service. The Caddyfile defines a reusable snippet:

```caddy
(authelia) {
    forward_auth 127.0.0.1:9091 {
        uri /api/authz/forward-auth
        copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
    }
}
```

Any vhost that includes `import authelia` is gated; the *arrs, qBittorrent, and Homepage all do. Jellyfin, Immich, Nextcloud, and Navidrome don't — and that's deliberate. Their native mobile clients can't navigate `forward_auth` redirects (an HTML login portal in the middle of an API conversation breaks them), so those services use their own per-app auth instead.

**The coupling with port binding.** Each gated service publishes its host port on `127.0.0.1` only, not `0.0.0.0`. Look at `services/sonarr/docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:8989:8989"
```

This is load-bearing. The *arrs are configured with `<AuthenticationMethod>External</AuthenticationMethod>`, which means they render no login page and trust whoever connects to be authenticated. If the host port were `0.0.0.0:8989`, anyone on the LAN could connect directly to `192.168.1.50:8989` and get in with no auth at all. Binding to `127.0.0.1` makes the *only* possible path "via Caddy on the same host, which calls Authelia first." The bindings and the in-app auth setting are two halves of a single design — flip either one without the other and you've made an unauthenticated open door. qBittorrent has the same problem solved a slightly different way (an in-app subnet whitelist for the docker bridge, since qBit has no "external auth" mode).

## Docker networking

Containers run in their own network namespaces. By default, Docker creates a "bridge" network and puts containers on it; each gets a private IP in some subnet like `172.18.0.x`. Containers on the same bridge can reach each other by container name (Docker runs a DNS resolver that knows the mapping). Containers on different bridges can't, unless one is explicitly attached to the other's network.

A **host port publish** like `127.0.0.1:8989:8989` does a separate thing: it tells the host kernel to DNAT inbound traffic on the host's `127.0.0.1:8989` to the container's `:8989`. The host port and the container-to-container traffic are different code paths in the kernel.

**The `host.docker.internal` trap.** Docker provides a special name `host.docker.internal` that resolves, from inside a container, to the host's bridge-gateway IP — *not* `127.0.0.1`. If you publish a port on `0.0.0.0` (or any IP the bridge gateway can reach), `host.docker.internal:<port>` works. If you publish on `127.0.0.1` only, the host kernel rejects the connection from the bridge gateway because that source isn't loopback. You get connection refused, and the error message doesn't make the cause obvious.

**In this homelab:** when host ports got rebound to `127.0.0.1` on 2026-05-18, Prowlarr ↔ *arrs and *arrs → qBittorrent inter-container calls broke the next day, because they were routed via `host.docker.internal`. The fix wasn't to re-expose ports — that would have undone the Authelia coupling above. Instead, two shared docker networks now carry the inter-container traffic: **arrnet** (172.29.0.0/16) for Prowlarr ↔ Sonarr ↔ Radarr, and **qbittorrent_default** (172.23.0.0/16, created by the qBittorrent stack and attached to by Sonarr/Radarr as `external: true`) for the *arrs → `gluetun:8080`. Container-name addressing on a shared network: `http://sonarr:8989`, `http://gluetun:8080`. No host port involved, no `host.docker.internal`, and the LAN exposure stays closed.

## VPN-as-egress: Proton VPN via gluetun

A VPN does one of two things depending on direction. As an **overlay mesh** (Tailscale, Nebula, ZeroTier), it builds a private network for peer-to-peer reach. As an **egress VPN** (Proton, Mullvad, NordVPN), it routes your outbound traffic out through someone else's exit IP, so the public sees their address instead of yours. They're conceptually different even though both use WireGuard or OpenVPN under the hood.

**In this homelab:** qBittorrent's outbound traffic must egress through Proton VPN, not natto's normal route. The standard pattern is the **`gluetun` sidecar**. `gluetun` is a container that holds an active WireGuard tunnel to a Proton exit. `qbittorrent` runs with `network_mode: "service:gluetun"` (see `services/qbittorrent/docker-compose.yml` lines 61–64), which means qBit doesn't have its own network namespace at all — it shares gluetun's. Every packet qBit sends goes out gluetun's tun0 interface, i.e. through the Proton tunnel. If gluetun dies, qBit has no network. That's the kill switch — there is no path for qBit's traffic to "fall back" to natto's default route, because qBit literally cannot see natto's default route.

A wrinkle: a netns-shared container can't publish ports of its own (it doesn't own a network namespace). So the qBittorrent WebUI on `:8080` is published by `gluetun`, not by `qbittorrent`. That's why the Caddyfile reverse-proxies `torrent.nthncrtr.com` to `127.0.0.1:8080` and the `127.0.0.1:8080:8080` line lives in gluetun's compose service.

This pattern is general: any service whose outbound traffic should be tunneled (torrent clients, scrapers, geo-restricted automation) can be slotted in by adding a `network_mode: "service:gluetun"` and not publishing its own ports.

## Where to go next

For the full operational picture — the safety rules, the deployment workflow, every gotcha that earned a paragraph — read [`CLAUDE.md`](../CLAUDE.md). It's the most authoritative document in the repo and assumes you've absorbed everything in this primer.

For host-level rebuild procedures, see the [`runbooks/`](../runbooks/) directory: [`migrate-natto.md`](../runbooks/migrate-natto.md) is the cold-rebuild story, and each runbook has a Gaps section that's basically a list of "things that surprised us."

For per-service specifics — what each `*.nthncrtr.com` actually points at, what auth it uses, what data path it touches — the README inside each `services/<svc>/` directory is the source of truth.
