# Wallet connectivity API

Two read-only routes that let a wallet find its public address and check whether a port it
listens on can be reached from the internet. They replace third-party "what is my IP" and
port-checker services. Source: `src/epic_status/reach.py`; nginx: `ops/nginx/`.

Both are `GET`, answer JSON with `cache-control: no-store` and
`access-control-allow-origin: *`, and are not in the OpenAPI schema.

## `GET /api/v1/ip`

The address the request came from, as this server sees it.

```json
{"ip": "198.51.100.7"}
```

`ip` is IPv4 or IPv6, whichever the client connected over (an IPv4-mapped IPv6 address is
given as IPv4). status.btlabs.uk has no AAAA record today, so it is IPv4 there.

## `GET /api/v1/reach?port=<n>`

Opens one TCP connection from this server to **the requester's own address** on `port`.
When it opens, sends one Epic wallet foreign API call on it (`POST /v2/foreign`,
JSON-RPC `check_version`, plain HTTP) and reads the reply.

```json
{"ip": "198.51.100.7", "port": 3415, "open": true, "foreign_api": true,
 "detail": "port open; Epic wallet foreign API answered (version 2)"}
```

| Field | Meaning |
|---|---|
| `ip` | The address that was checked: always the requester's own. |
| `port` | The port that was checked. |
| `open` | `true` if the TCP connection was accepted within 5 s. |
| `foreign_api` | `true` if an Epic wallet foreign API answered `check_version` on it (plain HTTP only; a TLS listener shows `open: true, foreign_api: false`). |
| `detail` | One English sentence for people, e.g. `connection refused (nothing listens on this port)`, `no answer within 5 s (closed or filtered)`. Not for parsing. |

A closed or filtered port is still `200` with `"open": false`.

## Errors

Every error is `{"error": "<message>"}`.

| Status | `error` | When |
|---|---|---|
| 400 | `not available over Tor` | The request came through the status onion. Over Tor this server cannot know the caller's address, so neither route answers there. |
| 400 | `port must be an integer from 1024 to 65535` | `port` missing, not plain digits, or out of range. |
| 400 | `no public address was seen for this request` | The caller's address is not a public one (loopback, private, CGNAT, reserved). |
| 400 | `this server's own address is never checked` | The caller is this server. |
| 429 | `too many checks; try again in N s` | Over the per-address limit; `retry-after` header in seconds. |
| 429 | `busy; try again in a few seconds` | Eight checks already running. |
| 429 | (nginx, HTML body) | Over nginx's per-address rate in front of the application. |

## Limits and guarantees

- No target parameter exists: any `ip`, `host` or other query parameter is ignored. The
  address is nginx's `$remote_addr` (`X-Real-IP`, set by the vhost over anything the client
  sent); `X-Forwarded-For` is neither passed nor read.
- Ports 1024–65535 only.
- `/api/v1/reach`: 10 a minute per address in nginx (burst 5, 2 concurrent connections), and
  10 a minute and 60 an hour per address (IPv6 per /64) in the application; 8 at once in total.
- One TCP connect with a 5 s timeout; the foreign API reply is read within another 5 s and
  capped at 16 KiB. Nothing is retried.
- `/api/v1/ip` is on the ordinary JSON API limit (10 a second per address, burst 20).
- No request address is logged: both routes write to the IP-less API access log
  (`status_api_anon`), the application logs nothing per request, and the rate-limit keys are
  salted hashes held in memory only.
- Over the onion both routes are answered by nginx itself with the 400 above
  (`ops/nginx/status-onion.conf`), so a request over Tor never makes this server connect
  anywhere.
