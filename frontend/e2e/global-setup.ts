import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:net";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "..");
const REPO = resolve(FRONTEND, "..");
const STORE = resolve(REPO, "data", "demo-store"); // git-ignored (data/)

const freePort = (): Promise<number> =>
  new Promise((ok, ko) => {
    const s = createServer();
    s.once("error", ko);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address() as { port: number };
      s.close(() => ok(port));
    });
  });

async function waitFor(url: string, name: string, ms = 90_000): Promise<void> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`${name} did not become ready at ${url}`);
}

/** Stops a detached child and its descendants (uv -> python, npm -> node). */
const stop = (p: ChildProcess): void => {
  if (p.pid === undefined) return;
  try {
    process.kill(-p.pid, "SIGTERM");
  } catch {
    /* already gone */
  }
};

export default async function globalSetup(): Promise<() => void> {
  // 1. SYNTHETIC demo stores (idempotent). Its stdout (which prints a throwaway key) is discarded.
  execFileSync("uv", ["run", "python", "scripts/demo_data.py", "--root", STORE], { cwd: REPO, stdio: "ignore" });

  // 2. Per-run random key: only its SHA-256 reaches the API, the key only the Vite proxy. Never logged or written.
  const key = randomBytes(24).toString("base64url");
  const digest = `sha256:${createHash("sha256").update(key).digest("hex")}`;
  const [apiPort, webPort] = [await freePort(), await freePort()];
  const apiUrl = `http://127.0.0.1:${apiPort}`;
  const baseUrl = `http://127.0.0.1:${webPort}`;

  const children: ChildProcess[] = [];
  const run = (cmd: string, args: string[], cwd: string, env: NodeJS.ProcessEnv): void => {
    const c = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, detached: true, stdio: "ignore" });
    children.push(c);
  };
  const teardown = (): void => children.forEach(stop);

  try {
    // 3. API then Vite dev server (strictPort: the port was just verified free).
    run("uv", ["run", "adp", "api", "serve", "--port", String(apiPort)], REPO, {
      ADP_API_KEYS: digest,
      ADP_GOLD_STORE_ROOT: resolve(STORE, "gold"),
      ADP_SILVER_STORE_ROOT: resolve(STORE, "silver"),
      ADP_QUARANTINE_STORE_ROOT: resolve(STORE, "quarantine"),
      ADP_REPORTS_DIR: resolve(STORE, "reports"),
      ADP_REGISTRY_URL: `sqlite:///${resolve(STORE, "registry.db")}`,
    });
    run("npx", ["vite", "--host", "127.0.0.1", "--port", String(webPort), "--strictPort"], FRONTEND, {
      ADP_API_KEY: key,
      ADP_API_URL: apiUrl,
    });
    await waitFor(`${apiUrl}/health`, "API");
    await waitFor(`${baseUrl}/`, "Vite");
    await waitFor(`${baseUrl}/src/main.tsx`, "Vite (warm-up: first transform of the app)");
    await waitFor(`${baseUrl}/api/runs`, "proxy to the API");
  } catch (e) {
    teardown();
    throw e;
  }
  process.env["ADP_E2E_BASE_URL"] = baseUrl; // inherited by the workers started after global setup
  return teardown; // 4. runs at the end of the whole run
}
