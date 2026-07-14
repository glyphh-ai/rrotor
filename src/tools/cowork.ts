/**
 * `cowork` tool pack — the collaborative surface: a shared task list and named
 * artifacts (the "canvas"). State persists in the stator's kv (durable across the
 * session), so a co-work session's todos and artifacts survive a runtime restart.
 */

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

/** The minimal durable kv the pack needs — the stator's kvGet/kvSet, injected. */
export interface KvLike {
  get(key: string): Promise<unknown>;
  set(key: string, value: unknown): Promise<void>;
}

const TODO_KEY = "cowork:todos";
const artifactKey = (name: string) => `cowork:artifact:${name}`;

export function coworkPack(kv: KvLike): ToolPack {
  const tools: ToolSpec[] = [
    {
      name: "todo.write",
      version: 1,
      description: "Set the session's task list: an array of { task, status }.",
      effect: "mutating",
      grants: ["cowork.write"],
      input: {
        type: "object",
        properties: { todos: { type: "array", items: { type: "object", properties: { task: { type: "string" }, status: { type: "string" } } } } },
        required: ["todos"],
      },
      output: { type: "object", properties: { count: { type: "number" } } },
      handler: async (args: Row) => {
        if (!Array.isArray(args.todos)) throw new RotorError("E_MISSING_INPUT", "todo.write requires a `todos` array");
        const todos = (args.todos as Array<Record<string, unknown>>).map((t) => ({
          task: String(t.task ?? ""),
          status: String(t.status ?? "pending"),
        }));
        await kv.set(TODO_KEY, todos);
        return { count: todos.length };
      },
    },
    {
      name: "todo.read",
      version: 1,
      description: "Read the session's task list.",
      effect: "reading",
      grants: ["memory.read"],
      input: { type: "object", properties: {} },
      output: { type: "object", properties: { todos: { type: "array" } } },
      handler: async () => {
        const todos = (await kv.get(TODO_KEY)) ?? [];
        return { todos };
      },
    },
    {
      name: "artifact.write",
      version: 1,
      description: "Create or replace a named artifact (a document/output on the canvas).",
      effect: "mutating",
      grants: ["cowork.write"],
      input: {
        type: "object",
        properties: { name: { type: "string" }, content: { type: "string" }, kind: { type: "string" } },
        required: ["name", "content"],
      },
      output: { type: "object", properties: { name: { type: "string" }, bytes: { type: "number" } } },
      handler: async (args: Row) => {
        const name = String(args.name ?? "").trim();
        if (!name) throw new RotorError("E_MISSING_INPUT", "artifact.write requires a `name`");
        const content = String(args.content ?? "");
        await kv.set(artifactKey(name), { name, content, kind: String(args.kind ?? "text") });
        return { name, bytes: Buffer.byteLength(content) };
      },
    },
    {
      name: "artifact.read",
      version: 1,
      description: "Read a named artifact.",
      effect: "reading",
      grants: ["memory.read"],
      input: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
      output: { type: "object", properties: { name: { type: "string" }, content: { type: "string" }, found: { type: "boolean" } } },
      handler: async (args: Row) => {
        const name = String(args.name ?? "");
        const a = (await kv.get(artifactKey(name))) as { name: string; content: string; kind: string } | undefined;
        if (!a) return { name, content: "", found: false };
        return { ...a, found: true };
      },
    },
  ];

  return { name: "cowork", version: "1.0.0", tools };
}
