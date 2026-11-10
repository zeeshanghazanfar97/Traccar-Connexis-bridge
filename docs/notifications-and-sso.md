# Email notifications, reports, and SSO

Both Compose files support optional SMTP and native OpenID Connect login using the existing Traccar image. Blank `SMTP_HOST` disables global SMTP; blank `OPENID_CLIENT_ID` disables SSO. These settings belong to the **Traccar service** and do not require rebuilding or publishing the adapter image.

## Apply configuration changes

Copy the updated Compose file to the server and add the desired settings from `.env.example` to your existing `.env`. Keep existing credentials and the Compose project name. From your production Compose directory:

```bash
docker compose config --quiet
docker compose up -d --no-deps --force-recreate traccar
docker compose logs --tail=80 traccar
```

For development, use `docker compose -p traccar-dev -f compose.dev.yml` with the same subcommands. In Portainer, update the stack YAML and its environment variables, then redeploy the stack. A plain container restart does not apply changed Compose environment values.

The inline startup guard removes inactive keys because Traccar 6.17.0 treats the *presence* of `MAIL_SMTP_HOST` and `OPENID_CLIENT_ID` as enabling those features, even if the values are empty. It then executes the image's original Java command. Incomplete SSO settings and conflicting SMTP TLS modes fail with a configuration message before Java starts.

## SMTP

Use your mail provider's SMTP host, credentials, and authorized sender. For port 587 with mandatory STARTTLS:

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=traccar@example.com
SMTP_PASSWORD='your-smtp-password-or-app-password'
SMTP_FROM=traccar@example.com
SMTP_FROM_NAME=Traccar
SMTP_STARTTLS=true
SMTP_SSL=false
SMTP_TIMEOUT=10000
```

For implicit TLS on port 465, change these three settings:

```dotenv
SMTP_PORT=465
SMTP_STARTTLS=false
SMTP_SSL=true
```

`SMTP_STARTTLS=true` both enables and requires the TLS upgrade. Configure only one TLS mode. For an explicitly trusted plaintext relay, set both TLS options to `false`; leave the username and password blank if that relay needs no authentication. Traccar uses the supplied username and password for authenticated SMTP; no separate `SMTP_AUTH` switch is needed in the pinned version. `SMTP_TIMEOUT` applies to connection, read, and write operations in milliseconds.

The container needs outbound access to the SMTP server. No inbound SMTP port or extra service is required. The global configuration applies to notifications, report emails, and password resets. Existing per-user SMTP attributes can override it; remove those attributes if you want that user to use the global settings.

### Verify email and configure notifications

1. Set a valid recipient email in your Traccar account settings.
2. In **Settings → Notifications**, create or edit a notification, select the **Email** channel, and use the channel's test action. Confirm receipt and check the Traccar logs if it fails.
3. Choose the event types you want, such as ignition changes or geofence entry/exit. Connect the notification to the relevant devices/groups and recipients; recipients must also have access to those devices.

SMTP setup does not automatically subscribe users to every event. The adapter forwards position and ignition data; Traccar generates the supported events and sends notifications.

### Schedule email reports

Create a calendar with recurring reporting intervals in Traccar, then open **Reports**, select a report type that supports scheduling, choose your devices/groups, and use **Schedule** to select the calendar and a description. For a daily report, use a recurring full-day calendar interval in your intended timezone. Traccar emails the report after each interval ends. Review existing schedules under **Reports → Scheduled Reports**.

Verify the recipient's email delivery and device permissions before relying on a schedule. SMTP alone does not create schedules.

## Native SSO with Authentik

The existing `authentik@file` middleware authenticates requests at Traefik. Native OpenID Connect also signs the user into **Traccar**. Set up a dedicated OAuth2/OIDC application/provider in your existing Authentik installation; its proxy provider alone is insufficient.

### 1. Create the provider

In the Authentik admin interface, create an application and OAuth2/OpenID Connect provider with:

| Setting | Value |
| --- | --- |
| Application name / slug | `Traccar` / `traccar` (use another slug if already taken) |
| Launch URL | `https://traccar.zeeit.dev` |
| Client type | Confidential |
| Redirect URI matching | Strict |
| Redirect URI | `https://traccar.zeeit.dev/api/session/openid/callback` |
| Scopes / property mappings | `openid`, `profile`, `email` |
| Issuer mode | Per provider |

Use your actual `TRACCAR_DOMAIN` in both URLs. Choose your normal authorization flow and restrict application access to the intended users/groups with Authentik policy bindings. Keep the existing proxy application if it is still serving the middleware.

Copy the client ID, client secret, and **issuer** from the provider's details. With per-provider issuer mode, it normally looks like `https://YOUR-AUTHENTIK-HOST/application/o/traccar/`. Preserve its trailing slash. The issuer is not the URL ending in `.well-known/openid-configuration`. Both the browser and Traccar container must be able to reach Authentik using its public HTTPS hostname and a trusted certificate.

For development, add a separate strict redirect URI `http://localhost:8082/api/session/openid/callback` (adjust the port to `TRACCAR_WEB_PORT`), preferably on a separate development provider. Development uses that localhost URL for report links and SSO callbacks.

### 2. Enable SSO in Traccar

```dotenv
OPENID_CLIENT_ID=your-authentik-client-id
OPENID_CLIENT_SECRET='your-authentik-client-secret'
OPENID_ISSUER_URL=https://YOUR-AUTHENTIK-HOST/application/o/traccar/
OPENID_FORCE=false
OPENID_ALLOW_REGISTRATION=false
OPENID_ALLOW_GROUP=
OPENID_ADMIN_GROUP=
```

Apply the configuration and choose **Login with OpenID** on Traccar's login screen. Keep a local administrator account while testing. Traccar matches an existing account by email, so use the same email in Authentik and Traccar to retain that account's devices and permissions. Use trustworthy email claims and restrict who can edit identity information in Authentik.

`OPENID_ALLOW_REGISTRATION=false` respects Traccar's server registration setting: an unknown SSO user is still allowed if server registration is enabled. To admit only existing accounts, also disable registration in Traccar's server settings. Set this variable to `true` only when you want permitted Authentik users provisioned automatically even with server registration disabled. New accounts still need their Traccar device/group permissions assigned.

After verifying login, you can set `OPENID_FORCE=true` to redirect directly to Authentik and disable local password login. To recover local login, set it back to `false` and recreate Traccar. To disable SSO entirely, also clear `OPENID_CLIENT_ID`, `OPENID_CLIENT_SECRET`, and `OPENID_ISSUER_URL`.

### 3. Choose how Traefik authenticates requests

`TRACCAR_MIDDLEWARES=authentik@file` keeps your current forward-auth protection. It is the default and can remain while testing OIDC.

Once native OIDC and Authentik's application access policies work, you can remove the extra proxy login for this router by explicitly setting:

```dotenv
TRACCAR_MIDDLEWARES=
```

Recreate Traccar to update its Traefik labels. This leaves Traccar responsible for authentication, with OIDC available and local password login controlled by `OPENID_FORCE`. The HTTPS router, `traefik-public` network, `websecure` entrypoint, and `cloudflare` certificate resolver are unchanged. Removing the variable entirely restores `authentik@file`; an explicitly empty value removes the middleware. You can also provide a comma-separated list of other existing middleware names.

Browser-based forward auth can interfere with API and Traccar Manager clients. When using those clients, prefer native OIDC without the proxy authentication middleware. For Traccar Manager's OIDC login, also register the strict redirect URI `org.traccar.manager://api/session/openid/callback` on the Authentik provider.

### Optional group restrictions and administrators

Leave group variables blank initially to preserve existing account roles. To manage groups through Authentik, create an OAuth2 scope mapping named `groups` with this expression, then add it to this provider's selected scope mappings:

```python
return {"groups": [group.name for group in request.user.ak_groups.all()]}
```

Traccar 6.17.0 explicitly requests the `groups` scope when `OPENID_ADMIN_GROUP` is set. For both access restrictions and admin assignment, configure the exact Authentik group names:

```dotenv
OPENID_ALLOW_GROUP=traccar-users
OPENID_ADMIN_GROUP=traccar-admins
```

Members of the admin group are also allowed access, even without membership in the allow group. Assign the admin group deliberately: it grants Traccar administrator privileges to provisioned accounts. For existing local accounts, verify their roles in Traccar; the pinned version does not automatically synchronize admin status for every existing local account.

If you want only an allow group and no admin mapping, make sure the `groups` claim is included in an already requested scope, such as a custom `profile` mapping, or restrict access with Authentik application policy bindings instead. Setting only `OPENID_ALLOW_GROUP` does not make this Traccar version request an additional `groups` scope.

## References

- [Traccar SMTP and notification channels](https://www.traccar.org/notifications/)
- [Traccar reports and schedules](https://www.traccar.org/reports/)
- [Traccar OpenID Connect setup](https://www.traccar.org/openid-sso/)
- [Traccar configuration variables](https://www.traccar.org/configuration-file/)
- [Authentik OAuth2/OIDC provider](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)
- [Traccar 6.17.0 configuration and authentication implementation](https://github.com/traccar/traccar/tree/45ae7321fe56688ca26f2f5b62e6dbb7a4737562/src/main/java/org/traccar)
