/**
 * main.tsx — the `rrotor` TUI entry. Takes over the full screen (alternate
 * buffer + hidden cursor), renders the fixed-height chat, and restores the
 * terminal cleanly on exit. Requires a TTY — pipes get the readline chat.
 */

import { homedir } from "node:os";
import { render } from "ink";
import { App } from "./App.js";
import { applyStatorPrefs, applyModelPrefs } from "./prefs.js";

function displayCwd(): string {
  const cwd = process.cwd();
  const home = homedir();
  return cwd.startsWith(home) ? `~${cwd.slice(home.length)}` : cwd;
}

export async function runTui(version: string, rotorArg?: string): Promise<number> {
  applyStatorPrefs();
  applyModelPrefs();
  // ALTERNATE buffer: the app owns scrolling (locked header/footer, scrolling
  // middle). On exit the full transcript prints into the NORMAL buffer below,
  // so the session still lives in real shell scrollback afterward.
  process.stdout.write("\x1b[?1049h\x1b[?25l");
  const transcript: string[] = [];
  let restored = false;
  const restore = (): void => {
    if (!restored) {
      restored = true;
      process.stdout.write("\x1b[?25h\x1b[?1049l");
    }
  };
  process.on("exit", restore);
  try {
    const { waitUntilExit } = render(<App version={version} rotorArg={rotorArg} ws={displayCwd()} transcript={transcript} />);
    await waitUntilExit();
    restore();
    if (transcript.length > 0) {
      process.stdout.write(transcript.join("\n") + "\n");
    }
    return 0;
  } catch (err) {
    restore();
    process.stderr.write(`rrotor tui: ${(err as Error).message}\n`);
    return 1;
  }
}
