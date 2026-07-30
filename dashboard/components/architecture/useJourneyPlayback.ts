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
 */

export interface PlaybackState {
  /** True while a journey is running (including its final dwell). */
  playing: boolean;
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
  stepIndex: -1,
  progress: 0,
  visited: new Set(),
  activeNode: null,
  finished: false,
};

/**
 * Total time a step occupies: the hop itself plus any extra dwell once it
 * arrives (used to hold on the creation and the bridge ack, which are the two
 * moments the animation exists to explain).
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

export function useJourneyPlayback() {
  const [state, setState] = useState<PlaybackState>(IDLE);
  const frameRef = useRef<number | null>(null);
  const startRef = useRef<number>(0);
  const stepRef = useRef<number>(0);
  const visitedRef = useRef<Set<string>>(new Set());
  /**
   * Reduced-motion playback steps on timeouts rather than rAF, so the two modes
   * need separate handles and `stop` clears whichever is live.
   */
  const timerRef = useRef<number | null>(null);

  const cancel = useCallback(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    cancel();
    stepRef.current = 0;
    visitedRef.current = new Set();
    setState(IDLE);
  }, [cancel]);

  // Stop the loop if the component goes away mid-journey, so a stray frame
  // can't call setState on an unmounted tree.
  useEffect(() => cancel, [cancel]);

  const play = useCallback(() => {
    cancel();
    stepRef.current = 0;
    visitedRef.current = new Set([JOURNEY_STEPS[0].from]);
    const reducedMotion = prefersReducedMotion();

    /**
     * Reduced motion: no travelling token. The journey still plays so the
     * captions and the id handover are readable, but each step snaps to its
     * destination and holds — movement is the thing being suppressed, not the
     * explanation (WCAG 2.3.3 / prefers-reduced-motion).
     */
    if (reducedMotion) {
      let i = 0;
      const advance = () => {
        const step = JOURNEY_STEPS[i];
        const arrival = step.highlight ?? (step.reverse ? step.from : step.to);
        visitedRef.current = new Set(visitedRef.current).add(arrival);
        setState({
          playing: true,
          stepIndex: i,
          progress: 1,
          visited: visitedRef.current,
          activeNode: arrival,
          finished: false,
        });
        i += 1;
        if (i >= JOURNEY_STEPS.length) {
          window.setTimeout(
            () =>
              setState((prev) => ({ ...prev, playing: false, finished: true })),
            700
          );
          return;
        }
        timerRef.current = window.setTimeout(advance, 700);
      };
      advance();
      return;
    }

    startRef.current = performance.now();

    const tick = (now: number) => {
      const step = JOURNEY_STEPS[stepRef.current];
      if (!step) return;

      const elapsed = now - startRef.current;
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
        stepIndex: stepRef.current,
        progress,
        visited: visitedRef.current,
        activeNode: progress >= 1 ? arrival : null,
        finished: false,
      });

      if (elapsed >= total) {
        stepRef.current += 1;
        startRef.current = now;
        if (stepRef.current >= JOURNEY_STEPS.length) {
          // Hold the completed state on screen: the last caption is the
          // SUCCESS terminal, which is the punchline.
          setState((prev) => ({ ...prev, playing: false, finished: true }));
          frameRef.current = null;
          return;
        }
      }
      frameRef.current = requestAnimationFrame(tick);
    };

    frameRef.current = requestAnimationFrame(tick);
  }, [cancel]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  const stopAll = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    stop();
  }, [stop]);

  return { state, play, stop: stopAll };
}
