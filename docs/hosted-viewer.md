# Hosted run viewer

`simmons publish` puts run views on an unlisted URL so teammates can read results
without cloning the repo or running anything.

## Publish

```bash
uv run simmons publish runs-local/<experiment>/<run-id>   # add a scratch run, then publish
uv run simmons publish                                    # republish the current set
uv run simmons publish --remove runs-local/<experiment>/<run-id>
uv run simmons publish --build-only                       # render locally, no deploy
```

The site holds every committed run under `runs/` plus the scratch runs listed in
`runs-local/published.json`. Each publish re-renders all of them from the run
records into `runs-local/.publish/site/` and replaces the whole site. Run
directories are only read. A run still in progress is listed without a link.

The command prints the URL. It is `https://simmons.stage11.dev/<path>/`, where
`<path>` is a random segment stored in `runs-local/published.json`. Share that
URL. The bare hostname and any other path answer 404, and every response sends
`X-Robots-Tag: noindex, nofollow`.

## Access model

Unlisted, not private (Atin, 2026-10-04). Anyone holding the URL can read every
published prompt and model output. To retire a URL, delete `path` from
`runs-local/published.json` and publish again: the old path stops serving and a
new one is printed.

## Credentials

Set these in the repo's ignored `.env` (or the environment):

```
SIMMONS_CLOUDFLARE_API_TOKEN=   # Cloudflare token with Workers Scripts: Edit and Workers Routes: Edit
CLOUDFLARE_ACCOUNT_ID=          # Stage11 Projects account
```

Deploys use Cloudflare's `cf` CLI, so Node.js must be installed. Each publish writes a
small deploy project into `runs-local/.publish/` (`package.json` pinning `cf` and the
Wrangler it delegates static-assets builds to, `cloudflare.config.ts`, `wrangler.config.ts`),
runs `npm install` there, then `npx --no-install cf deploy`. The first publish on a machine
downloads the packages; later ones reuse them.

## One publisher at a time

The published list and path live in `runs-local/`, which is per machine, and
each deploy replaces the whole site. Publish from one machine. To publish from
another, copy `runs-local/published.json` and the listed run directories there
first, or set `SIMMONS_PUBLISH_PATH` to keep the same URL.

## Hosting

| Piece | Value |
|---|---|
| Worker | `simmons-viewer` (static assets only, no script), Stage11 Projects Cloudflare account |
| Hostname | `simmons.stage11.dev`, a Workers Custom Domain (DNS record created by the deploy) |
| Other hostnames | none: `workers_dev` and `preview_urls` are off |
| Overrides | `SIMMONS_PUBLISH_HOST`, `SIMMONS_PUBLISH_WORKER`, `SIMMONS_PUBLISH_PATH` |
