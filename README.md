# Kanidm Easy Deploy

Opinionated, wizard-driven [Kanidm](https://kanidm.com/) deployment for a single VPS: Docker Compose (Kanidm server, optional Caddy), `deploy.yaml` configuration, and generated secrets.

Kanidm is the **source of truth** for users, groups, authentication, and identity attributes. Other Easy Deploy kits (OpenCloud, Matrix, Stalwart) authenticate against this instance instead of keeping their own accounts.

## Requirements

- Linux host with Docker Engine and Docker Compose v2
- DNS `A`/`AAAA` for your identity domain (e.g. `idm.example.com`)
- [uv](https://docs.astral.sh/uv/) (installed automatically by `ensure-dependencies.sh`)

Kanidm must be reached over **HTTPS** (Caddy obtains certificates automatically).

### Proxy modes

- **`proxy.mode: standalone`** (default) — this repo runs `kanidm_caddy` on ports 80/443.
- **`proxy.mode: integrate`** — no local Caddy; emits a fragment for [easydeploy-engine](../easydeploy-engine/) (multi-service VPS). See [docs/integrating-engine.md](docs/integrating-engine.md).

## Quick start

```bash
git clone --recurse-submodules https://github.com/opencomp-eu/kanidm-easy-deploy.git
cd kanidm-easy-deploy
bash ensure-dependencies.sh
bash wizard.sh
```

Or manually:

```bash
cp deploy.yaml.example deploy.yaml
# edit deploy.yaml
bash apply.sh
```

## Configuration

- **`deploy.yaml`** — operator settings (domain, initial person, groups).
- **`.kanidm-easy-deploy/secrets.yaml`** — generated secrets (auto-created on first apply; do not commit).
- **`/var/lib/kanidm`** (default) — Kanidm database, TLS material, and `server.toml`.

Kanidm's TLS material in the data directory is a two-tier, locally-signed setup: `ca.pem` + `ca-key.pem` (a long-lived local CA) sign the server certificate (`key.pem`; `chain.pem` carries the leaf plus the CA). SANs cover your identity domain, the `kanidm` container name, and localhost. Strict clients trust the CA — the Admin UI mounts `ca.pem` via `KANIDM_TLS_CA_FILE`. rustls rejects both CA:TRUE certificates presented as a server certificate and self-signed certificates used as their own trust anchor, so apply regenerates the material automatically (and restarts Kanidm) if it finds anything that doesn't verify against the local CA.

Pin the Kanidm image tag in `deploy.yaml` (`kanidm.tag`) instead of floating `latest` for production. New installs default to **1.11.1**. If you already run an older tag, Kanidm requires **sequential** upgrades (1.7 → 1.8 → … → target); run `kanidmd domain upgrade-check` before each step. With almost no data yet, a fresh `data_dir` on the latest tag is often simpler.

### Identity

On first apply the kit:

1. Authenticates the built-in `idm_admin` account, recovering it only when needed. On Kanidm 1.7.3, if recovery fails after a previous attempt, apply runs `disable-account` then `recover-account` automatically.
2. Creates the initial person from `users:` and prints a one-time credential enrollment link. Open that link to set the person's web/OIDC password and optional MFA.
3. Creates the groups listed in `groups:` and memberships on that person.
4. Registers OAuth2/OIDC clients from `oidc.clients` and engine sidecars.
5. Applies portal branding (display name, logo) and OAuth2 application icons.
6. Deploys the optional **Kanidm Admin UI** (enabled by default): registers its
   `kanidm_admin_ui` OIDC client, creates the `admin_ui_svc` service account
   (member of `idm_admins`, read-write API token), and enrolls the initial
   people in `idm_admins` so they can log in.
7. Creates a `stalwart-ldap` service account and API token for directory search, and adds it to `idm_mail_servers` so Stalwart can see person `mail` attributes.

After that, manage people and groups **in Kanidm**, not in OpenCloud, Matrix, or Stalwart.

`person posix set-password` only sets a Unix/LDAP password; it does **not**
create a web-login credential. Easy Deploy instead uses Kanidm's supported
credential enrollment/reset links.

### Admin UI

The kit ships [Kanidm Admin UI](https://github.com/opencomp-eu/kanidm-admin-ui) — a web console for managing people, groups, and OAuth2 apps without the CLI. It is served at `https://admin.<organisation domain>` (e.g. identity `idm.example.com` → `admin.example.com`), with HTTPS from the same Caddy that fronts the Kanidm portal.

- Logins go through Kanidm OIDC and are restricted to members of `idm_admins` (override with `admin_ui.admin_group`). The wizard's initial person is enrolled automatically.
- Kanidm stays the source of truth; the UI talks to Kanidm through a dedicated `admin_ui_svc` service account (API token stored in `secrets.yaml`, never exposed to the browser).
- Enabled by default. Disable with the wizard (`n` at "Enable Kanidm Admin UI?") or in `deploy.yaml`:

  ```yaml
  admin_ui:
    enabled: false
  ```

  Re-apply afterwards: the `kanidm_admin_ui` OAuth2 client and the container are removed, and the Caddy site block disappears. Set `admin_ui.domain` to serve it on a different host.

The kit wires the Admin UI for Kanidm's self-signed TLS (`KANIDM_TLS_CA_FILE`), the per-client OIDC issuer, and public reset links (`KANIDM_PUBLIC_URL`); the pinned `admin_ui.tag` tracks a known-good kanidm-admin-ui release (0.1.1+, which implements that contract).

### Protocols

| Protocol | Where |
|----------|--------|
| HTTPS / OIDC | `https://idm.example.com` — per-client issuer `https://idm.example.com/oauth2/openid/<client_id>` |
| LDAPS | `ldaps://kanidm:3636` on the Docker network (read-only; POSIX password bind) |

Kanidm OIDC issuers are **per client**. OpenCloud uses `/oauth2/openid/opencloud`; Matrix uses `/oauth2/openid/matrix`.

### Branding

Kanidm ships with a Ferris-the-crab logo by default. This kit replaces it with a bundled professional logo and lets you customise branding in `deploy.yaml`:

```yaml
branding:
  display_name: Acme Organisation
  logo: branding/acme.svg          # local path or https URL
  oauth2_icons: true               # fetch each app's favicon (default)
```

- Omit `logo` to keep the bundled default.
- Set `logo: false` to leave the portal image unchanged.
- OAuth2 application icons are fetched from each client's `landing_url` by default. Override per client with `oidc.clients[].image`, or disable with `oauth2_icons: false`.
- The sign-in page uses `assets/branding/background.jpg` behind a blurred overlay, and a full-width Begin button. Replace that image (or edit `assets/branding/override.css`) and recreate the `kanidm` container to pick up changes.

Portal **logo** images must be PNG, JPG, GIF, SVG, or WebP and under 256 KB (Kanidm's limit). The login **background** is served as a static file and is not subject to that cap. Portal branding uses Kanidm's `admin` account (not `idm_admin`); `apply.sh` recovers and stores `ADMIN_PASSWORD` in secrets automatically.

## Day-to-day

```bash
bash apply.sh              # re-render config and reconcile stack
bash apply.sh --skip-runtime   # render only, no docker
bash kanidm-cli.sh login --name idm_admin   # CLI admin (after apply)
bash user.sh create alice "Alice Example" --email alice@example.com
bash user.sh reset alice   # issue a new one-time credential reset link
bash start.sh              # compose up (via apply, skip pull)
bash stop.sh               # compose down
```

## Backups

Back up:

- `kanidm.data_dir` (database, TLS, `server.toml`)
- `.kanidm-easy-deploy/secrets.yaml`

## Development

```bash
uv sync --dev
uv run pytest
```

## License

Same as sibling easy-deploy projects.
