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

Response is a GeoJSON `FeatureCollection` — one `Point` feature per project:

```jsonc
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [-95.37, 29.76] }, // [lng, lat]
      "properties": {
        "layer": "tceq",              // tceq | ercot
        "stage": "permitting",        // queued | permitting | permit_only
        "id": "tceq:RN123:PSD-TX-1",
        "name": "Acme Power LLC",
        "county": "HARRIS",
        "state": "TX",
        "precision": "exact",         // exact (FRS coords) | county (centroid)
        "capacity_mw": 250.0,
        "fuel": "GAS",
        "technology": "CC",
        "on_thesis": true,
        "resolution_status": "resolved"
      }
    }
  ],
  "meta": {
    "counts": { "total": 812, "exact": 540, "county": 272 },
    "by_stage": { "queued": 400, "permitting": 300, "permit_only": 112 }
  }
}
```

> **Coordinate order:** GeoJSON is `[longitude, latitude]`. MapLibre, Mapbox,
> and `L.geoJSON` all expect this order natively — don't swap it.

---

## 2. Query params (all optional)

| Param           | Values / type              | What it does                                        |
| --------------- | -------------------------- | --------------------------------------------------- |
| `source`        | `all` \| `tceq` \| `ercot` | Which layer(s) to include. Default `all`.           |
| `stage`         | `queued` \| `permitting` \| `permit_only` | Filter by funnel stage.              |
| `resolved_only` | `true` \| `false`          | Only permits confidently linked to an ERCOT project.|
| `on_thesis`     | `true` \| `false`          | Only electric-power-generation NAICS permits.       |
| `county`        | string, e.g. `HARRIS`      | County filter.                                       |
| `min_mw`        | number                     | Minimum ERCOT capacity in MW.                       |
| `limit`         | number (≤ 20000)           | Cap features returned. Default 5000.                |

Example: `GET /map?source=tceq&on_thesis=true&min_mw=100`

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
  const res = await fetch("http://localhost:8000/map?source=all");
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
        "queued",      "#94a3b8",
        "permitting",  "#f59e0b",
        "permit_only", "#3b82f6",
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

- **`stage`** — how far along the project is. Use it for pin color.
  - `queued` — in the ERCOT interconnection queue, no permit yet.
  - `permitting` — has a TCEQ air permit *and* links to a queue project.
  - `permit_only` — has a TCEQ air permit, no ERCOT match.
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
