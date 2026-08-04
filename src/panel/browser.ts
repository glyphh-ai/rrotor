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
 * `Page.frameNavigated`), url/title reads, teardown — and, for the v2 WebRTC
 * transport, the optional {@link PanelPage.capture} rect. The screencast wire
 * itself lives in the session; this seam is transport-agnostic.
 *
 * There are TWO implementations and the difference is exactly one capability:
 * {@link PlaywrightBrowserPool} (panel/playwright-driver.ts) runs headless with
 * no capturable display → screencast only; {@link XvfbBrowserPool}
 * (panel/xvfb-driver.ts) runs HEADFUL on a per-panel Xvfb → `capture()` returns
 * a rect and the native-encode WebRTC path becomes available. INPUT is identical
 * on both — it goes through CDP either way, so only the video path differs.
 */

import type { InputEvent } from "./input.js";
import type { CaptureTarget } from "./encoder.js";

export type { InputEvent, CaptureTarget };

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
  /**
   * Where this page's pixels live on a real X display, for the NATIVE video
   * encoder (panel/encoder.ts) to grab — or `null` when there is no capturable
   * display.
   *
   * The rect is the page's CONTENT area in PHYSICAL display pixels (CSS px ×
   * DPR), browser chrome excluded, so the client sees the page and nothing else.
   * Absent (or null) means "this pod cannot do WebRTC for this panel" and every
   * subscriber falls back to the screencast — the capability probe and the
   * fallback are the same fact.
   */
  capture?(): Promise<CaptureTarget | null>;
  /**
   * Resize the page's rendering surface to `width`×`height` CSS px.
   *
   * Only a HEADFUL page implements this, and it must: emulating device metrics
   * on a real window would decouple the rendered size from the window the
   * encoder is grabbing, and the stream would quietly show the wrong rect.
   * Absent → the session falls back to `Emulation.setDeviceMetricsOverride`,
   * which is correct for a headless page and is what v1 always did.
   */
  resize?(width: number, height: number): Promise<void>;
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
  /** Whether panels from this driver can carry video over WebRTC — i.e. the
   *  driver produces capturable pages AND the pod has a native encoder. Absent
   *  means no; the panel is screencast-only. */
  supportsWebrtc?(): Promise<boolean>;
}
