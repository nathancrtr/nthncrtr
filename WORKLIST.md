Session 1 (Phase 1, step 2 — Pi-hole compose file):  **DONE — commit acebc60**

Goal: produce services/pihole/docker-compose.yml that, when brought up, reproduces the current Pi-hole container.
Agent SSHes to natto, runs docker inspect pihole, transcribes into a compose file, commits.
Success criteria: docker compose config parses; the diff between docker inspect of the old container and the new one shows no meaningful drift in env vars, mounts, ports, network mode.
The agent does not stop the running container in this session. That's the next session.

Outcome: docker inspect revealed the running container was already compose-managed by /home/nthncrtr/docker/pihole-compose.yml (com.docker.compose.config_files label). That file was committed verbatim to services/pihole/docker-compose.yml. Cross-checked: image, network_mode (bridge), port bindings (53/tcp, 53/udp, 8053→80), TZ env, bind mounts, restart policy all match docker inspect. DNSMASQ_USER, FTL_CMD, and exposed ports 67/123/443 are image defaults, not user config. `docker compose config` parses cleanly.

Session 2 (Phase 1, step 3 — cutover):  **DONE (no-op) — 2026-05-09**

Goal: cut over Pi-hole to the compose-managed version with verified DNS continuity.
Preconditions: session 1 committed; dig @natto.local example.com works.
Steps: stop old container, docker compose up -d, wait, re-test DNS, commit.
Success criteria: DNS resolves; admin UI loads; query log shows new queries arriving.
Rollback: docker compose down && docker start pihole_old. If the original container is gone, restore from /srv/pihole/etc backup.

Outcome: no cutover needed. The running container was already created from the compose file (verified via `docker compose -f pihole-compose.yml ps` and `up -d --dry-run`, which reported only "Container pihole Running" — no recreate). DNS baseline confirmed: `dig @natto.local +short example.com` returned answers (104.20.23.154, 172.66.147.243). User chose "mark complete, no restart" rather than force-recreate, to avoid the ~30s DNS outage when there was no functional change to verify. If a future change to services/pihole/docker-compose.yml needs to be deployed, the cutover steps above still apply.
