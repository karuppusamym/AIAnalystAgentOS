"""Catalog profile output must not expose sensitive value distributions, including legacy rows."""
from analystos.api.routers.catalog import public_profile


def test_sensitive_profile_projection_strips_old_value_fields():
    profile = {"name": "salary", "non_null": 10, "null_rate": 0.1, "distinct": 8,
               "min": 1, "max": 100, "mean": 20, "percentiles": {"p50": 15},
               "top_values": [{"value": 100, "count": 2}], "histogram": [{"count": 2}],
               "monthly_counts": [{"month": "2026-01", "count": 2}]}
    assert public_profile(profile, ["restricted"]) == {"name": "salary", "non_null": 10,
                                                       "null_rate": 0.1, "distinct": 8}
    assert public_profile(profile, []) == profile
    assert public_profile({}, ["pii"]) is None
