import { readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

// Enumerate files ourselves: cmd.exe does not expand wildcards, and a single
// node --check invocation does not validate a list of source-file arguments.
function javascriptFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return javascriptFiles(path);
    return entry.isFile() && entry.name.endsWith(".js") ? [path] : [];
  });
}
const files = ["lib", "api"].flatMap((directory) => javascriptFiles(resolve(directory))).sort();
if (files.length === 0) throw new Error("No cloud JavaScript files found to check");
for (const file of files) {
  const result = spawnSync(process.execPath, ["--check", file], { stdio: "inherit", timeout: 30000 });
  if (result.error) console.error(`Syntax check could not run for ${file}: ${result.error.message}`);
  if (result.status !== 0 || result.error) process.exit(result.status || 1);
}
console.log(`Syntax checked ${files.length} JavaScript files.`);
