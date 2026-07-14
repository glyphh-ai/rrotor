// OpenRotor banner — OpenRotor's own identity. A plain figlet wordmark + one
// cyan accent; deliberately NOT glyphh's braille trademark.
const C = "\x1b[36m"; // cyan accent
const D = "\x1b[90m"; // dim
const R = "\x1b[0m";  // reset

const ART = "   ____                   ____        __\n  / __ \\____  ___  ____  / __ \\____  / /_____  _____\n / / / / __ \\/ _ \\/ __ \\/ /_/ / __ \\/ __/ __ \\/ ___/\n/ /_/ / /_/ /  __/ / / / _, _/ /_/ / /_/ /_/ / /\n\\____/ .___/\\___/_/ /_/_/ |_|\\____/\\__/\\____/_/\n    /_/";

export function printBanner(version: string): void {
  for (const ln of ART.split("\n")) console.log("  " + C + ln + R);
  console.log("  " + D + "the open runtime for deterministic agent loops" + R);
  console.log("  " + D + "v" + version + R);
  console.log();
}
