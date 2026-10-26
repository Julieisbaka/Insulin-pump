// Runs Python code from this repository inside the official MicroPython WebAssembly port,
// so MicroPython compatibility can be checked without a Pico attached.
//
//   npm install --no-save @micropython/micropython-webassembly-pyscript
//   node tools/run_micropython_wasm.mjs tools/micropython_smoke.py
//   node tools/run_micropython_wasm.mjs --sim 24 42      (prints a full simulation log)
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadMicroPython } from "@micropython/micropython-webassembly-pyscript";

const root = process.env.PUMP_ROOT || join(dirname(fileURLToPath(import.meta.url)), "..");
const mp = await loadMicroPython({
  stdout: (line) => process.stdout.write(line.endsWith("\n") ? line : line + "\n"),
  stderr: (line) => process.stderr.write(line.endsWith("\n") ? line : line + "\n"),
});

function copyTree(dir, filter) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const target = "/" + relative(root, full).split("\\").join("/");
    if (statSync(full).isDirectory()) {
      if (name === "__pycache__") continue;
      try { mp.FS.mkdir(target); } catch { /* exists */ }
      copyTree(full, filter);
    } else if (filter(name)) {
      mp.FS.writeFile(target, readFileSync(full));
    }
  }
}

for (const dir of ["insulin_pump", "profiles"]) {
  try { mp.FS.mkdir("/" + dir); } catch { /* exists */ }
  copyTree(join(root, dir), (name) => name.endsWith(".py") || name.endsWith(".json"));
}
mp.FS.chdir("/");
mp.runPython("import sys\nif '/' not in sys.path: sys.path.append('/')");

const args = process.argv.slice(2);
if (args[0] === "--sim") {
  const hours = parseInt(args[1] ?? "24", 10);
  const seed = parseInt(args[2] ?? "42", 10);
  mp.runPython(`from insulin_pump import simulate\nsimulate.run(hours=${hours}, seed=${seed}, verbose=True, summary=True)`);
} else {
  mp.runPython(readFileSync(join(root, args[0] ?? "tools/micropython_smoke.py"), "utf8"));
}
