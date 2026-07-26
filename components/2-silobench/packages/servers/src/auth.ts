import type { Principal } from "@silobench/domain";

/** Local test-auth perimeter. HTTP carries the principal in the
 * X-Test-Principal header; stdio carries it in SILOBENCH_PRINCIPAL. This is
 * deliberately not real auth (README scope trap: no OAuth/SSO); the perimeter
 * under test is what each principal may DO, not how identity is proven. */

export const PRINCIPALS = ["ap_clerk", "ap_approver"] as const;

export function parsePrincipal(value: unknown): Principal | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return (PRINCIPALS as readonly string[]).includes(normalized) ? (normalized as Principal) : null;
}
