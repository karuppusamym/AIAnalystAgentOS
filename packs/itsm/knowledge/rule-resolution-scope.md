---
kind: rule
name: "Resolution time counts resolved records only"
synonyms: ["MTTR scope"]
maps_to: [incident.resolved_at]
---
Resolution-time measures (MTTR, median resolution) use only records whose resolved timestamp is at or after the opened timestamp. Open records, and records resolved before they were opened, are data-quality issues, not zero-hour resolutions.
