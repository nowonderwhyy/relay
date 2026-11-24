# Profile: Reverse Proxy (Caddy / Nginx) & Tailscale

Relay blocks unknown `Host` headers (DNS-rebinding protection) and non-private source IPs by default. Use these settings when exposing Relay over Tailscale or behind a reverse proxy.

## 1. Tailscale (Direct Tailnet Access)

Tailscale addresses live in RFC 6598 CGNAT space (`100.64.0.0/10`) and MagicDNS names end in `.ts.net`. Update `config.json`:

```json
{
  "allow_cgnat": true,
  "allowed_hosts": ["my-server.tailnet-name.ts.net"]
}
```

Or allow your entire tailnet suffix: `"allowed_hosts": ["*.ts.net"]`.

## 2. Tailscale Serve (HTTPS on Tailnet)

Bind Relay to loopback (`"host": "127.0.0.1"`, `"port": 8080`) and trust the local proxy:

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "allow_cgnat": true,
  "trusted_proxies": ["127.0.0.1", "::1"],
  "allowed_hosts": ["my-server.tailnet-name.ts.net"]
}
```

Then run:

```bash
tailscale serve --bg http://127.0.0.1:8080
```

## 3. Caddy Reverse Proxy (LAN HTTPS)

In `config.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "trusted_proxies": ["127.0.0.1", "::1"],
  "allowed_hosts": ["relay.home.arpa"]
}
```

In `Caddyfile`:

```caddyfile
relay.home.arpa {
    tls internal
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
```

> `flush_interval -1` ensures streaming `.zip` archives begin downloading immediately without proxy buffering.

## 4. Nginx Reverse Proxy

In your `server` block:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_request_buffering off;
    client_max_body_size 12m;
    proxy_read_timeout 3600s;
}
```

> **Important**: If exposing Relay to clients outside your LAN/tailnet (`"allow_public_clients": true`), you **must** terminate TLS at the reverse proxy **and** set a strong household password via `relay passwd`.
