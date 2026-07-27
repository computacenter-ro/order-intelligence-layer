/**
 * Chart colors for the Overview page.
 *
 * Every value here was checked with the dataviz palette validator against the
 * page surface (`--cc-cloud-white` #FAFAFF) rather than picked by eye. Each
 * color does exactly ONE job:
 *
 * - `OUTCOME_*` — **status**: a journey outcome means good/bad, so it wears the
 *   signal colors, not a series palette.
 * - `SEVERITY_RAMP` — **ordinal**: severity is an ordered scale, so it is one
 *   hue with monotone lightness steps (deeper = more severe). This is the same
 *   language `lib/severityPill.ts` already uses for the alert pills.
 * - `SERIES_ONE` — **nominal single series**: departments have no natural order,
 *   so every bar takes the same hue. Coloring them by value would re-encode bar
 *   length as hue and spend the identity channel on nothing.
 *
 * Two validator results are load-bearing and must be re-checked if a color here
 * changes:
 *
 * 1. Green-vs-red is the classic colorblind collapse: Circuit Green against
 *    United Red scores ΔE 4.0 under deuteranopia (target ≥ 8) — indistinguishable.
 *    The pair below works only because the two differ sharply in LIGHTNESS
 *    (light green vs deep red → ΔE 27.2 deutan), so the bars stay distinct
 *    without color vision. Do not "fix" the red back to United Red.
 * 2. Circuit Green sits at 2.09:1 against the surface, under the 3:1 mark floor.
 *    That is allowed only with a relief channel, which is why every bar in these
 *    charts carries a visible value label and a text category label. Removing
 *    the labels would make the fill non-compliant.
 */

// --- journey outcomes (status) ------------------------------------------------

/** Circuit Green — the brand's positive signal. Only ever for SUCCESS. */
export const OUTCOME_SUCCESS = "#54C664";

/**
 * Deep red for every failure outcome. Darker than United Red on purpose: the
 * lightness gap against the green is what keeps the two readable under
 * protanopia/deuteranopia (see the note above).
 */
export const OUTCOME_FAILED = "#A30914";

// --- alert severity (ordinal, one hue, dark → light) -------------------------

/**
 * Red hue, monotone lightness: critical darkest → low lightest, so the
 * escalation is visible without reading the labels. Validated as an ordinal
 * ramp (monotone L, adjacent ΔL ≥ 0.06, light end 2.13:1 vs surface, hue
 * spread 11°).
 */
export const SEVERITY_RAMP: Record<string, string> = {
  critical: "#A30914",
  high: "#E4283A",
  medium: "#F26E7A",
  low: "#F79199",
};

/**
 * Neutral grey for the backend's "unrated" bucket — alerts with no severity
 * (`source="fallback"`). Deliberately OUTSIDE the ramp: "not rated" is an
 * absence of severity, not a fifth level, so it must not read as one.
 */
export const SEVERITY_UNRATED = "var(--cc-grey-three)";

export function severityColor(key: string): string {
  return SEVERITY_RAMP[key] ?? SEVERITY_UNRATED;
}

// --- nominal single series ---------------------------------------------------

/** Heritage Blue — one hue for all bars of a single, unordered series. */
export const SERIES_ONE = "#0D21A0";

/** De-emphasis fill for the "rest of the total" half of a split bar. */
export const SERIES_MUTED = "var(--cc-grey-five)";

// --- chrome ------------------------------------------------------------------

/** Hairline gridlines/axes: one step off the surface, solid, recessive. */
export const CHART_GRID = "var(--cc-grey-five)";
export const CHART_AXIS_TEXT = "var(--cc-grey-two)";
/** The 2px separator between touching fills is drawn in the surface color. */
export const CHART_SURFACE = "var(--cc-cloud-white)";
