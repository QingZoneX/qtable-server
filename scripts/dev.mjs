import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const projectRoot = fileURLToPath(new URL("..", import.meta.url));
const python = process.platform === "win32"
  ? ".venv\\Scripts\\python.exe"
  : ".venv/bin/python";
const pythonPath = `${projectRoot}/${python}`;

if (!existsSync(pythonPath)) {
  console.error("Missing .venv Python environment. Create it with: python3 -m venv .venv");
  process.exit(1);
}

// QTable 前端已迁至独立仓库 QTableUI，这里只启动后端。
const child = spawn(
  pythonPath,
  ["-m", "uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "9000"],
  { cwd: projectRoot, stdio: "inherit" },
);

let stopping = false;
const stop = (signal) => {
  if (stopping) return;
  stopping = true;
  child.kill(signal);
};

for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => stop(signal));
child.on("exit", (code) => {
  if (!stopping) {
    process.exitCode = code ?? 1;
  }
});
