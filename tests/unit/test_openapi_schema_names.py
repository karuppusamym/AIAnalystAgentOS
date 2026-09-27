"""Two request/response models with the same class name in different routers make FastAPI qualify both
(`analystos__api__routers__x__RunIn`), which silently renames the schema the web client is generated from."""
from __future__ import annotations


def test_no_two_api_models_share_a_schema_name():
    from analystos.api.app import app

    names = app.openapi()["components"]["schemas"]
    qualified = sorted(n for n in names if n.startswith("analystos__"))
    assert not qualified, f"rename one of the colliding models: {qualified}"
