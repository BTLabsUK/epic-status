# Epicmail fixtures

Recorded once each on 2026-11-15 01:08 UTC from the live relay at
`https://epicmail.btlabs.uk` (also serving `epicbox.btlabs.uk`), Epicmail 0.1.1:

| File | Request |
|---|---|
| `well-known.json` | `GET /.well-known/epicbox.json` |
| `status.json` | `GET /status.json` |
| `v1-info.json` | `GET /v1/info` |
| `ready.json` | `GET /ready` |
| `classic-any-path.html` | `GET /.well-known/epicbox.json` on `epicbox.epiccash.com`, a classic relay, which answers every path with its HTML status page |

Bodies are byte-for-byte as served. The tests never touch the network.
