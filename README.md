# Tarnów Wydarzenia

Live iCal feeds of every event in Tarnów and a 50 km radius, merged from many sources,
deduplicated, each event linking back to every source that reported it.

## Feeds

| Feed | Contents |
|---|---|
| `wszystkie.ics` | everything |
| `tarnow.ics` | city of Tarnów only |
| `okolice.ics` | outside Tarnów, up to 50 km |
| `kategoria/<slug>.ics` | `koncerty`, `teatr`, `kino`, `sport`, `wystawy`, `dzieci`, `spotkania`, `imprezy`, `festiwale`, `uroczystosci`, `inne` |
| `zrodlo/<source-id>.ics` | one source (debugging) |

`index.html` lists every feed with Subscribe / Google Calendar links, the next 150 events and
the health of every source. `status.json` has per-source counts and errors from the last run.

## How it works

```
sources.yaml ──► adapters ──► normalize ──► geocode + 50 km filter ──► categorize ──► dedup ──► .ics
                 ics          (Europe/      (Nominatim, cached)        (keywords /    (fuzzy title +
                 tribe         Warsaw)                                  Gemini)        day + place)
                 jsonld
                 html_ai  ── Gemini (cached by page hash)
                 rss      ── Gemini
                 facebook ◄── fb.com/events links discovered in all other sources
```

| Adapter `type` | Use for | AI? |
|---|---|---|
| `ics` | sites publishing iCal | no |
| `tribe` | WordPress + The Events Calendar (`/wp-json/tribe/events/v1/events`) | no |
| `jsonld` | pages with schema.org `Event` JSON-LD (ticketing, modern CMS); `follow:` regex reads detail pages | no |
| `html_ai` | any HTML event list; `selector:` narrows the page | yes |
| `rss` | news feeds whose posts announce events | yes |
| `facebook` | public FB event permalinks (`events:` list + auto-discovered) | no |

**Dedup:** same local day + fuzzy title match (rapidfuzz, noise words like "koncert" stripped) +
start within 60 min + not in different towns. The highest-`priority` source becomes the primary
(organiser 80 > venue 70 > municipal 60 > aggregator 40 > media 30), the rest fill gaps and are
listed under "Źródła" in the description. UIDs persist in `state.json`, so a calendar app
updates an event instead of duplicating it when a new source starts reporting it.

**Facebook:** a Page's event list is behind a login wall for non-browser clients (verified
2026-11-23), so FB pages cannot be crawled. Single public event permalinks can. They are found
automatically wherever another source links to them, or added by hand under the `facebook`
source's `events:` list.

## Run locally (Docker)

1. `cp .env.example .env` and set `GEMINI_API_KEY`
2. `docker compose up --build` – builds once now, then every 24 h
3. Open http://localhost:8080

Useful one-offs:

```sh
docker compose run --rm scraper list                     # configured sources
docker compose run --rm scraper check tcil-kalendarz     # what one source returns
docker compose run --rm scraper run --only a,b --no-ai   # partial run, no Gemini
docker compose run --rm --entrypoint pytest scraper -q   # tests
```

## Deploy (free)

GitHub Actions + GitHub Pages, no server:

1. Push to a **public** GitHub repo (Actions minutes and Pages are free for public repos)
2. Settings → Pages → Source: **GitHub Actions**
3. Settings → Secrets → Actions → `GEMINI_API_KEY`
4. Actions → `build-feeds` → Run workflow

Feeds: `https://<user>.github.io/<repo>/wszystkie.ics`. Runs every 6 hours.

Cost: 0 zł. Gemini free tier (flash-lite ≈ 500 req/day) is enough because every AI answer is
cached by page content – only pages that changed since yesterday are re-sent.

## Adding a source

Add an entry to `sources.yaml`, then `docker compose run --rm scraper check <id>`:

```yaml
- id: mok-debica
  name: MOK Dębica
  type: html_ai            # ics | tribe | jsonld | html_ai | rss | facebook
  url: https://mok.debica.pl/wydarzenia
  group: kultura
  category: inne           # default when the event itself does not say
  town: Dębica             # default town for events without a location
  priority: 70
  selector: ".event-list"  # optional, html_ai only
```
