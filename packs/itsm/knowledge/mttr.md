---
kind: metric
name: "MTTR"
synonyms: ["mean time to resolve", "resolution time"]
maps_to: [incident.opened_at, incident.resolved_at]
---
Mean Time to Resolve: average of resolved_at - opened_at for resolved incidents, in hours.
