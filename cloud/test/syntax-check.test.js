import assert from "node:assert/strict";
import { test } from "node:test";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const cloud = dirname(dirname(fileURLToPath(import.meta.url)));
const command = JSON.parse(readFileSync(join(cloud, "package.json"), "utf8")).scripts.check;
function fixtureCheck(invalid) {
  const root = mkdtempSync(join(tmpdir(), "flexfactor syntax "));
  try {
    for (const name of ["lib", "api/deep folder", "scripts"]) mkdirSync(join(root, name), { recursive: true });
    writeFileSync(join(root, "package.json"), JSON.stringify({ type: "module" }));
    writeFileSync(join(root, "lib/good.js"), "export const value = 1;\n");
    writeFileSync(join(root, "api/root.js"), "export default function handler() {}\n");
    writeFileSync(join(root, "api/deep folder/nested.js"), invalid ? "export const broken = ;\n" : "export const nested = true;\n");
    const checker = join(cloud, "scripts/check-syntax.mjs");
    if (existsSync(checker)) copyFileSync(checker, join(root, "scripts/check-syntax.mjs"));
    return spawnSync(command, { shell: true, cwd: root, encoding: "utf8", timeout: 30000 });
  } finally { rmSync(root, { recursive: true, force: true }); }
}
test("cloud syntax command checks valid files including nested paths with spaces", () => {
  const result = fixtureCheck(false);
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  assert.match(result.stdout, /Syntax checked 3 JavaScript files/);
});
test("cloud syntax command rejects syntax errors in deeply nested API files", () => {
  const result = fixtureCheck(true);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /nested\.js|SyntaxError/);
});
