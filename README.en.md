# Kitsunebi Unified Platform

> **A self-hosted platform that folds four things — packet capture, packet rewriting,
> proxy licensing and redemption codes — behind one entry point.**
> Charles Proxy for capture, a CCProxy adapter layer for rewriting, the Yihua
> redemption-code system, and a FastAPI + Vue 3 console — plus an **MCP endpoint**
> so AI agents can read, edit, replay and export traffic directly.

[中文介绍 → `README.md`](README.md)

---

## 1. What this is

You stand up a complete `device → proxy → target service` chain, and make both
**observation** and **intervention** controllable:

- **See it** — every TCP / TLS / HTTP / UDP flow through the proxy is recorded into a
  ring buffer: filterable, full hex available, with layered protocol structure.
- **Change it** — hold a single packet and forward / drop / rewrite it by hand, or attach
  automatic rewrite rules that replace bytes in bulk.
- **Send it** — replay a captured packet, or build a brand-new one from scratch and send it.
- **License it** — issue, renew and bind redemption codes to servers; the data layer was
  rewritten into the platform, so PHP is no longer required.
- **Expose it** — `/mcp` publishes all of the above as 17 MCP tools, so an agent operates
  **the exact same code path** a human does. Not a parallel implementation.

In one line: **four systems, one entry point, one path.**

---

## 2. The four components

| Component | Role | Ports | systemd unit / container |
|---|---|---|---|
| **Charles Proxy (headless)** | Capture, root CA, upstream proxy. 4.5.6, self-contained with its own JRE | `18888` HTTP · `18889` SOCKS5 | `charles-headless.service` |
| **CCProxy adapter layer** | The capture point on the forwarding path + the WPE engine (record / rewrite / hold / replay / export) | `8888` HTTP · `8889` SOCKS5 · `8893` admin API · `40000-40063` UDP relay | `ccpx.service` + `ccpx-capture.service` |
| **Yihua redemption codes** | Codes / applications / servers / accounts (data layer rewritten into the platform) | legacy `127.0.0.1:8081` (PHP, optional) | docker `yihua-app` + `yihua-db` |
| **Unified platform** (this repo) | Console + API + MCP, folding the other three behind one entry | `8890` (incl. `/mcp`) | `rewrite.service` |

> **Where packets are captured:** on the **adapter's forwarding path** (TCP CONNECT
> tunnels, plain HTTP, UDP SOCKS5 relay). **No target process is injected** — no root
> needed to read another process's memory, and no cooperation required from the observed app.

---

## 3. Architecture

```
                    ┌─────────────────────────────────────────────┐
   device / phone ─▶│  CCProxy adapter  8888 HTTP / 8889 SOCKS5   │
   (Kitsunebi /     │  ├─ capture point → ring buffer (4000)      │
    browser /       │  ├─ hold / auto-rewrite / replay / send     │
    script)         │  └─ UDP relay 40000-40063 (orphan self-heal)│
                    └───────────────┬─────────────────────────────┘
                                    │ chained upstream
                                    ▼
                    ┌─────────────────────────────────────────────┐
                    │  Charles Proxy   18888 / 18889              │
                    │  (root CA · upstream proxy · sessions)      │
                    └───────────────┬─────────────────────────────┘
                                    ▼
                              target service

                    ┌─────────────────────────────────────────────┐
   browser / agent ─▶│  Unified platform  8890                    │
                    │  FastAPI  /api/v1/{auth,overview,charles,   │
                    │           ccpx,wpe,kami}                    │
                    │  MCP      /mcp  (17 tools, JSON-RPC 2.0)    │
                    │  device   /ok /socks /ruleset.conf /ca.crt  │
                    │           (no auth, registered before SPA)  │
                    │  Vue 3 SPA (served same-origin)             │
                    └──────────┬──────────────────┬───────────────┘
                               │ admin API        │ data layer
                               ▼                  ▼
                   adapter 8893 / Charles webif   SQLite (codes / accounts / sessions)
```

Three invariants enforced in code, not just documented:

1. **Read endpoints never mutate state.** Listing and status checks never change anything.
2. **Destructive operations have a safety net.** The buffer is flushed to disk *before*
   being cleared; if the safety net is not in place, the operation aborts.
3. **No varnishing.** Upstream non-200, missing parameters, truncated results — all
   reported honestly. **Never a "looks fine" empty result.**

---

## 4. The unified platform

### 4.1 Backend (FastAPI)

- 8 business routers, **88 routes**, plus `/healthz`, `/_frontend` and the SPA fallback:

  | Prefix | Routes | Purpose |
  |---|---|---|
  | `/api/v1/auth` | 5 | login / logout / current user / change password |
  | `/api/v1/overview` | 1 | aggregated health of all four systems |
  | `/api/v1/charles` | 14 | Charles status, config read/write, certificates, license |
  | `/api/v1/ccpx` | 8 | adapter status, rules, logs, restart |
  | `/api/v1/wpe` | 16 | packet list / detail / rewrite / hold / replay / export / import |
  | `/api/v1/kami` | 25 | codes / applications / servers / accounts / redeem |
  | `/mcp` | 3 | MCP endpoint (POST / GET-SSE / DELETE) |
  | `device` (no prefix) | 16 | unauthenticated device resources, see §4.3 |

- **Clean layering:** `config` → `security` (password/JWT) → `db` / `models` / `schemas`
  → `upstream` (clients for the adapter and Charles) → `deps` (auth funnel) → `routers`.
- **Semantics are never reimplemented.** The platform does not guess, patch or beautify
  upstream results; every WPE operation is translated into one adapter request.

### 4.2 Frontend (Vue 3 + Vite)

- Only `vue` and `vue-router` as dependencies; state is hand-rolled with `reactive`
  (no Pinia).
- 6 views: `Login` / `Overview` / `Wpe` / `Ccpx` / `Charles` / `Kami`.
- The WPE view is split into 7 components: `PacketTable`, `PacketDetail`, `HexDump`,
  `HoldPanel`, `RewritePanel`, `ExportPanel`, `SendPanel`.
- Branding lives in a single constant (`src/brand.js`) — renaming touches one place.

### 4.3 Device-side endpoints (no authentication)

Resources clients need (Kitsunebi on a phone, scripts, browsers) are **deliberately
unauthenticated**, because clients have no session:

```
GET /ok                       health probe
GET /socks                    SOCKS5 config snippet
GET /ruleset  /ruleset.conf   routing ruleset
GET /ca.crt /ca.cer /ca.pem   root certificate (PEM / DER)
GET /charles-ca.{crt,cer,pem} Charles root certificate
GET /ca.mobileconfig          iOS configuration profile
GET /ca.qr.svg                QR code for certificate download
GET /portal                   user portal
```

Two traps, already handled and commented in the source:

- These routes **must be registered before the SPA catch-all** — otherwise they get
  swallowed into `index.html` and the client receives HTML where it expected a
  certificate.
- **Client IP is read from the socket peer only, never from `X-Forwarded-For`** —
  otherwise anyone can spoof their origin.

---

## 5. WPE packet editor

### 5.1 Capabilities

| Capability | Notes |
|---|---|
| List | direction / protocol / target / HEX substring / app layer / length range / hold-queue only; `since` for newer, `before` for older |
| Detail | metadata + **full hex** + ascii + truncation flag + manual/rule rewrite markers |
| Structure | field-level `off` / `len` for HTTP / TLS / DNS / SOCKS5 / MQTT; the structure panel highlights hex from these offsets |
| Hold | freeze matching packets, then `forward` / `drop` / `modify` |
| Auto-rewrite | full-table replacement; `find_replace` for byte substitution or `set_hex` for whole-packet replacement; `rejected` lists every refused rule and why |
| Replay / send | replay a captured packet (new payload / target / protocol / repeat count) or build a TCP / UDP packet from scratch |
| Export / import | paginated in-memory export, or on-disk `json` / `txt` without response-size limits; imported packets are always tagged `imported` |
| Clear | buffer only — **does not** touch rewrite rules or hold settings |

### 5.2 Constraints worth knowing

- **Hex in list responses is a 512-character preview** (`hex_full_len` is a character
  count, not a byte count). Fetch full hex from the detail endpoint.
- **List page size caps at 2000** (`min(limit, 2000)`). To page further back, pass the
  returned `oldest_id` as `before`; `has_more=false` means you reached the oldest.
- **The ring buffer holds 4000 packets**; the oldest are overwritten.
- **The only trustworthy source for the hold queue is the "hold only" filter** — the
  adapter does not stamp a `hold` flag on packets.
- **Unconditional rewrite rules are rejected.** A rule needs at least one condition
  (`dir` / `proto` / `target` / `hex` / `app` / `min_len` / `max_len`) — an
  unconditional rule would rewrite every single packet.

---

## 6. MCP endpoint (`/mcp`)

All WPE capabilities are exposed as **17 MCP tools** over JSON-RPC 2.0;
`protocolVersion` accepts `2025-06-18 / 2025-03-26 / 2024-11-05`.

| Tool | Purpose |
|---|---|
| `wpe_stat` | overview: captured / buffer usage / hold state / rewrite hits / UDP relay ledger |
| `wpe_list` | filtered listing with `since` / `before` cursors |
| `wpe_get` | one packet: full hex + layered structure |
| `wpe_struct` | layered structure only |
| `wpe_rewrite_get` / `wpe_rewrite_set` | read / write auto-rewrite rules |
| `wpe_hold_get` / `wpe_hold_set` | read / write hold state and rule |
| `wpe_release` | forward / drop / modify a held packet |
| `wpe_replay` | replay a captured packet |
| `wpe_send` | build and send from scratch |
| `wpe_export` | in-memory export with a continuation cursor |
| `wpe_export_save` / `wpe_export_list` / `wpe_export_download` | on-disk export / list / fetch |
| `wpe_import` / `wpe_clear` | import / clear buffer |

Three design decisions:

- **`/mcp` itself is unauthenticated** (MCP clients cannot log in first), but the
  **large-file download URL it hands out carries a one-time token** — signed with the
  same key as login, bound to the file name, a `scope`, and a 30-minute TTL. Change the
  file name and the token stops working.
- Tool failures surface as `result.isError = true`, **not as a JSON-RPC error** — the
  latter is a protocol-level failure and clients treat it as a broken connection.
- Results beyond the per-call limit are **honestly marked `truncated`** with a concrete
  next step (export to disk, or page with `before`), instead of being silently cut.

---

## 7. One-shot deployment

```bash
# Full chain: all four systems in one pass (license + root CA + adapter + yihua + platform)
sudo bash deploy/fullchain.sh --public-host <your-public-ip> \
     --license-name <name> --license-key <key>

# Dry run — plan only, touches nothing
sudo bash deploy/fullchain.sh --plan
```

- `deploy/fullchain.sh` — full-chain installer; `--plan` / `--skip` / `--only` for staged runs.
- `deploy/deploy.sh` — platform-only update (backup → upload → three-way md5 → remote
  syntax check → restart → smoke test).
- `deploy/switchover.sh` / `rollback.sh` — port switchover and rollback. **The order is
  not negotiable:** move the new backend to the target port and stop it → stop the old
  service → start the new backend.
- `deploy/oneclick.sh` / `oneclick_deploy.py` — interactive one-click install.
- All environment variables live in `rewrite.env` (template: `deploy/rewrite.env.example`).
  **No IP, port or credential is hard-coded in the code.**

### Deployment discipline (learned the hard way)

- Back up first → verify a **three-way md5** after upload → remote `py_compile` /
  `node --check` → restart → smoke test.
- Charles config changes require **stop → edit → start**: on exit Charles writes its
  in-memory configuration back to disk, so editing the file while running is a no-op.
- File ownership must be correct (adapter `ccpx:ccpx`, platform `ubuntu:ubuntu`).
  `cp` as root changes ownership and the service can no longer read its files.
- Directory syncs must exclude runtime data directories and virtualenvs.

---

## 8. Regression suite

Seven suites, all executed against a real server: **403 PASS / 0 FAIL**.

| Suite | Coverage | Result |
|---|---|---|
| `_test_wpe_struct.py` | adapter structure parsing / hex helpers | 93 PASS / 0 FAIL |
| `_test_wpe_udp_adopt.py` | UDP orphan-adoption association lifecycle | 25 PASS / 0 FAIL |
| `_test_wpe_export_live.py` | export end-to-end (memory / disk / full hex) | 48 PASS / 0 FAIL |
| `_test_wpe_udp_live.py` | UDP relay on the live path | 12 PASS / 0 FAIL |
| `_test_wpe_console_live.py` | platform export and download endpoints | 30 PASS / 0 FAIL |
| `_test_mcp_live.py` | all 17 MCP tools online | 65 PASS / 0 FAIL |
| `_test_mcp_wpe.py` | MCP offline self-check (paths must exist in the real route table) | 130 PASS / 0 FAIL |
| `_smoke.py` | backend smoke | 85 PASS / 0 FAIL |

Lessons deliberately baked into the test scripts (each one cost real debugging time):

- **Assertions must be upstream-aware.** Hard-coding "upstream is dead / alive" makes the
  other branch a guaranteed false red.
- **A missing assertion is a source of false green.** A `None` silently passed down only
  surfaces at the far end; `all()` over an empty set is trivially true (a no-op test);
  **a green false positive is worse than a red.**
- **Extract the route table from source, never hand-copy constants.** Hand-copied
  constants drift, tests stay green, production returns 404.
- **Read response headers case-insensitively.** Servers emit their original casing;
  a case-sensitive lookup "finds nothing" and gets misread as a missing feature.

---

## 9. Repository layout

```
.
├── README.md / README.en.md      Chinese / English introduction (this file)
├── SECURITY.md                   How sensitive values are handled (read this first)
├── adapter/                      CCProxy adapter layer
│   ├── ccproxy_adapter.py        capture point + WPE engine (single file, ~191 KB)
│   ├── ccpx.service / config.example.json
│   └── e2e_api.sh / e2e_proxy.sh / mktoken.sh / setup.sh
├── platform/
│   ├── backend/app/              FastAPI: config/security/db/models/schemas/
│   │                             upstream/deps/routers
│   ├── backend/_smoke.py         backend smoke test (85 checks)
│   ├── frontend/                 Vue 3 + Vite (src / dist / build config)
│   └── deploy/                   deploy scripts + systemd units + env template
│       └── vendor/               third-party artifacts (**large binaries not committed**,
│                                 see vendor/README.md)
├── docs/                         architecture, deployment reports, root-cause analyses
├── screenshots/                  UI screenshots
└── tools/
    ├── _srv.py                   minimal SSH/SFTP channel (run / sudo / put / get)
    ├── tests/                    the seven regression suites
    └── probes/                   reconciliation probes (pass-through, zero-loss loop, MCP surface)
```

---

## 10. Known limitations (stated plainly)

- **The positive path of `wpe_release` is not verified end-to-end.** It requires creating
  a genuine hold, which would stall a real user's connection for up to 30 seconds — so it
  is left for a maintenance window.
- **The iOS `.mobileconfig` is unsigned.** No Apple developer certificate is available;
  unsigned profiles require manual trust on iOS.
- **The legacy Yihua PHP backend (8081) is still up.** The data layer has been rewritten
  into the platform; PHP is an optional leftover and should be observed in parallel
  before being stopped.
- **Legacy password hashes:** the platform can verify old PBKDF2 records to allow a
  smooth migration, but passwords should be reset once migration completes.
- The adapter's TLS decryption toggle defaults to the conservative setting. Without the
  root CA installed, HTTPS reports certificate errors — that is expected, not a bug.

---

## 11. License and credits

- The **project's own code** (platform frontend/backend, adapter, deploy scripts, tests)
  is self-hosted project source.
- **Charles Proxy is commercial software.** This repository does **not** include its
  distribution; it only provides installation scripts and license injection. You must
  obtain your own license.
- Third-party components (FastAPI, Vue 3, Vite, MySQL, …) follow their respective licenses.
