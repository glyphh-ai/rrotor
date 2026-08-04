/**
 * panel/browser.ts — the BROWSER DRIVER SEAM.
 *
 * The panel session (session.ts) depends on this narrow interface, never on
 * playwright directly, so the session's lifecycle/registry logic is unit-
 * testable against a fake CDP with no Chromium. The real implementation
 * ({@link PlaywrightBrowserPool}) launches ONE headless Chromium per pod and
 * gives each panel its OWN browser context (isolation: a panel cannot see
 * another panel's cookies/storage) with one page and one CDP session.
 *
 * Only what the panel needs is exposed: navigate, a CDP `send` (screencast +
 * input dispatch), a CDP event subscription (`Page.screencastFrame`,
 * `Page.frameNavigated`), url/title reads, and teardown. The screencast wire
 * itself lives in the session; this seam is transport-agnostic.
 */

import type { InputEvent } from "./input.js";

export type { InputEvent };

/** A live CDP-backed page inside its own isolated browser context. */
export interface PanelPage {
  /** Send a raw CDP command (Page.startScreencast, Input.dispatch*, Emulation.*). */
  send(method: string, params?: Record<string, unknown>): Promise<unknown>;
  /** Subscribe to a raw CDP event; returns an unsubscribe fn. */
  on(event: string, handler: (payload: any) => void): () => void;
  /** Navigate the page. Rejects on a hard navigation failure. */
  goto(url: string): Promise<void>;
  /** Current committed url. */
  url(): string;
  /** Current document title (best-effort; empty before load). */
  title(): Promise<string>;
  /** Close the page AND its owning context (full isolation teardown). */
  close(): Promise<void>;
}

/** Options for opening a panel page. */
export interface OpenPageOptions {
  viewport: { width: number; height: number };
  /** Client devicePixelRatio (1–3). Renders at physical resolution so a Retina
   *  or phone display is crisp instead of upscaled. */
  deviceScaleFactor?: number;
}

/** The pod-wide browser: mints one isolated {@link PanelPage} per panel. */
export interface BrowserDriver {
  open(opts: OpenPageOptions): Promise<PanelPage>;
  /** Tear the whole browser down (pod shutdown). Idempotent. */
  shutdown(): Promise<void>;
}
