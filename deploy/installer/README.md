# Installer endpoint

`https://install.openhack.com` serves the repository's `install.sh` directly as
plain text. The same script is available at `/install.sh`. The Worker embeds the
script at deployment time; it does not fetch mutable source on each request.

From the repository root, with Cloudflare access to the OpenHack account:

```sh
npx wrangler@4.166.0 deploy --config deploy/installer/wrangler.toml
```

Redeploy after changing `install.sh`. Wrangler manages the custom domain and
HTTPS certificate. Verify that the deployed response exactly matches the source:

```sh
curl -fsSL https://install.openhack.com -o /tmp/openhack-install.sh
cmp install.sh /tmp/openhack-install.sh
sh -n /tmp/openhack-install.sh
```
