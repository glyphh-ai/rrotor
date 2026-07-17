/**
 * Header.tsx — the top chrome: one outline whose TOP edge carries the version
 * ("rrotor v0.1.0"). LEFT: the braille rotor mark with the session facts
 * stacked beneath. RIGHT: the pinned rotor's manifest — mode, roles, tools —
 * the config contract a client is expected to surface. A single vertical rule
 * divides them.
 */

import type { ReactElement } from "react";
import { Box, Text } from "ink";
import { theme } from "./theme.js";
import { MARK, GRADIENT } from "../banner.js";

export interface HeaderProps {
  width: number;
  version: string;
  session: string;
  ws: string;
  model: string;
  /** The effective memory store (env/pref), e.g. `sqlite ~/.rrotor/stator.db`. */
  store: string;
  /** Stored size per retention tier — drives the memory gauges. */
  memory: Record<"short" | "mid" | "long", { count: number; bytes: number }>;
  /** Tiers written in the last moment — rendered hot. */
  memoryHot: { short: boolean; mid: boolean; long: boolean };
}

/** The hand-drawn top edge: `┌─ rrotor v0.1.0 ─────────┐`. */
function TopEdge({ width, version }: { width: number; version: string }): ReactElement {
  const label = `rrotor ${version}`;
  const inner = Math.max(0, width - 2);
  const trailing = Math.max(0, inner - 3 - label.length);
  return (
    <Text color={theme.line}>
      {"┌─ "}
      <Text color={theme.accent} bold>
        {label}
      </Text>
      {" " + "─".repeat(trailing) + "┐"}
    </Text>
  );
}

function LeftPanel({ session, ws, model, store }: { session: string; ws: string; model: string; store: string }): ReactElement {
  return (
    <Box flexDirection="column" width="30%" flexShrink={0} alignItems="center" paddingX={1}>
      <Box flexDirection="column">
        {MARK.map((row, i) => (
          <Text key={i} color={GRADIENT[Math.min(i, GRADIENT.length - 1)]}>
            {row}
          </Text>
        ))}
      </Box>
      <Box flexDirection="column" alignItems="center" marginTop={1}>
        <Text color={theme.white} bold>
          recursive reasoning
        </Text>
        <Text color={theme.gray}>on top of reasoning</Text>
        <Text color={theme.dim} wrap="truncate-start">
          {ws}
        </Text>
        <Text color={theme.dim} wrap="truncate-end">
          {model} · {session}
        </Text>
        <Text color={theme.dim} wrap="truncate-end">
          {store}
        </Text>
      </Box>
    </Box>
  );
}

/** The latest three ship notes — shown top-right, newest first. */
const WHATS_NEW = [
  "chat viewport — locked chrome, wheel-scrolled history",
  "section folding — tab browses, enter unfolds, ctrl+o all",
  "conversation memory — follow-ups resolve references",
];

/** Bytes per braille cell — the gauge's ABSOLUTE quantum. A cell is 8 dots,
 *  so one dot ≈ 128 B: a single small fact lights a flicker of the first
 *  square instead of pretending to fill the tank. */
const BYTES_PER_CELL = 1024;
const RAMP = ["⣀", "⣄", "⣤", "⣦", "⣶", "⣷", "⣿"];

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** One tier gauge row: absolute-scale braille bar, hot when written. */
function TierRow({ name, stat, color, hot, cells }: { name: string; stat: { count: number; bytes: number }; color: string; hot: boolean; cells: number }): ReactElement {
  const dots = stat.bytes / (BYTES_PER_CELL / 8);
  const whole = Math.min(cells, Math.floor(dots / 8));
  const fracDots = Math.min(7, Math.round(dots - whole * 8));
  const overflow = stat.bytes > cells * BYTES_PER_CELL;
  const tip = !overflow && whole < cells && fracDots > 0 ? RAMP[Math.max(0, fracDots - 1)] : "";
  const bar = "⣿".repeat(whole) + tip;
  const empty = "⠈".repeat(Math.max(0, cells - whole - (tip ? 1 : 0)));
  return (
    <Text wrap="truncate-end">
      <Text color={theme.dim}>{name.padEnd(6)}</Text>
      <Text color={color} bold={hot}>
        {bar}
      </Text>
      <Text color={theme.dim}>{empty}</Text>
      <Text color={hot ? theme.white : theme.dim} bold={hot}>
        {" "}
        {stat.count} · {humanBytes(stat.bytes)}
        {overflow ? " ▸" : ""}
        {hot ? " ✶" : ""}
      </Text>
    </Text>
  );
}

function MemoryPanel({ memory, memoryHot, width }: { memory: HeaderProps["memory"]; memoryHot: HeaderProps["memoryHot"]; width: number }): ReactElement {
  // Bars run to the far right: panel width minus the label and size columns.
  const cells = Math.max(10, width - 22);
  return (
    <Box flexDirection="column" marginTop={1}>
      <Box borderStyle="single" borderTop={false} borderLeft={false} borderRight={false} borderColor={theme.line}>
        <Text color={theme.accent} bold>
          Memory
        </Text>
      </Box>
      <TierRow name="short" stat={memory.short} color="cyan" hot={memoryHot.short} cells={cells} />
      <TierRow name="mid" stat={memory.mid} color="yellow" hot={memoryHot.mid} cells={cells} />
      <TierRow name="long" stat={memory.long} color="magenta" hot={memoryHot.long} cells={cells} />
    </Box>
  );
}

function NotesPanel({ memory, memoryHot, width }: { memory: HeaderProps["memory"]; memoryHot: HeaderProps["memoryHot"]; width: number }): ReactElement {
  return (
    <Box
      flexDirection="column"
      flexGrow={1}
      borderStyle="single"
      borderTop={false}
      borderBottom={false}
      borderRight={false}
      borderColor={theme.line}
      paddingLeft={2}
      paddingRight={1}
    >
      <Box borderStyle="single" borderTop={false} borderLeft={false} borderRight={false} borderColor={theme.line}>
        <Text color={theme.accent} bold>
          What's New
        </Text>
      </Box>
      {WHATS_NEW.map((n, i) => (
        <Text key={i} color={i === 0 ? theme.white : theme.gray} wrap="truncate-end">
          - {n}
        </Text>
      ))}
      <MemoryPanel memory={memory} memoryHot={memoryHot} width={width} />
      <Box marginTop={1}>
        <Text color={theme.dim} italic>
          shift+tab cycles rotors · tab folds · ctrl+o all · /model · /store · /quit
        </Text>
      </Box>
    </Box>
  );
}

export function Header({ width, version, session, ws, model, store, memory, memoryHot }: HeaderProps): ReactElement {
  return (
    <Box flexDirection="column" width={width}>
      <TopEdge width={width} version={version} />
      <Box borderStyle="single" borderTop={false} borderColor={theme.line} width={width}>
        <LeftPanel session={session} ws={ws} model={model} store={store} />
        <NotesPanel memory={memory} memoryHot={memoryHot} width={Math.max(30, Math.floor(width * 0.7) - 6)} />
      </Box>
    </Box>
  );
}
