# Wiring the `/map` endpoint to a live map

The backend already returns map-ready **GeoJSON**. You don't need to reshape
anything — point a map library at the response and add the pins.

---

## 1. The endpoint

```
GET http://localhost:8000/map
```

Run the backend locally:

```bash
cd backend
uvicorn main:app --reload           # serves on http://localhost:8000
```

Response is a GeoJSON `FeatureCollection` — **one `Point` feature per ERCOT
project** (keyed by `inr`). TCEQ permits and PUCT filings are folded into each
project, not shown as separate pins:

```jsonc
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [-95.37, 29.76] }, // [lng, lat]
      "properties": {
        // ── ERCOT core (pin identity + styling) ──
        "inr": "24INR0123",
        "name": "Acme Power LLC",       // interconnecting entity (falls back to project_name)
        "project_name": "Brazos Solar 1",
        "county": "HARRIS",
        "state": "TX",
        "zone": "COAST",
        "status": "active",             // active | inactive | cancelled
        "capacity_mw": 250.0,
        "fuel": "Gas",
        "technology": "Combined Cycle",
        "gim_study_phase": "SS",
        "projected_cod": "2027-06-01",
        "stage": "regulatory",          // queued | permitting | regulatory | approved
        "precision": "exact",           // exact (FRS coords) | county (centroid)

        // ── precomputed insights (style / rank without re-deriving) ──
        "has_air_permit": true,
        "has_regulatory": true,
        "permit_count": 2,
        "event_count": 5,
        "milestones_hit": 3,            // out of milestones_total
        "milestones_total": 5,
        "latest_event": "PUCT order issued",
        "latest_event_date": "2026-05-14",
        "days_in_queue": 612,
        "match_confidence": 0.94,       // best resolved_links score
        "match_status": "resolved",

        // ── detail payloads (JSON strings — JSON.parse on click) ──
        "milestones_json": "{\"in_queue\":true,\"air_permit\":true,...}",
        "timeline_json": "[{\"date\":\"2024-11-01\",\"source\":\"ercot\",\"type\":\"queue_seen\",\"label\":\"Seen in ERCOT queue\"}, ...]",
        "permits_json": "[{\"rn_number\":\"RN123\",\"permit_no\":\"PSD-TX-1\",\"site_name\":\"...\",\"on_thesis\":true,\"precision\":\"exact\"}, ...]"
      }
    }
  ],
  "meta": {
    "counts": {
      "total": 812, "exact": 540, "county": 272,
      "with_permit": 410, "with_regulatory": 96, "capacity_mw": 148230
    },
    "by_stage": { "queued": 400, "permitting": 320, "regulatory": 72, "approved": 20 }
  }
}
```

> **Nested payloads are JSON strings.** `timeline_json`, `permits_json`, and
> `milestones_json` are stringified so they survive MapLibre's property
> serialization intact. `JSON.parse` them inside your click handler.

> **Coordinate order:** GeoJSON is `[longitude, latitude]`. MapLibre, Mapbox,
> and `L.geoJSON` all expect this order natively — don't swap it.

---

## 2. Query params (all optional)

| Param        | Values / type              | What it does                                             |
| ------------ | -------------------------- | -------------------------------------------------------- |
| `stage`      | `queued` \| `permitting` \| `regulatory` \| `approved` | Furthest stage reached.      |
| `status`     | `active` \| `inactive` \| `cancelled` | ERCOT lifecycle status.                       |
| `has_permit` | `true` \| `false`          | Only projects with (or without) a linked TCEQ air permit.|
| `on_thesis`  | `true` \| `false`          | Only projects with a power-generation (on-thesis) permit.|
| `county`     | string, e.g. `HARRIS`      | County filter.                                           |
| `min_mw`     | number                     | Minimum ERCOT capacity in MW.                            |
| `limit`      | number (≤ 20000)           | Cap features returned. Default 5000.                     |

Example: `GET /map?stage=regulatory&on_thesis=true&min_mw=100`

---

## 3. Drop it on a map (MapLibre GL — free, no token)

```js
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

const map = new maplibregl.Map({
  container: "map",
  style: "https://demotiles.maplibre.org/style.json", // swap for your basemap
  center: [-99.5, 31.3],  // Texas
  zoom: 5,
});

map.on("load", async () => {
  const res = await fetch("http://localhost:8000/map");
  const geojson = await res.json();

  map.addSource("projects", { type: "geojson", data: geojson });

  map.addLayer({
    id: "project-pins",
    type: "circle",
    source: "projects",
    paint: {
      // color by stage
      "circle-color": [
        "match", ["get", "stage"],
        "queued",      "#8b98a9",
        "permitting",  "#ffb020",
        "regulatory",  "#4c9bff",
        "approved",    "#35d0a5",
        "#64748b",
      ],
      // bigger dot = confirmed exact location
      "circle-radius": ["case", ["==", ["get", "precision"], "exact"], 7, 4],
      "circle-stroke-width": 1,
      "circle-stroke-color": "#fff",
    },
  });

  // popup on click
  map.on("click", "project-pins", (e) => {
    const p = e.features[0].properties;
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`<strong>${p.name}</strong><br>${p.county} · ${p.stage}<br>${p.capacity_mw ?? "?"} MW`)
      .addTo(map);
  });
});
```

### Leaflet alternative

```js
const res = await fetch("http://localhost:8000/map");
const geojson = await res.json();

L.geoJSON(geojson, {
  pointToLayer: (feature, latlng) =>
    L.circleMarker(latlng, { radius: feature.properties.precision === "exact" ? 7 : 4 }),
  onEachFeature: (feature, layer) => {
    const p = feature.properties;
    layer.bindPopup(`<strong>${p.name}</strong><br>${p.county} · ${p.stage}`);
  },
}).addTo(map);
```

---

## 4. What the fields mean (for styling)

- **`stage`** — the furthest maturity the project has reached. Use it for pin
  color (it's a sequential ramp).
  - `queued` — in the ERCOT interconnection queue, no air permit linked.
  - `permitting` — has a linked TCEQ air permit (environmental review underway).
  - `regulatory` — has linked PUCT docket activity (public-utility review).
  - `approved` — ERCOT approved it for energization/synchronization.
- **`timeline_json` / `permits_json` / `milestones_json`** — `JSON.parse` these
  in your click handler to render a project dossier (merged multi-source event
  timeline, linked TCEQ permits, milestone checklist).
- **`precision`** — `exact` means real FRS coordinates; `county` means it's
  placed on the county centroid (many pins may stack on the same point).
  Use it to size/fade pins or to warn users a location is approximate.
- **`meta.counts` / `meta.by_stage`** — ready-made totals for a legend or
  summary bar; no need to recompute client-side.

---

## ⚠️ Two things to sort out before it works from the browser

1. **CORS is not enabled yet.** A browser `fetch` from the frontend origin will
   be blocked. Ask the backend team to add this to `backend/main.py`:

   ```python
   from fastapi.middleware.cors import CORSMiddleware

   app.add_middleware(
       CORSMiddleware,
       allow_origins=["http://localhost:5173"],  # your frontend dev origin
       allow_methods=["GET"],
       allow_headers=["*"],
   )
   ```

2. **Supabase must be configured.** `/map` returns `400` if `SUPABASE_URL` and
   `SUPABASE_SERVICE_KEY` aren't set, and it only has data after a discovery run
   (`POST /discover/ercot`, `POST /discover/tceq`) has populated the tables.
