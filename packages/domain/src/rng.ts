/** Deterministic PRNG used only at seed time. mulberry32 is 32-bit integer
 * arithmetic throughout, so sequences are identical on every platform. */

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return function next(): number {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function pickFrom<T>(rng: () => number, items: readonly T[]): T {
  const idx = Math.floor(rng() * items.length);
  const item = items[Math.min(idx, items.length - 1)];
  if (item === undefined) throw new Error("pickFrom: empty list");
  return item;
}

export function intBetween(rng: () => number, min: number, max: number): number {
  return min + Math.floor(rng() * (max - min + 1));
}
