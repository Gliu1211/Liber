# 0014 — Dashboard: served at /liber/, actions through the CLI's plan code, local-only guards

- Status: Proposed
- Date: 2026-11-07

## Context
Session 6 builds ARCHITECTURE.md §10's dashboard with D11's stack (FastAPI + Jinja + HTMX), bound
to 127.0.0.1. It covers:
- the session list with branches;
- the item table (role, kind, tokens, topic, status);
- the topic map with token totals;
- offload, summarize and delete into the pending plan, with the cost and payback preview;
- Apply and Undo;
- a per-session chart of input tokens, cache reads and cache writes.

Acceptance requires route tests for every action, and Apply must go through the same plan code
as the CLI.

Three things weren't settled by D11:
- where the pages live, given that the proxy forwards every path it doesn't own;
- how a page on 127.0.0.1 is kept from being driven by other websites in the same browser;
- where HTMX comes from, given §11's "nothing is sent anywhere except the upstream provider".

## Options considered
1. **A second server on another port.** This means two listeners and more configuration, and it
   still needs the same browser guards.
2. **The proxy's app, under a path the API never uses (chosen).** One process, one port: the
   store, settings and worker state are already in `app.state`.
3. **HTMX from a CDN.** The browser would contact a third party every time the page opens, which
   §11 rules out.

## Decision
1. **Routes under `/liber/`** (`dashboard/routes.py`), registered before the proxy's catch-all.
   Anthropic's API has no `/liber` path, and every other path is still forwarded. The router has
   its own catch-all for every other `/liber/` path and method (404). Without it, a wrong method,
   a CORS preflight or a typo would fall through to the proxy and be forwarded upstream. `liber
   serve` logs the dashboard's URL.
2. **No duplicate logic.**
   - Every action calls `plan/commands.create|apply|undo`, so the dashboard gets the same checks
     as `liber plan`: summaries must be ready, the plan must make requests smaller, and
     summaries must end on exchange boundaries.
   - What `show` printed is now `commands.SessionView`, which both use.
   - The cost preview is `commands.describe_estimate`.
   - Where a new summary may start is `commands.live_start`.
   - A refusal is shown in the page, not raised as an error.
3. **Local-only guards.**
   - **Every route requires a loopback Host** (`127.0.0.1`, `localhost`, `[::1]`). A site that
     points its own name at 127.0.0.1 (DNS rebinding) is refused, so it can't read the pages.
   - **Every action requires the `HX-Request` header,** which HTMX adds. Another site's form can't
     set a header, and its script can only set one after a CORS check that Liber never grants. If
     the browser sends `Origin`, it must be this page.
   - **No other site may frame the pages** (`X-Frame-Options: DENY`, CSP `frame-ancestors
     'none'`). An invisible frame would otherwise let a site trick a click on Apply or Undo, with
     every header above in place. The CSP (`default-src 'self'`) also limits the page to Liber's
     own script.
   - Actions don't work without JavaScript; that's accepted.
   - **Thinking blocks are refused when a plan is created** (`commands.create_now`), not only
     greyed out in the page. A crafted request, or the CLI, would otherwise create a version the
     request path can never use (§5, hard rule 6).
4. **HTMX 2.0.4 is shipped in the repo** (`dashboard/static/htmx.min.js`, BSD Zero Clause
   licence, taken from the npm registry tarball, sha512 integrity checked) and served by Liber.
   The pages load nothing else. Their CSS is inline, and the chart is an SVG drawn on the server
   (`views.usage_chart`), with no chart library.
5. **Topics are per exchange** (ADR 0013): every item shows its exchange's topic, "not summarized
   yet", or "current turn". The topic map has one row per completed exchange plus the turn in
   progress. Token counts are the worker's exact ones where it has them (a `~` marks estimates).
6. **Escaping.** Jinja's autoescaping is on. Topics are model-written from conversation text, and
   offload notes are user-typed, so both are shown as text, never as markup. The database holds
   no conversation content, so the item table shows none.

## Consequences
- **Jinja2 is a new dependency** (with MarkupSafe). Forms are read with `urllib.parse.parse_qs`
  rather than `python-multipart`.
- **The proxy now answers some paths itself.** That's only `/liber/...`; ADR 0001's "relay
  everything" still holds for every API path.
- **Messages read like the CLI's** (e.g. "To use it: liber plan apply…"), because they come from
  the same functions.
- **Not in v1:** selecting a summary range other than "from the live start through message N"
  (§5 only allows that shape); a view of offloaded originals, or restoring them; per-request
  details. These are candidates for the owner to decide.
- **Revisit when** the dashboard needs live updates (HTMX polling would do) or a separate port.
