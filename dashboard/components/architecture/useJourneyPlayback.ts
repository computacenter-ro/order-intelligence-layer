"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  HOP_DURATION,
  JOURNEY_STEPS,
  type JourneyStep,
} from "@/lib/architectureJourney";

/**
 * Drives the order-journey replay: advances a token along the diagram's edges,
 * one hop at a time, and reports where it is so the SVG can draw it.
 *
 * Kept out of `PipelineDiagram` because the two have nothing to say to each
 * other beyond "here is the token position": the diagram owns pan/zoom and
 * hit-testing, this owns the clock.
 *
 * The clock is `requestAnimationFrame` driven off `performance.now()` rather
 * than a `setInterval` per hop — the token position has to be a smooth function
 * of elapsed time, and an interval would quantise it to the timer's resolution
 * and drift over a ~25-hop sequence.
 *
 * Three transport controls, which is why elapsed time is accumulated rather
 * than measured from a start timestamp:
 *   - `play()`    starts from the beginning, or RESUMES from a pause.
 *   - `pause()`   freezes the token in place, keeping the visited trail.
 *   - `reset()`   clears everything back to idle.
 * Speed is a live multiplier — see `setSpeed`.
 */

export interface PlaybackState {
  /** True while the token is actually moving. */
  playing: boolean;
  /**
   * True when a journey is part-way through but frozen. Distinct from
   * `!playing`: an idle map and a paused one look different and offer different
   * controls (Play means "start" vs "resume").
   */
  paused: boolean;
  /** Index into JOURNEY_STEPS, or -1 when idle. */
  stepIndex: number;
  /** 0..1 along the current hop's path. */
  progress: number;
  /** Nodes the journey has already visited — drawn as "touched". */
  visited: Set<string>;
  /** The node the token most recently arrived at. */
  activeNode: string | null;
  /** True once the last hop has finished; keeps the final state on screen. */
  finished: boolean;
}

const IDLE: PlaybackState = {
  playing: false,
  paused: false,
  stepIndex: -1,
  progress: 0,
  visited: new Set(),
  activeNode: null,
  finished: false,
};

/** Speed multipliers the UI slider can select, slowest to fastest. */
export const MIN_SPEED = 0.25;
export const MAX_SPEED = 4;
export const DEFAULT_SPEED = 1;

/**
 * Total time a step occupies: the hop itself plus any extra dwell once it
 * arrives (used to hold on the creation and the bridge ack, which are the two
 * moments the animation exists to explain).
 *
 * Speed is applied to the accumulated clock, not here — so a step's dwell
 * scales with speed exactly like its hop does.
 */
function stepDuration(step: JourneyStep): number {
  return HOP_DURATION + (step.dwell ?? 0);
}

/**
 * Whether the viewer has asked for reduced motion. Read at Play time rather
 * than subscribed to: it only decides how a journey starts, and a mid-journey
 * change of OS setting is not worth a listener.
 */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** Reduced-motion step cadence at 1x, in ms. */
const REDUCED_STEP_MS = 700;

export function useJourneyPlayback() {
  const [state, setState] = useState<PlaybackState>(IDLE);
  const [speed, setSpeedState] = useState<number>(DEFAULT_SPEED);
  const frameRef = useRef<number | null>(null);
  const stepRef = useRef<number>(0);
  const visitedRef = useRef<Set<string>>(new Set());
  /**
   * Reduced-motion playback steps on timeouts rather than rAF, so the two modes
   * need separate handles and the stop paths clear whichever is live.
   */
  const timerRef = useRef<number | null>(null);

  /**
   * Time already spent inside the CURRENT step, in "journey ms" (i.e. speed
   * already applied). Accumulating this instead of diffing against a fixed
   * start timestamp is what makes both pause/resume and a live speed change
   * work: the token's position depends only on this number, so changing speed
   * alters how fast it grows and never where the token currently is.
   */
  const stepElapsedRef = useRef<number>(0);
  /** Timestamp of the previous frame, to derive each frame's delta. */
  const lastFrameRef = useRef<number>(0);
  /**
   * Live mirror of `speed` for the rAF loop. The loop is created once per
   * play/resume and closes over its variables, so it cannot read the state
   * value — without this a speed change would not take effect until the next
   * play. Not derivable from state inside the loop, hence a ref.
   */
  const speedRef = useRef<number>(DEFAULT_SPEED);

  const cancel = useCallback(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // Stop the loop if the component goes away mid-journey, so a stray frame or
  // timeout can't call setState on an unmounted tree.
  useEffect(() => cancel, [cancel]);

  const setSpeed = useCallback((next: number) => {
    const clamped = Math.min(MAX_SPEED, Math.max(MIN_SPEED, next));
    speedRef.current = clamped;
    setSpeedState(clamped);
  }, []);

  const reset = useCallback(() => {
    cancel();
    stepRef.current = 0;
    visitedRef.current = new Set();
    stepElapsedRef.current = 0;
    setState(IDLE);
  }, [cancel]);

  const pause = useCallback(() => {
    cancel();
    // Keep stepIndex/progress/visited exactly as they are — that is the freeze.
    setState((prev) =>
      prev.playing ? { ...prev, playing: false, paused: true } : prev
    );
  }, [cancel]);

  /**
   * Starts the rAF loop from whatever `stepRef`/`stepElapsedRef` currently hold,
   * so it serves both a fresh play and a resume.
   */
  const runFrom = useCallback(() => {
    cancel();
    lastFrameRef.current = performance.now();

    const tick = (now: number) => {
      const step = JOURNEY_STEPS[stepRef.current];
      if (!step) return;

      // Advance the journey clock by real time scaled by the CURRENT speed,
      // read fresh each frame so the slider takes effect immediately.
      const delta = now - lastFrameRef.current;
      lastFrameRef.current = now;
      stepElapsedRef.current += delta * speedRef.current;

      const elapsed = stepElapsedRef.current;
      const total = stepDuration(step);
      // The token reaches the destination at HOP_DURATION; any remaining time
      // is dwell, during which progress stays pinned at 1.
      const progress = Math.min(1, elapsed / HOP_DURATION);

      // Mark the arrival node as soon as the token gets there, not when the
      // whole step (hop + dwell) ends — otherwise a dwelling step looks like
      // the token stalled short of the box.
      const arrival = step.highlight ?? (step.reverse ? step.from : step.to);
      if (progress >= 1 && !visitedRef.current.has(arrival)) {
        visitedRef.current = new Set(visitedRef.current).add(arrival);
      }

      setState({
        playing: true,
        paused: false,
        stepIndex: stepRef.current,
        progress,
        visited: visitedRef.current,
        activeNode: progress >= 1 ? arrival : null,
        finished: false,
      });

      if (elapsed >= total) {
        stepRef.current += 1;
        // Carry the overshoot into the next step instead of dropping it, so a
        // high speed (where one frame can exceed a whole step) stays accurate.
        stepElapsedRef.current = elapsed - total;
        if (stepRef.current >= JOURNEY_STEPS.length) {
          // Hold the completed state on screen: the last caption is the
          // SUCCESS terminal, which is the punchline.
          setState((prev) => ({
            ...prev,
            playing: false,
            paused: false,
            finished: true,
          }));
          frameRef.current = null;
          return;
        }
      }
      frameRef.current = requestAnimationFrame(tick);
    };

    frameRef.current = requestAnimationFrame(tick);
  }, [cancel]);

  /**
   * Reduced motion: no travelling token. The journey still plays so the
   * captions and the id handover are readable, but each step snaps to its
   * destination and holds — movement is the thing being suppressed, not the
   * explanation (WCAG 2.3.3 / prefers-reduced-motion). Pause/resume and speed
   * apply here too: the cadence is a timeout scaled by the same multiplier.
   */
  const runReducedFrom = useCallback(() => {
    cancel();
    const advance = () => {
      const i = stepRef.current;
      const step = JOURNEY_STEPS[i];
      if (!step) return;
      const arrival = step.highlight ?? (step.reverse ? step.from : step.to);
      visitedRef.current = new Set(visitedRef.current).add(arrival);
      setState({
        playing: true,
        paused: false,
        stepIndex: i,
        progress: 1,
        visited: visitedRef.current,
        activeNode: arrival,
        finished: false,
      });
      stepRef.current = i + 1;
      const wait = REDUCED_STEP_MS / speedRef.current;
      if (stepRef.current >= JOURNEY_STEPS.length) {
        timerRef.current = window.setTimeout(
          () =>
            setState((prev) => ({
              ...prev,
              playing: false,
              paused: false,
              finished: true,
            })),
          wait
        );
        return;
      }
      timerRef.current = window.setTimeout(advance, wait);
    };
    advance();
  }, [cancel]);

  const play = useCallback(() => {
    // Resume: keep the accumulated position and carry on.
    if (state.paused) {
      if (prefersReducedMotion()) runReducedFrom();
      else runFrom();
      return;
    }
    // Fresh start (from idle, or replaying after finishing).
    stepRef.current = 0;
    stepElapsedRef.current = 0;
    visitedRef.current = new Set([JOURNEY_STEPS[0].from]);
    if (prefersReducedMotion()) runReducedFrom();
    else runFrom();
  }, [state.paused, runFrom, runReducedFrom]);

  return { state, speed, play, pause, reset, setSpeed };
}
