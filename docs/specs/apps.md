# Apps: what Claude made, published to this rrotor, served to the org's people

**Status: built, rrotor 0.3.0 (branch `apps-types`, 2026-11-02): [`architecture.md`, 5.8](../architecture.md#58-apps).**
The spec is the server's [`docs/specs/apps.md`](https://github.com/glyphh-ai/server/blob/main/docs/specs/apps.md)
(section 4 is rrotor's part); this file is its pointer so the rrotor repo carries the contract too. One word
everywhere: app. "Artifact" is Claude's word for the file in the session, and names nothing a person reads or
calls here (0.2.0's `artifacts.py`, `/data/artifacts.json` and the link's `artifact` names are gone; the link still
reads the old names, 3.4 of the server's spec).

An **app** is a program of `"kind": "app"`: what Claude made (a Claude Code artifact, `.html` or `.md`; a React
page, `.jsx` or `.tsx`; a Markdown document; a diagram, `.mmd` or `.svg`; a deck or a design exported as `.pdf` or
an image) and its files under `src/`, a manifest with `app: {entry, type, title, roles, functions}`, and optionally
`program.py` functions the page calls. Its `type` is one of `page`, `dashboard`, `doc`, `diagram`, `deck`,
`design`, by the entry's extension unless the publisher says one (`dashboard` and `deck` are never inferred). It is
published from Claude with `app_publish` (`POST /v1/apps`), reviewed in the Glyphh catalogue (the server's, told
down the link as `app` messages and the welcome's `apps`, protocol 3, kept in `/data/apps.json`), and served at
`https://<host>/apps/<name>/` to a browser signed in to Glyphh for this rrotor (rrotor as an OAuth client of the
Glyphh server, a session cookie, the sign-in introspected like a bearer) whose role or groups the app's `roles`
allow: a page as it is, a `.md` in the document shell, a `.jsx` or `.tsx` in the React shell (an import map, Babel
in the browser, nothing built), a `.mmd` in the diagram shell, an image in the image shell, a PDF or an SVG as it
is. The page's calls (`POST /apps/<name>/call/<function>`) run as the viewer: their HOME, their CLIs. A local rrotor
serves its apps to the browser on its own machine without a sign-in; one with the link off treats every deploy as
approved.
