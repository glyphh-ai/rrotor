/**
 * The stator factory (src/exec/stator.ts): sync backends resolve immediately, the
 * async `pgvector` backend is refused on the sync path, and env selection maps
 * ROTOR_STATOR_BACKEND to the right backend.
 */

import { describe, it, expect } from "vitest";

import { createStator, initStator, statorFromEnv, statorFromEnvAsync } from "../../src/exec/stator.js";
import { InProcessStore } from "../../src/exec/store.js";
import { SqliteStore } from "../../src/exec/sqlite-store.js";

describe("stator factory", () => {
  it("createStator returns the in-process store by default", () => {
    expect(createStator()).toBeInstanceOf(InProcessStore);
  });

  it("createStator builds a sqlite store", () => {
    const s = createStator({ backend: "sqlite" });
    expect(s).toBeInstanceOf(SqliteStore);
    s.close?.();
  });

  it("createStator refuses the async pgvector backend on the sync path", () => {
    expect(() => createStator({ backend: "pgvector" })).toThrow(/async/i);
  });

  it("initStator delegates sync backends without awaiting a connection", async () => {
    expect(await initStator({ backend: "memory" })).toBeInstanceOf(InProcessStore);
    const s = await initStator({ backend: "sqlite" });
    expect(s).toBeInstanceOf(SqliteStore);
    s.close?.();
  });

  it("env selection maps the backend name", async () => {
    expect(statorFromEnv({} as NodeJS.ProcessEnv)).toBeInstanceOf(InProcessStore);
    expect(statorFromEnv({ ROTOR_STATOR_BACKEND: "sqlite" } as unknown as NodeJS.ProcessEnv)).toBeInstanceOf(SqliteStore);
    expect(await statorFromEnvAsync({} as NodeJS.ProcessEnv)).toBeInstanceOf(InProcessStore);
  });
});
