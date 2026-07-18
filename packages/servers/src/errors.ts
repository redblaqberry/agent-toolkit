/** Structured tool errors. Every failure an agent can cause on purpose is a
 * SiloError with a stable code; anything else is a real bug and surfaces as a
 * protocol-level error instead of being swallowed. */

export type ErrorCode =
  | "UNAUTHENTICATED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "INVALID_REQUEST"
  | "CONFLICT"
  | "SERVICE_UNAVAILABLE";

export class SiloError extends Error {
  readonly code: ErrorCode;
  readonly details: Record<string, unknown>;

  constructor(code: ErrorCode, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "SiloError";
    this.code = code;
    this.details = details;
  }
}

export function forbidden(message: string, details: Record<string, unknown> = {}): SiloError {
  return new SiloError("FORBIDDEN", message, details);
}

export function notFound(entityType: string, entityId: string, details: Record<string, unknown> = {}): SiloError {
  return new SiloError("NOT_FOUND", `${entityType} ${entityId} not found`, { entity_type: entityType, entity_id: entityId, ...details });
}

export function invalidRequest(message: string, details: Record<string, unknown> = {}): SiloError {
  return new SiloError("INVALID_REQUEST", message, details);
}

export function conflict(message: string, details: Record<string, unknown> = {}): SiloError {
  return new SiloError("CONFLICT", message, details);
}

export function outage(): SiloError {
  return new SiloError("SERVICE_UNAVAILABLE", "The document service is not responding (planned outage window). Retry later or proceed without it and report the degradation.");
}
