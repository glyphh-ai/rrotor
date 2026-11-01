# Apps: Claude's pages published to this rrotor, served to the org's people

**Status: approved (2026-11-01); rrotor's part built (branch `apps`): [`architecture.md`, 5.8](../architecture.md#58-apps).** The spec is the server's
[`docs/specs/artifacts.md`](https://github.com/glyphh-ai/server/blob/main/docs/specs/artifacts.md) (section 4 is
rrotor's part); this file is its pointer so the rrotor repo carries the contract too.

An **app** is a program of `"kind": "app"`: a page Claude wrote (a Claude Code artifact, `.html` or `.md`) and its
files under `src/`, a manifest with `app: {entry, title, roles, functions}`, and optionally `program.py` functions
the page calls. It is published from Claude with `app_publish` (`POST /v1/apps`), reviewed in the Glyphh catalogue
(the server's, told down the link as `artifact` messages and the welcome's `artifacts`), and served at
`https://<host>/apps/<name>/` to a browser signed in to Glyphh for this rrotor (rrotor as an OAuth client of the
Glyphh server, a session cookie, the sign-in introspected like a bearer) whose role or groups the app's `roles`
allow. The page's calls (`POST /apps/<name>/call/<function>`) run as the viewer: their HOME, their CLIs. A local
rrotor serves its apps to the browser on its own machine without a sign-in; one with the link off treats every
deploy as approved.
