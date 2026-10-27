import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = fileURLToPath(new URL("..", import.meta.url));
const TSC = fileURLToPath(new URL("../node_modules/typescript/bin/tsc", import.meta.url));

test("the spike compiles under strict tsc --noEmit", () => {
  const result = spawnSync(process.execPath, [TSC, "-p", "tsconfig.json", "--noEmit"], {
    cwd: PACKAGE_ROOT,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
