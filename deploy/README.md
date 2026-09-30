YarraTrak runs as one Uvicorn worker behind Nginx. Rate and connection limits
are in memory; multiple workers require a shared limiter before scaling out.

Use `nginx/ptv.conf.example` as a template. WebSocket locations must repeat the
forwarded headers because Nginx location-level headers replace inherited ones.
Forward only `$remote_addr`, not caller-provided `X-Forwarded-For` chains.
For a Cloudflare-proxied public host, install `nginx/cloudflare-real-ip.conf`
at `/etc/nginx/cloudflare-real-ip.conf`. Only Cloudflare's listed networks may
provide `CF-Connecting-IP`; refresh these ranges when Cloudflare changes them.
Remove that include when hosting without Cloudflare.

Install `systemd/limits.conf` as the service drop-in shown in its comment.
It bounds WebSocket frames and queued frames before application parsing, and
trusts forwarded headers only from the local proxy. Keep Uvicorn loopback-only.
Validate Nginx with `nginx -t` before reloading and run `systemctl daemon-reload`
before restarting the service after changing its drop-in.

The application validates WebSocket inputs, allows three connections per IP,
and limits data requests, queries, connection attempts and all inbound messages.
Quotas survive reconnects and client-ID changes. Departure/position caches and
client activity tables are bounded; full quota tables reject new keys until old
windows expire. These controls reduce abuse of the shared server and PTV quota;
they do not replace upstream protection against distributed denial of service.
