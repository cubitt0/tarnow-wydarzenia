# Polish fetch proxy (GCP, Warsaw)

Some sources (municipal CMS hosting, Cloudflare, government firewalls) answer GitHub Actions'
US datacenter IPs with 403 or drop the connection. Sources marked `proxy: true` in
`sources.yaml` are fetched through this Cloud Run function in `europe-central2`, so the sites
see a Polish IP. The scraper uses it only when `TE_PROXY_URL` and `TE_PROXY_TOKEN` are set.

- Auth: `X-Proxy-Token` header; only hosts in `ALLOWED_HOSTS` are fetched (redirects included).
- Hard cap: `MONTHLY_LIMIT` accepted requests per calendar month, counted in Firestore
  (`usage/<YYYY-MM>`). Over it, the function answers 429 and the scraper reports
  "PL proxy monthly limit reached" for those sources until the next month.
- `?url=ip` shows the IP and country the sites see (counts as one request).

`env.yaml` (gitignored) holds `PROXY_TOKEN`, `ALLOWED_HOSTS`, `MONTHLY_LIMIT`, and in comments
the project and function names. When adding a `proxy: true` source on a new host, add the host
there and redeploy:

```bash
gcloud functions deploy <function> --gen2 --runtime=python312 --region=europe-central2 \
  --source=. --entry-point=fetch --trigger-http --allow-unauthenticated \
  --env-vars-file=env.yaml --memory=256Mi --timeout=60s --max-instances=2 \
  --service-account=<proxy service account> --project=<project> --format=none
```

`--format=none` keeps the deploy from printing the environment variables, token included.
