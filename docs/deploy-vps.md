# Deploying to a small VPS

This guide puts the assistant on a rented Linux server with HTTPS, in about 30 minutes. Any provider works. For
patient data, choose one with data centres in the EU and sign its data processing agreement (DPA).

> **Before real patients use it:** this is not certified medical software, and you are responsible for GDPR
> compliance (see the warning in the [README](../README.md)).

## 1. The server

- **Size:** 2 vCPU, 4 GB RAM, 20 GB disk. The local embedding model and Chroma need about 1.5 GB of RAM. A local
  LLM needs much more (see [ollama.md](ollama.md)).
- **System:** Ubuntu 24.04 LTS (other Linux distributions work similarly).
- **Domain:** a DNS **A record** such as `assistant.your-clinic.example` pointing at the server's IP address.

Log in over SSH as a user with sudo, then:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git ufw unattended-upgrades
# Docker (official convenience script; or follow docs.docker.com/engine/install/ubuntu)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# Firewall: only SSH and the web
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
```

## 2. The assistant

```bash
git clone https://github.com/satishkumarnirujogi/Business_communication_tool.git && cd Business_communication_tool
cp .env.example .env
nano .env
```

Set at least:

```env
LLM_PROVIDER=...            # see the README's model table
LLM_MODEL=...
LLM_API_KEY=...
ADMIN_TOKEN=<long random string: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
PUBLIC_BASE_URL=https://assistant.your-clinic.example
DEMO_MODE=false             # true only for a test installation
PORT=127.0.0.1:8080         # only reachable through the HTTPS proxy below
```

Edit `config/clinic.yaml` (name, phone, address, timezone, languages) and `config/rules.yaml` (opening hours,
booking rules), or do it later in the admin dashboard. Then:

```bash
docker compose up -d
docker compose ps                # backend shows "healthy" after about a minute
docker compose logs -f backend   # Ctrl+C to stop following
```

The first start downloads the embedding model (about 220 MB) into the data volume.

## 3. HTTPS with Caddy

[Caddy](https://caddyserver.com) gets and renews certificates automatically.

```bash
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
assistant.your-clinic.example {
    encode gzip
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1        # stream chat replies immediately
    }
}
EOF
sudo systemctl reload caddy
```

Open `https://assistant.your-clinic.example/admin` **right away** and complete the setup wizard. Until the first
account exists, anyone who opens that page could create it. To set it up before the site is public, use an SSH
tunnel instead (`ssh -L 8080:127.0.0.1:8080 you@server`, then open http://localhost:8080/admin) and reload
Caddy afterwards.

Add the chat to the clinic website with the snippet from the README, using your domain in `widget.js`.

## 4. Backups

Everything worth keeping lives in three places: `.env`, `config/`, and the `clinic-data` volume (database,
document text, logo). The Chroma volume doesn't need a backup: rebuild it from the stored document text with
`curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://assistant.your-clinic.example/api/admin/knowledge/reindex`.

```bash
mkdir -p ~/backups
# a consistent copy of the SQLite database while the assistant keeps running
docker compose exec -T backend python -c "import sqlite3; s=sqlite3.connect('/app/data/clinic.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close()"
docker compose cp backend:/app/data/backup.db ~/backups/clinic-$(date +%F).db
tar czf ~/backups/config-$(date +%F).tgz .env config
```

Put this in a script, run it nightly with `crontab -e` (e.g. `0 3 * * * /home/you/backup.sh`), and copy
`~/backups` off the server, for example with your provider's backup service or `rclone`. Backups contain patient
data: encrypt them and delete old ones on a schedule that matches your retention policy. **Test a restore**
once: stop the stack, copy the backup to `/app/data/clinic.db` with `docker compose cp`, start again.

With `DATA_SOURCE=postgres`, back up that database with your provider's tools or `pg_dump` instead.

## 5. Updates

```bash
cd Business_communication_tool
git pull
docker compose up -d --build
docker image prune -f
```

Read the release notes first. Back up before updating. Database tables are created automatically; if a release
changes existing tables, its notes say what to do.

## 6. Running it well

- **Logs:** `docker compose logs --since 1h backend`. Emails, phone numbers and birth dates are masked in the log.
- **Health:** `curl -s https://assistant.your-clinic.example/api/health` returns `{"ok": true, ...}`. Point an
  uptime monitor at it.
- **WhatsApp:** with `PUBLIC_BASE_URL` set, follow [whatsapp.md](whatsapp.md). The webhook URL is
  `https://assistant.your-clinic.example/webhooks/whatsapp/...`.
- **Security:** keep `unattended-upgrades` on, use SSH keys (no password login), give staff their own accounts
  (Admin → Settings → Users), and keep `ADMIN_TOKEN` for emergencies only.
- **One process:** the backend must run as a single worker (it does by default). Don't scale it with
  `--scale backend=2`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `backend` never becomes healthy | `docker compose logs backend` shows a readable configuration message; fix `.env` / `config/` and run `docker compose up -d` |
| Admin UI can't save settings ("permission denied") | The container runs as uid 1000. Run `sudo chown 1000:1000 .env config config/*` |
| Chat replies arrive all at once | Make sure the Caddy `flush_interval -1` line is there |
| Caddy can't get a certificate | The DNS record must point at this server, and ports 80 and 443 must be open |
