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
             OIDC      OIDC      OIDC (webmail)
              │         │         LDAP (directory + IMAP fallback)
              │         │         │
          OpenCloud   Matrix    Stalwart / Bulwark
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
| Stalwart / webmail | `stalwart-webui` | public + PKCE | `openid profile email` (landing URL is Bulwark / webmail) |

OpenCloud no longer uses the `oidc` role-assignment driver with Kanidm. Custom claims such as `opencloudRoles` often never reach the access token or UserInfo, which sent people to `/access-denied` after a successful login. The Kanidm overlay assigns the built-in `user` role at login (`PROXY_ROLE_ASSIGNMENT_DRIVER=default`). The operator in `opencloud-admin` is still recorded as `OC_ADMIN_USER_ID`.

The OpenCloud web client still requests the `groups` scope; that scope must stay on the Kanidm client or Kanidm denies the grant.

On a same-VPS engine install, `easydeploy-engine` writes client sidecars under `.kanidm-easy-deploy/integration/oidc-clients.d/` and provider sidecars into each app kit. Re-apply Kanidm first, then the apps.

## LDAP (Stalwart)

Kanidm LDAP is read-only LDAPS on `kanidm:3636`.

- Base DN is derived from the identity domain (`idm.example.com` → `dc=idm,dc=example,dc=com`).
- Directory search uses the `stalwart-ldap` service account (`dn=token` + API token).
- That account is in `idm_mail_servers` and `idm_people_pii_read` so login-by-email can see `mail` (Kanidm hides mail from anonymous/unprivileged binds).
- IMAP/SMTP/WebUI password binds are unused while Stalwart selects the Kanidm OIDC directory for Bulwark SSO. Desktop and phone clients should use a **Stalwart app password**. Set `identity.auth_directory: ldap` on the Stalwart kit to bind with the Kanidm password again.

Anonymous bind is not supported. StartTLS is not supported.

## Groups

The wizard creates these groups so role mapping is consistent:

- `opencloud-admin`, `opencloud-user`, `opencloud-guest`
- `matrix-admins`
- `mail-users`

Add further people to these groups in Kanidm.
