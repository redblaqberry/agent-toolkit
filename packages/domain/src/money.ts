/** All money is integer euro cents. The EUR 500 line is the board-approved
 * delegation-of-authority threshold from the Nordlicht discovery contract. */

export const APPROVAL_THRESHOLD_CENTS = 50_000;

export function formatEur(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const euros = Math.trunc(abs / 100);
  const rest = String(abs % 100).padStart(2, "0");
  return `${sign}EUR ${euros}.${rest}`;
}

/** Renders cents as the decimal euro string used by the v1 warehouse export
 * (for example 129900 -> "1299.00"). */
export function centsToEuroString(cents: number): string {
  const euros = Math.trunc(cents / 100);
  const rest = String(Math.abs(cents) % 100).padStart(2, "0");
  return `${euros}.${rest}`;
}
