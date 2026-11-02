/* rrotor.js: what an app's page needs of its rrotor (docs/architecture.md, 5.8). Served at /apps/_/rrotor.js on the
   app's own origin, within its Content-Security-Policy; plain JavaScript, no build.

     <script src="/apps/_/rrotor.js"></script>
     await rrotor.ready();                         // each CLI the app names signed in for this viewer (a card asks)
     const rows = await rrotor.call("failures");   // a function's `done` data; a wait or a failure throws

   rrotor.call(fn, inputs) answers a done Result's data; a needs_input or needs_approval throws an error with
   `runId`, `step` and `prompt` (answer it with rrotor.continue(runId, answer), also rrotor.resume); a failed one
   throws an error with `code` and `reason`. rrotor.clis() lists the app's CLIs as this viewer stands.
   rrotor.ready(names) resolves once each named CLI (every one of the app's by default) is signed in, showing
   rrotor's sign-in card (in a shadow root: the page's styles are untouched) for each that is not. rrotor.app is a
   promise of {name, title, type, run_as, viewer}. The app's name is the page's path, /apps/<name>/. */
(function () {
  "use strict";
  var match = /^\/apps\/([a-z][a-z0-9_]{0,62})\//.exec(window.location.pathname);
  var name = match ? match[1] : null;
  var base = "/apps/" + name;
  var POLL_MS = 2000;

  function RrotorError(message, extra) {
    var err = new Error(message);
    err.name = "RrotorError";
    Object.assign(err, extra || {});
    return err;
  }

  function api(method, path, body) {
    var init = { method: method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    return fetch(base + path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var error = (data && data.error) || {};
          throw RrotorError(error.message || ("rrotor answered " + res.status), {
            code: error.code || "E_HTTP",
            status: res.status
          });
        }
        return data;
      });
    });
  }

  function outcome(result) {
    if (result.status === "done") return result.data;
    if (result.status === "needs_input" || result.status === "needs_approval") {
      throw RrotorError(result.prompt || ("The run is waiting: " + result.status), {
        code: result.status === "needs_input" ? "E_NEEDS_INPUT" : "E_NEEDS_APPROVAL",
        status: result.status,
        runId: result.run_id,
        step: result.step,
        prompt: result.prompt,
        data: result.data
      });
    }
    var data = result.data || {};
    throw RrotorError(data.reason || ("The run " + result.status), {
      code: data.code || "E_RUN_" + String(result.status).toUpperCase(),
      status: result.status,
      runId: result.run_id,
      step: result.step,
      data: data
    });
  }

  function call(fn, inputs) {
    return api("POST", "/call/" + encodeURIComponent(fn), inputs || {}).then(outcome);
  }

  function resume(runId, answer) {
    return api("POST", "/runs/" + encodeURIComponent(runId) + "/continue", { answer: answer }).then(outcome);
  }

  function clis() {
    return api("GET", "/clis").then(function (data) { return data.clis; });
  }

  var appPromise = null;
  function app() {
    if (appPromise === null) appPromise = api("GET", "/me");
    return appPromise;
  }

  /* The sign-in card: one fixed panel, bottom right, in a shadow root; one CLI at a time. */
  var CARD_CSS =
    ":host{all:initial;position:fixed;right:1rem;bottom:1rem;z-index:2147483647;font-family:system-ui,sans-serif;" +
    "font-size:14px;color:#1a1a1a}" +
    ".card{background:#fff;border:1px solid #d0d0d0;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.14);" +
    "padding:1rem 1.1rem;width:min(22rem,calc(100vw - 2rem));line-height:1.45}" +
    "h2{margin:0 0 .5rem;font-size:15px;font-weight:600}" +
    "p{margin:.4rem 0}" +
    "a{color:#0a58ca;word-break:break-all}" +
    ".code{font-family:ui-monospace,monospace;font-size:26px;letter-spacing:.08em;margin:.6rem 0 .3rem;" +
    "display:flex;align-items:center;gap:.6rem}" +
    "button{font:inherit;font-size:13px;padding:.25rem .6rem;border:1px solid #bbb;border-radius:6px;" +
    "background:#f6f6f6;cursor:pointer}" +
    "button:hover{background:#eee}" +
    "input{font:inherit;padding:.3rem .5rem;border:1px solid #bbb;border-radius:6px;width:100%;box-sizing:border-box}" +
    ".row{display:flex;gap:.5rem;margin-top:.4rem}" +
    ".status{color:#555}.done{color:#1a7f37;font-weight:600}.error{color:#b42318}";

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function Card(cli) {
    this.host = el("div", { "data-rrotor": "sign-in" });
    this.root = this.host.attachShadow({ mode: "open" });
    this.root.appendChild(el("style", {}, CARD_CSS));
    this.box = el("div", { class: "card", role: "dialog", "aria-live": "polite" });
    this.root.appendChild(this.box);
    this.box.appendChild(el("h2", {}, "Sign in to " + cli.title + " to continue"));
    this.body = el("div");
    this.box.appendChild(this.body);
    this.status = el("p", { class: "status" }, "Starting the sign-in…");
    this.box.appendChild(this.status);
    document.body.appendChild(this.host);
  }

  Card.prototype.show = function (data, onCode) {
    this.body.textContent = "";
    if (data.url) {
      var p = el("p", {}, "Open ");
      p.appendChild(el("a", { href: data.url, target: "_blank", rel: "noopener" }, data.url));
      p.appendChild(document.createTextNode(" and sign in."));
      this.body.appendChild(p);
    }
    if (data.code) {
      var line = el("div", { class: "code" });
      line.appendChild(el("span", {}, data.code));
      var copy = el("button", { type: "button" }, "Copy");
      copy.addEventListener("click", function () {
        var done = function () { copy.textContent = "Copied"; };
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(data.code).then(done, done);
        else done();
      });
      line.appendChild(copy);
      this.body.appendChild(line);
      this.body.appendChild(el("p", {}, "Enter this code on the page."));
    } else if (data.typed !== false) {
      var row = el("div", { class: "row" });
      var input = el("input", { type: "text", placeholder: "The code the page shows", "aria-label": "Code" });
      var send = el("button", { type: "button" }, "Continue");
      var submit = function () { if (input.value.trim()) onCode(input.value.trim()); };
      send.addEventListener("click", submit);
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
      row.appendChild(input);
      row.appendChild(send);
      this.body.appendChild(row);
    }
    this.say("Waiting…");
  };

  Card.prototype.say = function (text, cls) {
    this.status.textContent = text;
    this.status.className = cls || "status";
  };

  Card.prototype.remove = function () {
    if (this.host.parentNode) this.host.parentNode.removeChild(this.host);
  };

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /* One CLI's sign-in: the run started, its address and code shown, the run polled every two seconds, a code
     posted back when the CLI asks for one, "Signed in" then the card gone. */
  function signIn(cli) {
    var card = new Card(cli);
    var runId = null;
    var shown = null; // the last needs_input shown or answered, so the same ask is not answered twice
    var finished = false;
    var failure = null;

    function fail(err) {
      if (finished) return;
      finished = true;
      failure = err;
      card.say(err.message || "The sign-in failed.", "error");
      setTimeout(function () { card.remove(); }, 8000);
    }

    function post(answer) {
      // A continue waits while the CLI finishes signing in (the person is on the page meanwhile); the poll below
      // shows where the run stands. A run no longer waiting (E_RUN_STATE) was moved on by the poll already.
      api("POST", "/runs/" + encodeURIComponent(runId) + "/continue", { answer: answer }).then(take, function (err) {
        if (err.code !== "E_RUN_STATE") fail(err);
      });
    }

    function take(result) {
      if (finished) return;
      if (result.status === "done") {
        finished = true;
        card.say("Signed in", "done");
        setTimeout(function () { card.remove(); }, 1500);
        return;
      }
      if (result.status === "needs_input") {
        var data = result.data || {};
        var key = JSON.stringify([data.url, data.code, data.output]);
        if (key !== shown) {
          shown = key;
          card.show(data, post);
          if (data.code || data.typed === false) post({ code: data.code || "done" }); // the CLI finds out itself
        }
        return;
      }
      if (result.status === "needs_approval") return; // not a sign-in's: left to the page
      fail(RrotorError((result.data && result.data.reason) || ("The sign-in " + result.status), {
        code: (result.data && result.data.code) || "E_SIGN_IN", status: result.status
      }));
    }

    return api("POST", "/clis/" + encodeURIComponent(cli.name) + "/login").then(function (first) {
      runId = first.run_id;
      return new Promise(function (resolve, reject) {
        var step = function (result) {
          take(result);
          if (finished) {
            if (failure) reject(failure); else resolve(true);
            return;
          }
          sleep(POLL_MS).then(function () {
            return api("GET", "/runs/" + encodeURIComponent(runId));
          }).then(step, function (err) { fail(err); reject(err); });
        };
        step(first);
      });
    }, function (err) { fail(err); throw err; });
  }

  function ready(names) {
    return app().then(function (me) {
      if (me.run_as === "app") return true;
      return clis().then(function (list) {
        var wanted = names ? list.filter(function (c) { return names.indexOf(c.name) !== -1; }) : list;
        var todo = wanted.filter(function (c) { return c.login && c.signed_in === false; });
        return todo.reduce(function (chain, cli) {
          return chain.then(function () { return signIn(cli); });
        }, Promise.resolve()).then(function () { return true; });
      });
    });
  }

  var rrotor = { call: call, continue: resume, resume: resume, clis: clis, ready: ready, name: name };
  Object.defineProperty(rrotor, "app", { get: app, enumerable: true });
  window.rrotor = rrotor;
})();
