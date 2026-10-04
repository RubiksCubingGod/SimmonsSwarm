# Infrastructure

External resources this project uses. Update when one is added or removed.

| Resource | Where | Purpose |
|---|---|---|
| OpenRouter API | `OPENROUTER_API_KEY` in `.env` | Model calls for real runs |
| Cloudflare Worker `simmons-viewer` | Stage11 Projects account | Static hosted run viewer (`simmons publish`) |
| DNS `simmons.stage11.dev` | Stage11 Projects, zone `stage11.dev` | Workers Custom Domain for the viewer; created by the first deploy, 2026-10-04 |

The viewer is unlisted, not access-gated; see `docs/hosted-viewer.md`.
