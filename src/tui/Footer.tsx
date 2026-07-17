/**
 * Footer.tsx — the quiet status line: permission mode (the rotor's declared
 * tool ladder), the model lane, and the pinned rotor with session token
 * totals. Chrome, not content.
 */

import type { ReactElement } from "react";
import { Box, Text } from "ink";
import { theme } from "./theme.js";

export interface FooterProps {
  width: number;
  mode: string;
  model: string;
  live: boolean;
  rotor: string;
  totalUp: number;
  totalDown: number;
}

const MODE_GLYPH: Record<string, string> = {
  chat: "◎ chat · read-only tools",
  cowork: "✎ cowork · docs + artifacts",
  code: "⏵ code · full workbench",
};

export function Footer({ width, mode, model, live, rotor, totalUp, totalDown }: FooterProps): ReactElement {
  return (
    <Box width={width} paddingX={1} justifyContent="space-between">
      <Text color={theme.gray}>{MODE_GLYPH[mode] ?? mode}</Text>
      <Box>
        <Text color={theme.gray}>model </Text>
        <Text color={live ? theme.accent : theme.dim}>{model}</Text>
        <Text color={theme.dim}>{"   ·   "}</Text>
        <Text color={theme.accent}>{rotor}</Text>
        {totalUp + totalDown > 0 ? <Text color={theme.dim}>{`   ·   ${totalUp}↑ ${totalDown}↓ tok`}</Text> : null}
      </Box>
    </Box>
  );
}
