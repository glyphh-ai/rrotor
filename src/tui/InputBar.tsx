/**
 * InputBar.tsx — the composer. One outlined row: an accent `›` prompt, the typed
 * text with a block cursor, and a button pinned to the far right. Idle it reads
 * "Send ⏎"; while a turn runs it becomes "Esc ✕ stop" and turns red — a terminal
 * button can't be clicked, so it names the key that acts on it. The border glows
 * accent when there is something to send, red while a turn is in flight.
 */

import type { ReactElement } from "react";
import { Box, Text } from "ink";
import { theme } from "./theme.js";

export interface InputBarProps {
  width: number;
  value: string;
  busy: boolean;
}

export function InputBar({ width, value, busy }: InputBarProps): ReactElement {
  const hasText = value.trim().length > 0;
  // While busy the button is the STOP control: it names the key (Esc) because a
  // terminal button isn't clickable, and turns red so it reads as "interrupt".
  const edge = busy ? theme.danger : hasText ? theme.accent : theme.line;
  const button = busy ? " Esc ✕ stop " : " Send ⏎ ";
  return (
    <Box width={width} borderStyle="round" borderColor={edge} paddingX={1} justifyContent="space-between">
      <Box>
        <Text color={theme.accent}>{"› "}</Text>
        {busy ? (
          <Text color={theme.faint}>working… press Esc to stop</Text>
        ) : value ? (
          <Text color={theme.text} wrap="truncate-start">
            {value}
          </Text>
        ) : (
          <Text color={theme.faint}>type a message</Text>
        )}
        {!busy ? <Text color={theme.accent}>█</Text> : null}
      </Box>
      <Text backgroundColor={busy ? theme.danger : hasText ? theme.accent : theme.faint} color={theme.text}>
        {button}
      </Text>
    </Box>
  );
}
