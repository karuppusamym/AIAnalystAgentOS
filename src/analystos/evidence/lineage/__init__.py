"""Column lineage from SQL text and dbt manifests (P6-07), rebuilt on sqlglot `qualify` + `lineage()`.

Recipe lineage comes from the IR (`recipes/lineage.py`); this package covers SQL the platform did
not compile itself: views, INSERT ... SELECT, and the compiled SQL of dbt models.
"""
