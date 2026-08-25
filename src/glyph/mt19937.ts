/**
 * mt19937.ts — the Mersenne Twister, numpy-RandomState-compatible.
 *
 * The canonical glyph atoms are minted by
 * `np.random.RandomState(derived_seed).choice([-1, 1], size=dim)`
 * (glyphh/core/ops.py `generate_symbol`). Legacy numpy seeds MT19937 via
 * `init_by_array([seed])` and `choice([-1,1])` reduces to bit 0 of successive
 * raw 32-bit outputs (verified empirically against numpy — randint(0,2) and
 * choice agree draw-for-draw with the raw stream's LSBs).
 *
 * This is the reference MT19937 (Matsumoto & Nishimura) with the init_by_array
 * seeding numpy uses — ported so TS atoms are BYTE-IDENTICAL to the Python
 * canon's. The oracle suite asserts exactly that.
 */

const N = 624;
const M = 397;
const MATRIX_A = 0x9908b0df;
const UPPER_MASK = 0x80000000;
const LOWER_MASK = 0x7fffffff;

export class Mt19937 {
  private readonly mt = new Uint32Array(N);
  private mti = N + 1;

  /** numpy RandomState seeding: a SCALAR seed runs init_genrand (the legacy
   *  `mt19937_seed`); an ARRAY seed runs init_by_array. The canon's
   *  generate_symbol passes a scalar — the atoms ride init_genrand. */
  constructor(seed: number | number[]) {
    if (Array.isArray(seed)) this.initByArray(seed);
    else { this.initGenrand(seed >>> 0); this.mti = N; }
  }

  private initGenrand(s: number): void {
    this.mt[0] = s >>> 0;
    for (this.mti = 1; this.mti < N; this.mti++) {
      const prev = this.mt[this.mti - 1]! ^ (this.mt[this.mti - 1]! >>> 30);
      // 1812433253 * prev + mti, in 32-bit arithmetic (split multiply — JS
      // doubles lose bits past 2^53, so the reference splits into 16-bit halves)
      this.mt[this.mti] =
        ((((prev >>> 16) * 1812433253) << 16) + (prev & 0xffff) * 1812433253 + this.mti) >>> 0;
    }
  }

  private initByArray(key: number[]): void {
    this.initGenrand(19650218);
    let i = 1;
    let j = 0;
    let k = Math.max(N, key.length);
    for (; k; k--) {
      const prev = this.mt[i - 1]! ^ (this.mt[i - 1]! >>> 30);
      this.mt[i] =
        ((this.mt[i]! ^ ((((prev >>> 16) * 1664525) << 16) + (prev & 0xffff) * 1664525)) +
          (key[j]! >>> 0) + j) >>> 0;
      i++; j++;
      if (i >= N) { this.mt[0] = this.mt[N - 1]!; i = 1; }
      if (j >= key.length) j = 0;
    }
    for (k = N - 1; k; k--) {
      const prev = this.mt[i - 1]! ^ (this.mt[i - 1]! >>> 30);
      this.mt[i] =
        ((this.mt[i]! ^ ((((prev >>> 16) * 1566083941) << 16) + (prev & 0xffff) * 1566083941)) - i) >>> 0;
      i++;
      if (i >= N) { this.mt[0] = this.mt[N - 1]!; i = 1; }
    }
    this.mt[0] = 0x80000000;
  }

  /** The next raw 32-bit output. */
  next32(): number {
    let y: number;
    if (this.mti >= N) {
      const mag01 = [0, MATRIX_A];
      let kk = 0;
      for (; kk < N - M; kk++) {
        y = (this.mt[kk]! & UPPER_MASK) | (this.mt[kk + 1]! & LOWER_MASK);
        this.mt[kk] = (this.mt[kk + M]! ^ (y >>> 1) ^ mag01[y & 0x1]!) >>> 0;
      }
      for (; kk < N - 1; kk++) {
        y = (this.mt[kk]! & UPPER_MASK) | (this.mt[kk + 1]! & LOWER_MASK);
        this.mt[kk] = (this.mt[kk + (M - N)]! ^ (y >>> 1) ^ mag01[y & 0x1]!) >>> 0;
      }
      y = (this.mt[N - 1]! & UPPER_MASK) | (this.mt[0]! & LOWER_MASK);
      this.mt[N - 1] = (this.mt[M - 1]! ^ (y >>> 1) ^ mag01[y & 0x1]!) >>> 0;
      this.mti = 0;
    }
    y = this.mt[this.mti++]!;
    y ^= y >>> 11;
    y = (y ^ ((y << 7) & 0x9d2c5680)) >>> 0;
    y = (y ^ ((y << 15) & 0xefc60000)) >>> 0;
    y ^= y >>> 18;
    return y >>> 0;
  }
}
