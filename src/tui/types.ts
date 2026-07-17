/** TUI view-model types — derived purely from wire events. */

export interface TurnStep {
  label: string;
  ok: boolean;
  /** Client-observed arrival spacing (s), when ≥ 0.1. */
  secs?: number;
  tokens?: { up: number; down: number };
  /** Summarized output facts / body lines tabbed under the step. */
  details: string[];
  error?: string;
}

export interface ChatTurn {
  id: string;
  role: "user" | "assistant";
  text: string;
  steps: TurnStep[];
  status: "running" | "done" | "refused" | "failed";
  durationMs?: number;
  tokensUp: number;
  tokensDown: number;
  stubbed: boolean;
  degraded: boolean;
}
