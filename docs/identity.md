# Kanidm as the organisation identity source

Easy Deploy treats Kanidm as the authoritative directory for the organisation.

```
                    EasyDeploy
                        │
                        ▼
                     Kanidm
                  users / groups
                  authentication
                  OIDC / LDAP
                        │
              ┌─────────┼─────────┐
              │         │         │
             OIDC      OIDC      LDAP
              │         │         │
          OpenCloud   Matrix    Stalwart
```

Do **not** create independent application accounts. Provision people and groups in Kanidm; the apps consume those identities.

## OIDC (OpenCloud, Matrix)

Kanidm uses a **per-client issuer**:

```
https://idm.example.com/oauth2/openid/<client_id>
```

Discovery is at that URL plus `/.well-known/openid-configuration`.

| App | Client | Type | Typical scopes |
|-----|--------|------|----------------|
| OpenCloud | `opencloud` | public + PKCE | `openid profile email groups groups_name` |
| Matrix MAS | `matrix` | confidential | `openid profile email` |
| Stalwart / webmail | `stalwart` | confidential (optional) | `openid profile email` |

On a same-VPS engine install, `easydeploy-engine` writes client sidecars under `.kanidm-easy-deploy/integration/oidc-clients.d/` and provider sidecars into each app kit. Re-apply Kanidm first, then the apps.

## LDAP (Stalwart)

Kanidm LDAP is read-only LDAPS on `kanidm:3636`.

- Base DN is derived from the identity domain (`idm.example.com` → `dc=idm,dc=example,dc=com`).
- Directory search uses the `stalwart-ldap` service account (`dn=token` + API token).
- IMAP/SMTP password binds use each person's **POSIX** password (enabled on people created by this kit).

Anonymous bind is not supported. StartTLS is not supported.

## Groups

The wizard creates these groups so role mapping is consistent:

- `opencloud-admin`, `opencloud-user`, `opencloud-guest`
- `matrix-admins`
- `mail-users`

Add further people to these groups in Kanidm.
