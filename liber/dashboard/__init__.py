"""Dashboard: FastAPI + Jinja + HTMX, served by the proxy's own app at /liber/ (ARCHITECTURE.md §10, D11).

routes.py serves the pages and actions (every action calls plan/commands.py, the code behind
`liber plan`); views.py works out what they show; templates/ lays it out; static/ holds HTMX,
shipped with Liber so the page loads nothing from the internet (docs/decisions/0014).
"""
