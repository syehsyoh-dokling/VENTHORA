"""Endpoints by domain, one module each.

api.py grew to hold every endpoint. New endpoints are written here as FastAPI routers, and the existing ones move here
domain by domain (payments, vendors, budgets, reporting, platform), each move covered by the suite. A router reads the
running service from ``request.app.state`` (the stores, the executor, the audit chain); it never imports api.py.
"""
