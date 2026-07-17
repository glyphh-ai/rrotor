/**
 * InputBar.tsx — the composer. One outlined row: accent prompt, typed text
 * with a block cursor. The border glows accent when there is something to
 * send and dims while a turn is in flight.
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
  const active = value.length > 0 && !busy;
  return (
    <Box width={width} borderStyle="round" borderColor={active ? theme.accent : theme.line} paddingX={1}>
      <Text color={theme.accent}>{"› "}</Text>
      <Text color={busy ? theme.dim : theme.white} wrap="truncate-start">
        {value}
      </Text>
      <Text color={busy ? theme.dim : theme.accent}>█</Text>
    </Box>
  );
}
