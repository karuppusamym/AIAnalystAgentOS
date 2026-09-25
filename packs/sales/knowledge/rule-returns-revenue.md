---
kind: rule
name: "Returned orders stay in revenue"
synonyms: ["gross vs net of returns"]
maps_to: [orders.returned, orders.net_amount]
---
Net revenue is booked at order time; returned orders are not subtracted from net_amount. Report return rate alongside revenue rather than netting returns out silently.
