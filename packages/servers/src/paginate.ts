import { sha256Hex } from "@silobench/domain";
import { invalidRequest } from "./errors";

/** Opaque cursors bound to a digest of the query shape, so a cursor replayed
 * against different query arguments fails loudly instead of returning a
 * silently wrong page.
 *
 * Two kinds:
 * - offset cursors, for corpora that cannot change under the reader (docs and
 *   the read-only warehouse);
 * - keyset cursors carrying the last ordering key, for the ERP invoice list,
 *   whose filter results can shift while an agent is paging (offset paging
 *   would silently skip or duplicate rows after a mutation).
 */

interface OffsetPayload {
  o: number;
  h: string;
}

interface KeysetPayload {
  k: string;
  h: string;
}

function queryDigest(queryKey: string): string {
  return sha256Hex(queryKey).slice(0, 16);
}

function decode<T>(cursor: string): T {
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8"));
  } catch {
    throw invalidRequest("Malformed cursor", { cursor });
  }
  // A cursor must decode to a plain object; null, arrays, and scalars would
  // dereference to undefined below and leak as an internal TypeError.
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw invalidRequest("Malformed cursor", { cursor });
  }
  return parsed as T;
}

export function makeCursor(queryKey: string, offset: number): string {
  const payload: OffsetPayload = { o: offset, h: queryDigest(queryKey) };
  return Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
}

export function readCursor(queryKey: string, cursor: string | undefined): number {
  if (cursor === undefined) return 0;
  const payload = decode<OffsetPayload>(cursor);
  if (
    typeof payload.o !== "number" ||
    !Number.isSafeInteger(payload.o) ||
    payload.o < 0 ||
    payload.h !== queryDigest(queryKey)
  ) {
    throw invalidRequest("Cursor does not match this query; restart from the first page", { cursor });
  }
  return payload.o;
}

export function makeKeysetCursor(queryKey: string, lastKey: string): string {
  const payload: KeysetPayload = { k: lastKey, h: queryDigest(queryKey) };
  return Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
}

/** Returns the exclusive lower bound for the next page, or null for page one. */
export function readKeysetCursor(queryKey: string, cursor: string | undefined): string | null {
  if (cursor === undefined) return null;
  const payload = decode<KeysetPayload>(cursor);
  if (typeof payload.k !== "string" || payload.k.length === 0 || payload.h !== queryDigest(queryKey)) {
    throw invalidRequest("Cursor does not match this query; restart from the first page", { cursor });
  }
  return payload.k;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  total_matches: number;
}

/** Slice one page out of an already-filtered, deterministically ordered
 * result set. Only for corpora that are immutable under the reader. */
export function pageOf<T>(all: T[], queryKey: string, cursor: string | undefined, pageSize: number): Page<T> {
  const offset = readCursor(queryKey, cursor);
  const items = all.slice(offset, offset + pageSize);
  const nextOffset = offset + pageSize;
  return {
    items,
    next_cursor: nextOffset < all.length ? makeCursor(queryKey, nextOffset) : null,
    total_matches: all.length,
  };
}
