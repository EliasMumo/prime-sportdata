"""HTTP server for prime-sportdata (SPEC architecture ``server/`` node).

``app.py`` holds the FastAPI factory (``create_app(engine)``, dependency
injection for TestClient tests) and the module-level ``app`` uvicorn target;
``routes.py`` holds /health, /catalog and the /v1/{sport}/{category} handlers.
"""
