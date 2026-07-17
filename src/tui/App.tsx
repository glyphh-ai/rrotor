/**
 * App.tsx — the rrotor TUI root: a CHAT VIEWPORT.
 *
 * Fixed full-screen layout in the alternate buffer: Header locked at the top,
 * the strip (SubFooter · composer · Footer) locked at the bottom, and ONLY the
 * middle transcript scrolls — the app owns scrolling (wheel via the terminal's
 * alternate-scroll arrows, plus PgUp/PgDn · ctrl+u/ctrl+d), not the terminal.
 * Items flatten to a LINE MODEL (lines.ts), so the visible window is an exact
 * slice, follow-the-tail is offset 0, and ctrl+o re-expansion is retroactive.
 * On exit the full transcript prints into the NORMAL buffer, so the session
 * lives on in real shell scrollback. The root height stays under the terminal
 * rows — Ink's clearTerminal path (whose \x1b[3J erases scrollback) can never
 * fire. Every transcript pixel derives from wire events — the TUI is a pure
 * stream consumer, same as any SDK client.
 */

import type { ReactElement } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, Text, measureElement, useApp, useInput, useStdout, type DOMElement } from "ink";
import { openChat, type ChatSession } from "../chat.js";
import type { WireEvent } from "../transport/events.js";
import { theme } from "./theme.js";
import { Header } from "./Header.js";
import { InputBar } from "./InputBar.js";
import { SubFooter } from "./SubFooter.js";
import { Footer } from "./Footer.js";
import { userLines, answerLines, sectionLines, noteLines, cardLines, type Line } from "./lines.js";
import { describeStore, writePrefs, readPrefs } from "./prefs.js";
import { loadedEnvFiles } from "../env.js";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { homedir } from "node:os";
import type { ChatTurn, TurnStep } from "./types.js";

/** The sub-footer's working verbs — one pair per turn, cycled deterministically. */
const VERBS: ReadonlyArray<{ ing: string; past: string }> = [
  { ing: "Rotoring", past: "Rotored" },
  { ing: "Reasoning", past: "Reasoned" },
  { ing: "Spinning", past: "Spun" },
  { ing: "Crunching", past: "Crunched" },
  { ing: "Composing", past: "Composed" },
  { ing: "Brewing", past: "Brewed" },
];

/** Omit that distributes over a union (plain Omit collapses discriminants). */
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

/** One transcript item (flattened to lines at render time). */
type Item =
  | { id: string; kind: "user"; text: string }
  | { id: string; kind: "section"; step: TurnStep }
  | { id: string; kind: "answer"; text: string }
  | { id: string; kind: "note"; text: string }
  | { id: string; kind: "card"; title: string; body: string[] };

/** The rotors shift+tab cycles through — the chat-drivable set. */
const ROTOR_CYCLE = ["router", "base-memory", "code", "base-single"];

/** Provider shorthands: `/model coder anthropic <model> <key>` → hosted URL. */
const PROVIDER_URLS: Record<string, { url: string; provider: "openai" | "anthropic" }> = {
  anthropic: { url: "https://api.anthropic.com", provider: "anthropic" },
  claude: { url: "https://api.anthropic.com", provider: "anthropic" },
  openai: { url: "https://api.openai.com", provider: "openai" },
};

/** The model registry a control plane would inject — built from per-role prefs. */
function modelsFromPrefs(): { registry: Record<string, { url: string; model?: string; headers?: Record<string, string>; provider?: "openai" | "anthropic" }> } | undefined {
  const roles = readPrefs().roles;
  if (!roles || Object.keys(roles).length === 0) return undefined;
  const registry: Record<string, { url: string; model?: string; headers?: Record<string, string>; provider?: "openai" | "anthropic" }> = {};
  for (const [role, b] of Object.entries(roles)) {
    const provider = b.provider ?? (b.url.includes("anthropic.com") ? "anthropic" : "openai");
    // Stored key first; else the provider's conventional env key (.env-able).
    const key = b.key ?? (provider === "anthropic" ? process.env.ANTHROPIC_API_KEY : process.env.OPENAI_API_KEY);
    registry[role] = {
      url: b.url,
      provider,
      ...(b.model ? { model: b.model } : {}),
      ...(key
        ? { headers: provider === "anthropic" ? { "x-api-key": key, "anthropic-version": "2023-06-01" } : { authorization: `Bearer ${key}` } }
        : {}),
    };
  }
  return { registry };
}

/** The rotor-instructions card body: what this rotor is and what it needs. */
function rotorCard(session: ChatSession): { title: string; body: string[] } {
  const m = session.manifest;
  const live = Boolean(process.env.ROTOR_MODEL_URL);
  const frontier = Boolean(process.env.ROTOR_FRONTIER_URL);
  const roleBindings = readPrefs().roles ?? {};
  const body: string[] = [];
  if (m.description) body.push(m.description);
  body.push("");
  body.push(`mode ${m.mode} — ${m.mode === "code" ? "full workbench (files + shell)" : m.mode === "cowork" ? "docs + artifacts, no shell" : "read-only tools"}`);
  if (m.roles.length) {
    for (const r of m.roles) {
      const b = roleBindings[r.role];
      if (b) {
        body.push(`role ${r.role} → ${b.model ?? "default"} @ ${b.url}${b.key ? " (key)" : ""}`);
      } else {
        const fallback =
          r.lane === "frontier"
            ? frontier
              ? `frontier lane (${process.env.ROTOR_FRONTIER_URL})`
              : `frontier UNBOUND → degrades to ${live ? `local (${process.env.ROTOR_MODEL_URL})` : "stub"} — /model frontier <url> [key]`
            : live
              ? `${r.lane} lane (${process.env.ROTOR_MODEL_URL})`
              : "stub — /model local <url>";
        body.push(`role ${r.role} → ${fallback} · /model ${r.role} <url> [model]`);
      }
    }
  }
  if (m.tools.length) body.push(`tools ${m.tools.map((t) => t.name).join("  ")}`);
  body.push("");
  body.push(`just type — your line fills \`${session.inputs.primary}\`${session.inputs.extras.length ? `; also asks: ${session.inputs.extras.map((e) => e.name).join(", ")}` : ""}`);
  return { title: session.rotor, body };
}

/** Style one body line: `→` file paths, `$` commands, `-` plan bullets. */
function styleLine(l: string): string {
  const file = /^FILE:\s*(.+)$/.exec(l);
  if (file) return `→ ${file[1]}`;
  const test = /^TEST:\s*(.+)$/.exec(l);
  if (test) return `$ ${test[1]}`;
  if (/^PLAN:\s*$/.test(l)) return "plan:";
  const num = /^\s*\d+\.\s+(.+)$/.exec(l);
  if (num) return `- ${num[1]}`;
  return l;
}

const cap = (l: string): string => (l.length > 100 ? `${l.slice(0, 99)}…` : l);

/** Facts a step's wire output contributes beneath its row (bounded), rendered
 *  as human receipts: `→ path (N bytes)`, `→ exit 0 · PASS`, `$ command`. */
function stepDetails(output?: Record<string, unknown>): string[] {
  if (!output) return [];
  const lines: string[] = [];
  for (const key of Object.keys(output).sort()) {
    if (key === "usage") continue;
    const v = output[key];
    if (v === undefined || v === null) continue;
    if (typeof v === "object" && !Array.isArray(v)) {
      const o = v as Record<string, unknown>;
      if (typeof o.path === "string" && o.bytes_written !== undefined) {
        lines.push(`→ ${o.path} (${o.bytes_written} bytes)`);
        continue;
      }
      if (o.exit_code !== undefined) {
        const out = typeof o.stdout === "string" ? (o.stdout.split("\n").find((x) => x.trim()) ?? "") : "";
        lines.push(cap(`→ exit ${o.exit_code}${o.timed_out ? " · timed out" : ""}${out ? ` · ${out}` : ""}`));
        const errLine = typeof o.stderr === "string" ? (o.stderr.split("\n").find((x) => x.trim()) ?? "") : "";
        if (errLine) lines.push(cap(`✗ ${errLine}`));
        continue;
      }
    }
    if (typeof v === "string" && (key === "text" || v.includes("\n") || v.length > 100)) {
      const body = v.replace(/\n+$/, "").split("\n");
      for (const l of body.slice(0, 40)) lines.push(cap(styleLine(l)));
      if (body.length > 40) lines.push(`… (+${body.length - 40} more lines)`);
      continue;
    }
    if (key === "lane_notes" && Array.isArray(v)) {
      for (const n of v as string[]) lines.push(cap(`✗ ${n}`));
      continue;
    }
    if (key === "served" && typeof v === "string") {
      lines.push(`served ${v}`);
      continue;
    }
    if (typeof v === "string" && (key === "file" || key === "path")) {
      lines.push(cap(`→ ${v}`));
      continue;
    }
    if (typeof v === "string" && (key === "test_cmd" || key === "command" || key === "cmd")) {
      lines.push(cap(`$ ${v}`));
      continue;
    }
    const img = typeof v === "string" ? v : (JSON.stringify(v) ?? "");
    if (!img || img === "[]" || img === "{}") continue;
    lines.push(cap(`${key} ${img.length > 100 ? `${img.slice(0, 99)}…` : img}`));
  }
  return lines.slice(0, 60);
}

function TranscriptLine({ line }: { line: Line }): ReactElement {
  if (line.style === "blank") return <Text> </Text>;
  if (line.style === "headSel") {
    return (
      <Text wrap="truncate-end" backgroundColor={theme.accent} color="black" bold>
        {line.text}
      </Text>
    );
  }
  if (line.style === "head" || line.style === "headErr") {
    const glyph = line.text.slice(0, 2);
    return (
      <Text wrap="truncate-end">
        <Text color={line.style === "headErr" ? "red" : theme.accent}>{glyph}</Text>
        <Text color={theme.white} bold>
          {line.text.slice(2)}
        </Text>
      </Text>
    );
  }
  if (line.style === "answer" && line.text.trimStart().startsWith("●")) {
    const i = line.text.indexOf("●");
    return (
      <Text wrap="truncate-end">
        {line.text.slice(0, i)}
        <Text color={theme.accent}>● </Text>
        <Text color={theme.white}>{line.text.slice(i + 2)}</Text>
      </Text>
    );
  }
  if (line.style === "user" && line.text.startsWith("›")) {
    return (
      <Text wrap="truncate-end">
        <Text color={theme.accent}>› </Text>
        <Text color={theme.white}>{line.text.slice(2)}</Text>
      </Text>
    );
  }
  if (line.style === "detail" || line.style === "hint") {
    // White gutter rule, dim body — the line is chrome, the text is content.
    const i = line.text.indexOf("│");
    if (i >= 0) {
      return (
        <Text wrap="truncate-end" italic={line.style === "hint"}>
          <Text color={theme.line}>{line.text.slice(0, i + 1)}</Text>
          <Text color={theme.gray}>{line.text.slice(i + 1)}</Text>
        </Text>
      );
    }
  }
  if (line.style === "cardTitle") {
    const start = line.text.indexOf("─ ") + 2;
    const end = line.text.indexOf(" ─", start);
    return (
      <Text wrap="truncate-end">
        <Text color={theme.line}>{line.text.slice(0, start)}</Text>
        <Text color={theme.accent} bold>
          {line.text.slice(start, end)}
        </Text>
        <Text color={theme.line}>{line.text.slice(end)}</Text>
      </Text>
    );
  }
  if (line.style === "cardEdge") {
    return (
      <Text color={theme.line} wrap="truncate-end">
        {line.text}
      </Text>
    );
  }
  if (line.style === "cardBody") {
    const first = line.text.indexOf("│");
    const last = line.text.lastIndexOf("│");
    return (
      <Text wrap="truncate-end">
        <Text color={theme.line}>{line.text.slice(0, first + 1)}</Text>
        <Text color={theme.gray}>{line.text.slice(first + 1, last)}</Text>
        <Text color={theme.line}>{line.text.slice(last)}</Text>
      </Text>
    );
  }
  const color = line.style === "answer" || line.style === "user" ? theme.white : theme.dim;
  return (
    <Text color={color} wrap="truncate-end" italic={line.style === "hint" || line.style === "note"}>
      {line.text}
    </Text>
  );
}

export interface AppProps {
  version: string;
  rotorArg?: string;
  ws: string;
  /** Collector: the plain transcript, printed to the NORMAL buffer on exit. */
  transcript: string[];
}

export function App({ version, rotorArg, ws, transcript }: AppProps): ReactElement {
  const { exit } = useApp();
  const { stdout } = useStdout();
  const [cols, setCols] = useState(stdout.columns || 80);
  const [rows, setRows] = useState(stdout.rows || 24);
  const [chat, setChat] = useState<ChatSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [totals, setTotals] = useState({ up: 0, down: 0 });
  const [last, setLast] = useState<ChatTurn | undefined>(undefined);
  const [verb, setVerb] = useState<{ ing: string; past: string }>(VERBS[0]);
  const [elapsed, setElapsed] = useState(0);
  const [expanded, setExpanded] = useState(false);
  /** Per-section fold overrides (item id → open?). Falls back to `expanded`. */
  const [folds, setFolds] = useState<Record<string, boolean>>({});
  /** `compose` types into the input; `browse` walks section heads (tab flips). */
  const [uiMode, setUiMode] = useState<"compose" | "browse">("compose");
  const [selId, setSelId] = useState<string | null>(null);
  /** Lines scrolled up from the tail; 0 = follow mode. */
  const [scroll, setScroll] = useState(0);
  const [chromeH, setChromeH] = useState({ header: 12, strip: 6 });
  const EMPTY_MEM = { short: { count: 0, bytes: 0 }, mid: { count: 0, bytes: 0 }, long: { count: 0, bytes: 0 } };
  const [memory, setMemory] = useState(EMPTY_MEM);
  const memoryPrev = useRef(EMPTY_MEM);
  const [memoryHot, setMemoryHot] = useState({ short: false, mid: false, long: false });

  const nextId = useRef(0);
  const lastStepAt = useRef(0);
  const turnStartedAt = useRef(0);
  const chatRef = useRef<ChatSession | null>(null);
  const currentRotor = useRef<string | undefined>(undefined);
  const pending = useRef<string[]>([]);
  const inputRef = useRef("");
  const runTurnRef = useRef<((prompt: string) => Promise<void>) | null>(null);
  const headerBox = useRef<DOMElement | null>(null);
  const stripBox = useRef<DOMElement | null>(null);

  useEffect(() => {
    const onResize = () => {
      setCols(stdout.columns || 80);
      setRows(stdout.rows || 24);
    };
    stdout.on("resize", onResize);
    return () => {
      stdout.off("resize", onResize);
    };
  }, [stdout]);

  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => setElapsed((Date.now() - turnStartedAt.current) / 1000), 250);
    return () => clearInterval(t);
  }, [busy]);

  // Memory gauges: poll the stator's tier counts — fast while a turn writes,
  // slow when idle. A tier that grew renders HOT briefly.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const session = chatRef.current;
      if (!session) return;
      try {
        const stats = await session.memoryStats();
        if (!alive) return;
        const prev = memoryPrev.current;
        const hot = {
          short: stats.short.bytes > prev.short.bytes,
          mid: stats.mid.bytes > prev.mid.bytes,
          long: stats.long.bytes > prev.long.bytes,
        };
        memoryPrev.current = stats;
        setMemory(stats);
        if (hot.short || hot.mid || hot.long) {
          setMemoryHot(hot);
          setTimeout(() => alive && setMemoryHot({ short: false, mid: false, long: false }), 1200);
        }
      } catch {
        /* stats are cosmetic — never break the app */
      }
    };
    const t = setInterval(tick, busy ? 500 : 3000);
    void tick();
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [busy, chat]);

  // Real chrome heights each frame — the viewport height is exact, not guessed.
  useEffect(() => {
    const header = headerBox.current ? measureElement(headerBox.current).height : chromeH.header;
    const strip = stripBox.current ? measureElement(stripBox.current).height : chromeH.strip;
    if (header !== chromeH.header || strip !== chromeH.strip) setChromeH({ header, strip });
  });

  const push = useCallback((item: DistributiveOmit<Item, "id">) => {
    setItems((l) => [...l, { ...item, id: `i${++nextId.current}` } as Item]);
  }, []);

  const editInput = useCallback((fn: (v: string) => string) => {
    inputRef.current = fn(inputRef.current);
    setInput(inputRef.current);
  }, []);

  const openRotor = useCallback(
    async (name?: string) => {
      setError(null);
      currentRotor.current = name ?? currentRotor.current ?? "router";
      try {
        const session = await openChat(name ?? currentRotor.current, { models: modelsFromPrefs() });
        void chatRef.current?.close().catch(() => {});
        chatRef.current = session;
        setChat(session);
        const card = rotorCard(session);
        push({ kind: "card", title: card.title, body: card.body });
        const queued = pending.current;
        pending.current = [];
        for (const line of queued) void runTurnRef.current?.(line);
      } catch (err) {
        setError((err as Error).message);
      }
    },
    [],
  );

  useEffect(() => {
    push({
      kind: "card",
      title: "Welcome",
      body: [
        "rrotor — Recursive Reasoning on top of reasoning.",
        "Every line you type is one full rotor turn: deterministic steps, streamed live, permanently on the record.",
        loadedEnvFiles.length
          ? `env loaded: ${loadedEnvFiles.join(" · ")}`
          : "no .env found (looked in ./ and ~/.rrotor/) — see .env.example",
        "shift+tab cycles rotors · tab browses/folds sections · /store · /model · /quit",
      ],
    });
    void openRotor(rotorArg);
    // Mount-only by design: the rotor argument is fixed for the process.
  }, []);

  const runTurn = useCallback(
    async (prompt: string) => {
      const session = chatRef.current;
      if (!session) {
        pending.current.push(prompt);
        return;
      }
      const startedAt = Date.now();
      turnStartedAt.current = startedAt;
      setElapsed(0);
      setVerb(VERBS[nextId.current % VERBS.length]);
      lastStepAt.current = startedAt;
      push({ kind: "user", text: prompt });
      const turn: ChatTurn = {
        id: `t${nextId.current}`,
        role: "assistant",
        text: "",
        steps: [],
        status: "running",
        tokensUp: 0,
        tokensDown: 0,
        stubbed: false,
        degraded: false,
      };
      setLast({ ...turn });
      setBusy(true);
      try {
        await session.turnEvents(prompt, (ev: WireEvent) => {
          if (ev.kind === "step") {
            const now = Date.now();
            const secs = (now - lastStepAt.current) / 1000;
            lastStepAt.current = now;
            const up = ev.usage?.input ?? 0;
            const down = ev.usage?.output ?? 0;
            if (up + down > 0) setTotals((t) => ({ up: t.up + up, down: t.down + down }));
            const step: TurnStep = {
              label: ev.display?.label ?? ev.step_id,
              ok: !ev.error && ev.status !== "failed",
              ...(secs >= 0.1 ? { secs } : {}),
              ...(up + down > 0 ? { tokens: { up, down } } : {}),
              details: stepDetails(ev.output),
              ...(ev.error ? { error: ev.error } : {}),
            };
            turn.tokensUp += up;
            turn.tokensDown += down;
            turn.stubbed = turn.stubbed || ev.frames.includes("stub");
            turn.degraded = turn.degraded || ev.frames.includes("degrade");
            setLast({ ...turn });
            push({ kind: "section", step });
          } else if (ev.kind === "answer") {
            turn.text = ev.text;
            if (ev.text) push({ kind: "answer", text: ev.text });
          } else if (ev.kind === "error") {
            turn.text = `${ev.code}: ${ev.detail}`;
            turn.status = "failed";
            push({ kind: "answer", text: turn.text });
          } else if (ev.kind === "done") {
            if (turn.status !== "failed") {
              turn.status = ev.status === "ok" ? "done" : ev.status === "refused" ? "refused" : "failed";
            }
            turn.durationMs = Date.now() - startedAt;
            setLast({ ...turn });
          }
        });
      } catch (err) {
        turn.status = "failed";
        turn.text = (err as Error).message;
        turn.durationMs = Date.now() - startedAt;
        push({ kind: "answer", text: turn.text });
        setLast({ ...turn });
      } finally {
        setBusy(false);
      }
    },
    [push],
  );
  runTurnRef.current = runTurn;

  const submit = useCallback(() => {
    const line = inputRef.current.trim();
    if (!line || busy) return;
    editInput(() => "");
    if (line === "/quit" || line === "/exit") {
      void chatRef.current?.close().catch(() => {});
      exit();
      return;
    }
    if (line.startsWith("/rotor")) {
      const name = line.split(/\s+/)[1];
      push({ kind: "note", text: `switching rotor → ${name ?? "router"}` });
      void openRotor(name);
      return;
    }
    if (line.startsWith("/model")) {
      const [, target, url, modelId, key] = line.split(/\s+/);
      if (!target) {
        const roles = readPrefs().roles ?? {};
        const roleDesc = Object.entries(roles).map(([r, b]) => `${r}→${b.model ?? "default"}@${b.url}`);
        push({
          kind: "note",
          text: `local ${process.env.ROTOR_MODEL_URL ?? "stub"} · frontier ${process.env.ROTOR_FRONTIER_URL ?? "unbound"}${process.env.ROTOR_FRONTIER_KEY ? " (key)" : ""}${roleDesc.length ? ` · roles ${roleDesc.join(" ")}` : ""}`,
        });
        push({ kind: "note", text: "usage: /model local <url> · /model frontier <url> [key] · /model <role> <url> [model] [key] · /model <role> clear" });
        return;
      }
      if (target === "local" && url) {
        process.env.ROTOR_MODEL_URL = url;
        writePrefs({ modelUrl: url });
        push({ kind: "note", text: `local model → ${url}` });
      } else if (target === "frontier" && url) {
        process.env.ROTOR_FRONTIER_URL = url;
        writePrefs({ frontierUrl: url });
        if (modelId) {
          process.env.ROTOR_FRONTIER_KEY = modelId;
          writePrefs({ frontierKey: modelId });
        }
        push({ kind: "note", text: `frontier → ${url}${modelId ? " (key stored)" : ""}` });
      } else if (url === "clear") {
        const roles = { ...(readPrefs().roles ?? {}) };
        delete roles[target];
        writePrefs({ roles });
        push({ kind: "note", text: `role ${target} → unbound (lane fallback)` });
      } else if (url === undefined && (readPrefs().roles ?? {})[target]) {
        // Bare `/model <role>` INSPECTS — it must never mutate.
        const b = (readPrefs().roles ?? {})[target];
        push({ kind: "note", text: `role ${target} → ${b.model ?? "default"} @ ${b.url}${b.key ? " (key)" : ""} · /model ${target} clear to unbind` });
        return;
      } else if (url) {
        // Per-role binding: the registry entry a control plane would inject.
        // `url` may be a provider shorthand (anthropic/claude/openai).
        const shorthand = PROVIDER_URLS[url.toLowerCase()];
        const endpoint = shorthand?.url ?? url;
        const provider = shorthand?.provider;
        const roles = { ...(readPrefs().roles ?? {}) };
        roles[target] = {
          url: endpoint,
          ...(provider ? { provider } : {}),
          ...(modelId ? { model: modelId } : {}),
          ...(key ? { key } : {}),
        };
        writePrefs({ roles });
        push({ kind: "note", text: `role ${target} → ${modelId ?? "default"} @ ${endpoint}${key ? " (key stored)" : ""}` });
      } else {
        push({ kind: "note", text: "usage: /model local <url> · /model frontier <url> [key] · /model <role> <url|anthropic|openai> [model] [key] · /model <role> clear" });
        return;
      }
      void openRotor(undefined);
      return;
    }
    if (line.startsWith("/store")) {
      const [, kind, rawPath] = line.split(/\s+/);
      if (!kind) {
        push({ kind: "note", text: `store: ${describeStore()} · /store sqlite [path] · /store memory` });
        return;
      }
      if (kind === "memory") {
        delete process.env.ROTOR_STATOR_BACKEND;
        delete process.env.ROTOR_STATOR_URL;
        writePrefs({ statorBackend: undefined, statorUrl: undefined });
      } else if (kind === "sqlite") {
        const path = resolve((rawPath ?? "~/.rrotor/stator.db").replace(/^~/, homedir()));
        try {
          mkdirSync(dirname(path), { recursive: true });
        } catch {
          /* surfaced by the stator open below */
        }
        process.env.ROTOR_STATOR_BACKEND = "sqlite";
        process.env.ROTOR_STATOR_URL = path;
        writePrefs({ statorBackend: "sqlite", statorUrl: path });
      } else {
        push({ kind: "note", text: `unknown store '${kind}' — sqlite | memory` });
        return;
      }
      push({ kind: "note", text: `store → ${describeStore()} (new session; durable facts persist, the conversation window restarts)` });
      void openRotor(undefined);
      return;
    }
    void runTurn(line);
  }, [busy, exit, openRotor, runTurn, editInput, push]);

  // The line model — regenerated from items, so folding is retroactive.
  // Anchors record each section head's id + line index for browse/scroll.
  const width = Math.max(20, cols - 4);
  const { lines, anchors } = useMemo(() => {
    const out: Line[] = [];
    const anchors: Array<{ id: string; line: number; foldable: boolean }> = [];
    for (const item of items) {
      if (item.kind === "user") out.push(...userLines(item.text, width));
      else if (item.kind === "section") {
        anchors.push({ id: item.id, line: out.length, foldable: item.step.details.length > 3 });
        const open = folds[item.id] ?? expanded;
        out.push(...sectionLines(item.step, open, uiMode === "browse" && selId === item.id));
      } else if (item.kind === "answer") out.push(...answerLines(item.text, width));
      else if (item.kind === "card") out.push(...cardLines(item.title, item.body, width));
      else out.push(...noteLines(item.text));
    }
    return { lines: out, anchors };
  }, [items, expanded, folds, width, uiMode, selId]);

  // Keep the exit transcript current (plain text, colors stripped by content).
  useEffect(() => {
    transcript.length = 0;
    for (const l of lines) transcript.push(l.text);
  }, [lines, transcript]);

  const paneH = Math.max(3, rows - 2 - chromeH.header - chromeH.strip);
  const maxScroll = Math.max(0, lines.length - paneH);
  const clampedScroll = Math.min(scroll, maxScroll);
  const start = Math.max(0, lines.length - paneH - clampedScroll);
  const visible = lines.slice(start, start + paneH);

  const scrollBy = useCallback(
    (d: number) => setScroll((s) => Math.max(0, Math.min(Math.max(0, lines.length - paneH), s + d))),
    [lines.length, paneH],
  );

  /** Scroll so a line index sits inside the viewport (either direction). */
  const ensureVisible = useCallback(
    (line: number) => {
      setScroll((s) => {
        const maxS = Math.max(0, lines.length - paneH);
        const cur = Math.min(s, maxS);
        const start = Math.max(0, lines.length - paneH - cur);
        if (line < start) return Math.min(maxS, lines.length - paneH - line);
        if (line >= start + paneH) return Math.max(0, lines.length - paneH - (line - paneH + 1));
        return cur;
      });
    },
    [lines.length, paneH],
  );

  // Folding changes geometry AFTER the keypress — re-anchor the selection with
  // fresh line indices (setScroll bails when unchanged, so no loops).
  useEffect(() => {
    if (uiMode !== "browse" || !selId) return;
    const a = anchors.find((x) => x.id === selId);
    if (a) ensureVisible(a.line);
  }, [folds, selId, uiMode, anchors, ensureVisible]);

  useInput((ch, key) => {
    if (key.ctrl && ch === "o") {
      setExpanded((e) => !e);
      setFolds({});
      return;
    }
    if (key.ctrl && ch === "c") {
      void chatRef.current?.close().catch(() => {});
      exit();
      return;
    }
    // shift+tab cycles rotors (arrives as tab+shift, or raw "[Z" after ink
    // strips the ESC prefix from the sequence).
    if ((key.tab && key.shift) || ch === "[Z") {
      const cur = currentRotor.current ?? "router";
      const next = ROTOR_CYCLE[(ROTOR_CYCLE.indexOf(cur) + 1) % ROTOR_CYCLE.length];
      push({ kind: "note", text: `switching rotor → ${next}` });
      void openRotor(next);
      return;
    }
    if (key.tab || ch === "\t" || (uiMode === "browse" && key.escape)) {
      if (uiMode === "compose") {
        const lastFoldable = [...anchors].reverse().find((a) => a.foldable);
        const start = lastFoldable?.id ?? (anchors.length ? anchors[anchors.length - 1].id : null);
        setSelId(start);
        if (start) {
          const a = anchors.find((x) => x.id === start);
          if (a) ensureVisible(a.line);
        }
        setUiMode(start ? "browse" : "compose");
      } else {
        setUiMode("compose");
        setScroll(0);
      }
      return;
    }
    if (uiMode === "browse") {
      const idx = anchors.findIndex((a) => a.id === selId);
      if (key.upArrow || key.downArrow) {
        const next = anchors[Math.max(0, Math.min(anchors.length - 1, idx + (key.upArrow ? -1 : 1)))];
        if (next) {
          setSelId(next.id);
          ensureVisible(next.line);
        }
        return;
      }
      if (key.return || ch === " ") {
        if (selId) {
          setFolds((f) => ({ ...f, [selId]: !(f[selId] ?? expanded) }));
          const a = anchors.find((x) => x.id === selId);
          if (a) ensureVisible(a.line);
        }
        return;
      }
      if (key.pageUp || key.pageDown) {
        scrollBy(key.pageUp ? paneH - 1 : -(paneH - 1));
        return;
      }
      if (ch && (ch >= " " || /[\r\n]/.test(ch))) {
        // Typing means the user wants the composer back — switch and let the
        // shared handling below process this same keystroke.
        setUiMode("compose");
        setScroll(0);
      } else {
        return;
      }
    }
    // The scroll surface: wheel arrives as arrows (alternate-scroll), plus
    // paging keys. Follow mode is offset 0 — any new content snaps the tail.
    if (key.upArrow) {
      scrollBy(1);
      return;
    }
    if (key.downArrow) {
      scrollBy(-1);
      return;
    }
    if (key.pageUp || (key.ctrl && ch === "u")) {
      scrollBy(paneH - 1);
      return;
    }
    if (key.pageDown || (key.ctrl && ch === "d")) {
      scrollBy(-(paneH - 1));
      return;
    }
    if (key.return) {
      submit();
      return;
    }
    if (key.backspace || key.delete) {
      editInput((v) => v.slice(0, -1));
      return;
    }
    if (ch && !key.ctrl && !key.meta) {
      const nl = ch.search(/[\r\n]/);
      if (nl >= 0) {
        const before = ch.slice(0, nl);
        if (before) editInput((v) => v + before);
        submit();
        return;
      }
      if (ch >= " ") editInput((v) => v + ch);
    }
  });

  const model = process.env.ROTOR_MODEL_URL ?? "stub";
  const live = Boolean(process.env.ROTOR_MODEL_URL);

  return (
    // rows-1 is the ceiling: Ink's log-update appends a trailing newline (the
    // cursor row), and content height >= rows trips the clearTerminal path.
    <Box flexDirection="column" width={cols} height={rows - 1}>
      <Box flexDirection="column" ref={headerBox}>
        {chat ? (
          <Header width={cols} version={version} session={chat.session} ws={ws} model={model} store={describeStore()} memory={memory} memoryHot={memoryHot} />
        ) : (
          <Text color={error ? theme.white : theme.dim}>{error ? `rotor error: ${error}` : "opening rotor…"}</Text>
        )}
      </Box>
      <Box flexDirection="column" flexGrow={1} paddingX={1} overflow="hidden">
        {visible.map((l, i) => (
          <TranscriptLine key={`${start + i}`} line={l} />
        ))}
      </Box>
      {clampedScroll > 0 ? (
        <Text color={theme.dim}>{`  ↓ ${clampedScroll} lines below · ↑/↓ wheel scrolls · pgdn returns`}</Text>
      ) : null}
      <Box flexDirection="column" ref={stripBox}>
        <SubFooter width={cols} busy={busy} elapsed={elapsed} last={last} verb={verb} />
        <InputBar width={cols} value={input} busy={busy} />
        {chat ? (
          <Footer width={cols} mode={chat.manifest.mode} model={model} live={live} rotor={chat.rotor} totalUp={totals.up} totalDown={totals.down} />
        ) : null}
      </Box>
    </Box>
  );
}
