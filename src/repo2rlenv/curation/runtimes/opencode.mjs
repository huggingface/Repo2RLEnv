/**
 * OpenCode 1.18.28 adapter. Run with: node opencode.mjs CONFIG_JSON_PATH
 *
 * Only the private Anthropic/tool HTTP bridge is exposed to the model. The
 * bridge owns credentials, request limits, tool execution, and actual cost.
 * OpenCode's native session loop and events are retained. No SDK is required.
 */
import { execFileSync, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { copyFile, mkdir, mkdtemp, readFile, symlink, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { describeError, requestJSON, RUNTIME_REQUEST_TIMEOUT_MS } from "./local_http.mjs";

const require = createRequire(import.meta.url);
const VERSION = "1.18.28";
const PROVIDER = "repo2rlenv";
const AGENT = "author";

export function validateConfig(config) {
  for (const key of ["model", "system", "prompt", "bridge_url", "bridge_token", "session_dir"]) {
    if (typeof config[key] !== "string" || !config[key]) throw new Error(`Missing ${key}`);
  }
  const url = new URL(config.bridge_url);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("OpenCode requires a local HTTP bridge");
  }
  if (url.username || url.password || url.search || url.hash) throw new Error("Invalid bridge URL");
  if (!Number.isInteger(config.max_turns) || config.max_turns < 1) throw new Error("Invalid max_turns");
  if (!Number.isInteger(config.max_tokens) || config.max_tokens < 1) throw new Error("Invalid max_tokens");
  if (!Number.isFinite(config.model_timeout_sec ?? 300) || (config.model_timeout_sec ?? 300) <= 0) throw new Error("Invalid model_timeout_sec");
  if (!Array.isArray(config.tools)) throw new Error("tools must be an array");
  const names = new Set();
  for (const item of config.tools) {
    const fn = item.function;
    if (item.type !== "function" || !fn || !/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(fn.name)) {
      throw new Error("Invalid function tool");
    }
    if (names.has(fn.name)) throw new Error(`Duplicate tool: ${fn.name}`);
    if (fn.parameters?.type !== "object") throw new Error(`Expected object schema: ${fn.name}`);
    names.add(fn.name);
  }
  return config;
}

export function normalizeMessages(entries, system) {
  const messages = [{ role: "system", content: system }];
  for (const { info, parts = [] } of entries) {
    if (!["user", "assistant"].includes(info.role)) continue;
    const content = parts.filter((part) => part.type === "text").map((part) => part.text).join("\n");
    const calls = parts.filter((part) => part.type === "tool");
    const message = { role: info.role, content };
    if (calls.length) {
      message.tool_calls = calls.map((part) => ({
        id: part.callID,
        type: "function",
        function: { name: part.tool, arguments: JSON.stringify(part.state.input ?? {}) },
      }));
    }
    messages.push(message);
    for (const part of calls) {
      messages.push({
        role: "tool",
        tool_call_id: part.callID,
        name: part.tool,
        content: part.state.output ?? part.state.error ?? `[${part.state.status}]`,
      });
    }
  }
  return messages;
}

function pluginSource(configPath, eventsPath, turnPath) {
  // This file deliberately imports no third-party code. JSON schemas pass
  // through the 1.18.28 tool.definition hook without a lossy Zod conversion.
  return `import { readFileSync, appendFileSync, writeFileSync } from "node:fs";
import { requestJSON, RUNTIME_REQUEST_TIMEOUT_MS } from ${JSON.stringify(new URL("./local_http.mjs", import.meta.url).href)};
const config = JSON.parse(readFileSync(${JSON.stringify(configPath)}, "utf8"));
const definitions = new Map(config.tools.map(item => [item.function.name, item.function]));
const record = event => appendFileSync(${JSON.stringify(eventsPath)}, JSON.stringify(event) + "\\n");
let turns = 0;
export default async () => ({
  event: async ({ event }) => record(event),
  tool: Object.fromEntries([...definitions].map(([name, fn]) => [name, {
    description: fn.description ?? "",
    args: fn.parameters.properties ?? {},
    execute: async (args, context) => {
      const result = await requestJSON(config.bridge_url.replace(/\\/$/, "") + "/tool", {
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + config.bridge_token },
        body: { name, arguments: args },
        signal: context.abort,
        timeoutMs: RUNTIME_REQUEST_TIMEOUT_MS,
        label: "Tool bridge",
      });
      if (typeof result.output !== "string") throw new Error("Invalid tool bridge output");
      return result.output;
    },
  }])),
  "tool.definition": async ({ toolID }, output) => {
    const fn = definitions.get(toolID);
    if (fn) output.jsonSchema = fn.parameters;
  },
  "tool.execute.before": async ({ tool }) => {
    if (!definitions.has(tool)) throw new Error("Host tool disabled: " + tool);
  },
  "experimental.chat.system.transform": async (_input, output) => {
    output.system.splice(0, output.system.length, config.system);
  },
  "chat.params": async (input, output) => {
    if (input.agent !== ${JSON.stringify(AGENT)}) throw new Error("Auxiliary model calls disabled");
    if (turns >= config.max_turns) throw new Error("REPO2RLENV_MAX_TURNS");
    turns += 1;
    writeFileSync(${JSON.stringify(turnPath)}, JSON.stringify({ turns }));
    record({ type: "repo2rlenv.model_turn", properties: { turns, max_tokens: config.max_tokens, inference_options: config.inference_options ?? {}, model_timeout_sec: config.model_timeout_sec ?? 300 } });
    output.maxOutputTokens = config.max_tokens;
    output.options = config.inference_options?.thinking ? {
      thinking: config.inference_options.thinking,
      effort: config.inference_options.output_config?.effort,
    } : {};
  },
});
`;
}

async function prepare(config) {
  // OpenCode has no switch for macOS MDM preferences. Refuse to start if that
  // final config layer exists, since it can replace the isolated provider.
  if (process.platform === "darwin" && [
    path.join("/Library/Managed Preferences", os.userInfo().username, "ai.opencode.managed.plist"),
    "/Library/Managed Preferences/ai.opencode.managed.plist",
  ].some(existsSync)) throw new Error("OpenCode managed preferences prevent an isolated comparison");
  const sessionDir = path.resolve(config.session_dir);
  await mkdir(sessionDir, { recursive: true });
  const isolated = await mkdtemp(path.join(sessionDir, "opencode-"));
  const workDir = path.join(isolated, "work");
  const configDir = path.join(isolated, "config", "opencode");
  const privateHome = path.join(isolated, "home");
  const tempDir = path.join(isolated, "tmp");
  await Promise.all([workDir, configDir, privateHome, tempDir].map((dir) => mkdir(dir, { recursive: true })));
  let binary = process.env.R2E_OPENCODE_BIN;
  if (!binary) {
    const packageFile = require.resolve("opencode-ai/package.json");
    const packageInfo = JSON.parse(await readFile(packageFile, "utf8"));
    binary = path.resolve(path.dirname(packageFile), packageInfo.bin.opencode);
  }
  const runtimeDir = path.dirname(fileURLToPath(import.meta.url));
  // OpenCode checks this dependency even for a dependency-free local plugin.
  // Reuse the explicitly installed, locked packages rather than permitting an
  // implicit npm install during a comparison run.
  const installedPlugin = JSON.parse(await readFile(path.join(runtimeDir, "node_modules", "@opencode-ai", "plugin", "package.json"), "utf8"));
  if (installedPlugin.version !== VERSION) throw new Error(`Install @opencode-ai/plugin@${VERSION}`);
  await symlink(path.join(runtimeDir, "node_modules"), path.join(configDir, "node_modules"), "dir");
  await copyFile(path.join(runtimeDir, "package.json"), path.join(configDir, "package.json"));
  await copyFile(path.join(runtimeDir, "package-lock.json"), path.join(configDir, "package-lock.json"));
  const bridgeConfig = path.join(isolated, "bridge.json");
  await writeFile(bridgeConfig, JSON.stringify(config), { mode: 0o600 });
  const pluginPath = path.join(isolated, "bridge-plugin.mjs");
  const eventsPath = path.join(sessionDir, "opencode-events.jsonl");
  const turnPath = path.join(isolated, "turns.json");
  await writeFile(pluginPath, pluginSource(bridgeConfig, eventsPath, turnPath), { mode: 0o600 });
  const permission = { "*": "deny", ...Object.fromEntries(config.tools.map(({ function: fn }) => [fn.name, "allow"])) };
  const model = `${PROVIDER}/${config.model}`;
  const opencodeConfig = {
    model,
    small_model: model,
    enabled_providers: [PROVIDER],
    provider: {
      [PROVIDER]: {
        npm: "@ai-sdk/anthropic",
        name: "Repo2RLEnv local bridge",
        options: {
          baseURL: `${config.bridge_url.replace(/\/$/, "")}/v1`,
          apiKey: config.bridge_token,
          headers: { Authorization: `Bearer ${config.bridge_token}` },
          timeout: (config.model_timeout_sec ?? 300) * 1000,
        },
        models: {
          [config.model]: {
            name: config.model,
            tool_call: true,
            reasoning: config.inference_options?.thinking?.type === "adaptive",
            limit: { context: 200000, output: config.max_tokens },
            cost: { input: 0, output: 0 },
          },
        },
      },
    },
    default_agent: AGENT,
    agent: {
      build: { disable: true }, plan: { disable: true }, general: { disable: true },
      explore: { disable: true }, title: { disable: true }, summary: { disable: true },
      compaction: { disable: true },
      [AGENT]: { mode: "primary", model, prompt: config.system, permission },
    },
    permission,
    plugin: [pathToFileURL(pluginPath).href],
    mcp: {},
    instructions: [],
    skills: { paths: [], urls: [] },
    share: "disabled",
    autoupdate: false,
    snapshot: false,
    lsp: false,
    formatter: false,
    compaction: { auto: false, prune: false },
    experimental: { batch_tool: false, openTelemetry: false },
  };
  const opencodeConfigPath = path.join(configDir, "opencode.json");
  await writeFile(opencodeConfigPath, JSON.stringify(opencodeConfig), { mode: 0o600 });
  const password = randomBytes(24).toString("hex");
  // Use OpenCode's home override; do not change the user's HOME or inherit
  // provider keys, GitHub credentials, proxy settings, or OpenCode config.
  const env = {
    PATH: process.env.PATH ?? "/usr/bin:/bin",
    LANG: "en_US.UTF-8",
    XDG_CONFIG_HOME: path.join(isolated, "config"),
    XDG_DATA_HOME: path.join(isolated, "data"),
    XDG_CACHE_HOME: path.join(isolated, "cache"),
    XDG_STATE_HOME: path.join(isolated, "state"),
    TMPDIR: tempDir,
    OPENCODE_TEST_HOME: privateHome,
    OPENCODE_TEST_MANAGED_CONFIG_DIR: path.join(isolated, "managed"),
    OPENCODE_CONFIG: opencodeConfigPath,
    OPENCODE_DISABLE_PROJECT_CONFIG: "true",
    OPENCODE_DISABLE_CLAUDE_CODE: "true",
    OPENCODE_DISABLE_EXTERNAL_SKILLS: "true",
    OPENCODE_DISABLE_DEFAULT_PLUGINS: "true",
    OPENCODE_DISABLE_AUTOUPDATE: "true",
    OPENCODE_DISABLE_MODELS_FETCH: "true",
    OPENCODE_DISABLE_AUTOCOMPACT: "true",
    OPENCODE_DISABLE_PRUNE: "true",
    OPENCODE_DISABLE_LSP_DOWNLOAD: "true",
    OPENCODE_DISABLE_FFF: "true",
    OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER: "true",
    OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX: String(config.max_tokens),
    OPENCODE_SERVER_PASSWORD: password,
    OPENCODE_SERVER_USERNAME: "repo2rlenv",
    NO_COLOR: "1",
  };
  return { binary, env, password, isolated, workDir, eventsPath, turnPath };
}

function signalGroup(child, signal) {
  if (!child?.pid) return;
  try {
    // The adapter owns this detached process group, including any children.
    process.kill(process.platform === "win32" ? child.pid : -child.pid, signal);
  } catch (error) {
    // macOS can report EPERM (rather than ESRCH) while a detached group is
    // disappearing. Ignore it only after checking that no group member exists.
    if (error.code === "EPERM") {
      const groups = execFileSync("/bin/ps", ["-axo", "pgid=,stat="], { encoding: "utf8", timeout: 2000 });
      const live = groups.trim().split("\n").some((line) => {
        const [group, status] = line.trim().split(/\s+/);
        return group === String(child.pid) && !status.startsWith("Z");
      });
      if (!live) return;
    }
    if (error.code !== "ESRCH") throw error;
  }
}

export async function runOpenCode(rawConfig) {
  const config = validateConfig(rawConfig);
  if (process.platform === "win32") throw new Error("OpenCode adapter process cleanup requires macOS or Linux");
  const context = await prepare(config);
  const record = (event) => appendFileSync(context.eventsPath, `${JSON.stringify(event)}\n`);
  const abort = new AbortController();
  let child;
  let serverURL;
  let session;
  let exited = false;
  let cancelled = false;
  let entries = [];
  let turns = 0;
  let result;
  let failure;
  const cancel = () => {
    cancelled = true;
    abort.abort();
    try { signalGroup(child, "SIGTERM"); }
    catch (error) { record({ type: "repo2rlenv.cleanup_error", properties: { error: describeError(error) } }); }
  };
  process.on("SIGINT", cancel);
  process.on("SIGTERM", cancel);
  const parentPID = process.ppid;
  const parentWatch = setInterval(() => { if (process.ppid !== parentPID) cancel(); }, 1000);
  parentWatch.unref();
  const auth = `Basic ${Buffer.from(`repo2rlenv:${context.password}`).toString("base64")}`;
  const request = (route, body, signal = abort.signal, timeoutMs = 60000) => requestJSON(`${serverURL}${route}`, {
    body, headers: { Authorization: auth }, signal, timeoutMs, label: "OpenCode",
  });
  try {
    child = spawn(context.binary, ["serve", "--hostname", "127.0.0.1", "--port", "0"], {
      cwd: context.workDir,
      env: context.env,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.once("exit", () => { exited = true; });
    serverURL = await new Promise((resolve, reject) => {
      let startup = "";
      const timer = setTimeout(() => reject(new Error(`OpenCode startup timed out: ${startup.slice(-2000)}`)), 45000);
      const finish = (error, url) => { clearTimeout(timer); error ? reject(error) : resolve(url); };
      const onChunk = (stream) => (chunk) => {
        const text = chunk.toString();
        appendFileSync(path.join(context.isolated, "server.log"), text);
        startup += text;
        if (stream === "stdout") {
          const match = startup.match(/server listening on (http:\/\/127\.0\.0\.1:\d+)/);
          if (match) finish(null, match[1]);
        }
      };
      child.stdout.on("data", onChunk("stdout"));
      child.stderr.on("data", onChunk("stderr"));
      child.once("error", (error) => finish(error));
      child.once("exit", (code, signal) => finish(new Error(`OpenCode exited ${code ?? signal}: ${startup.slice(-2000)}`)));
      abort.signal.addEventListener("abort", () => finish(new Error("OpenCode cancelled")), { once: true });
    });
    record({ type: "repo2rlenv.server", properties: { pid: child.pid, url: serverURL } });
    const health = await request("/global/health");
    if (health.version !== VERSION) throw new Error(`OpenCode ${VERSION} required; found ${health.version}`);
    session = await request("/session", { title: "Repo2RLEnv matched environment authoring" });
    let response;
    let promptError;
    try {
      response = await request(`/session/${session.id}/message`, {
        agent: AGENT,
        model: { providerID: PROVIDER, modelID: config.model },
        system: config.system,
        parts: [{ type: "text", text: config.prompt }],
      }, abort.signal, RUNTIME_REQUEST_TIMEOUT_MS);
    } catch (error) {
      if (cancelled) throw error;
      promptError = error;
    }
    try {
      entries = await request(`/session/${session.id}/message`);
    } catch (error) {
      if (promptError) throw new Error(`${describeError(promptError)}; transcript retrieval failed: ${describeError(error)}`, { cause: promptError });
      throw error;
    }
    await writeFile(path.join(config.session_dir, "opencode-messages.json"), JSON.stringify(entries, null, 2));
    turns = existsSync(context.turnPath) ? JSON.parse(readFileSync(context.turnPath, "utf8")).turns : 0;
    const modelError = response?.info?.error ?? entries.findLast((entry) => entry.info.error)?.info.error;
    const errorText = promptError ? describeError(promptError) : (modelError ? JSON.stringify(modelError) : "");
    if (errorText && !errorText.includes("REPO2RLENV_MAX_TURNS")) throw promptError ?? new Error(errorText);
    // The loop-limit hook creates a local error placeholder without making a
    // provider request. Validate the last actual response, not that marker.
    const lastAssistant = entries.findLast((entry) => entry.info.role === "assistant" &&
      !JSON.stringify(entry.info.error ?? "").includes("REPO2RLENV_MAX_TURNS"));
    if (["length", "max_tokens"].includes(lastAssistant?.info.finish)) {
      throw new Error(`OpenCode provider response truncated: ${lastAssistant.info.finish}`);
    }
    if (!lastAssistant || !lastAssistant.parts.some((part) =>
      (part.type === "text" && part.text.trim()) || part.type === "tool")) {
      throw new Error("OpenCode provider response contained no text or tool calls (thinking-only or empty)");
    }
    result = { messages: normalizeMessages(entries, config.system), turns, cost: 0 };
  } catch (error) {
    failure = error;
  } finally {
    clearInterval(parentWatch);
    try {
      if (serverURL && session && !exited) {
        await request(`/session/${session.id}/abort`, {}, AbortSignal.timeout(1000)).catch(() => {});
      }
      signalGroup(child, "SIGTERM");
      if (child?.pid && !exited) {
        await new Promise((resolve) => {
          const timer = setTimeout(resolve, 1500);
          child.once("exit", () => { clearTimeout(timer); resolve(); });
        });
      }
    } catch (error) {
      failure ??= error;
      record({ type: "repo2rlenv.cleanup_error", properties: { error: describeError(error) } });
    } finally {
      // Attempt the final group kill even if graceful cleanup failed, preserving
      // the original request/provider error in the diagnostic.
      try { signalGroup(child, "SIGKILL"); }
      catch (error) {
        failure ??= error;
        record({ type: "repo2rlenv.cleanup_error", properties: { error: describeError(error) } });
      }
      process.off("SIGINT", cancel);
      process.off("SIGTERM", cancel);
      record({ type: "repo2rlenv.server_stopped", properties: { cancelled } });
    }
  }
  if (failure) {
    const error = new Error(describeError(failure), { cause: failure });
    turns = existsSync(context.turnPath) ? JSON.parse(readFileSync(context.turnPath, "utf8")).turns : turns;
    error.result = { messages: normalizeMessages(entries, config.system), turns, cost: 0, error: error.message, ...(cancelled ? { interrupted: true } : {}) };
    record({ type: "repo2rlenv.error", properties: { error: error.message, turns } });
    throw error;
  }
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (!process.argv[2]) throw new Error("Usage: node opencode.mjs CONFIG_JSON_PATH");
    const config = JSON.parse(await readFile(process.argv[2], "utf8"));
    const result = await runOpenCode(config);
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify(error.result ?? { messages: [], turns: 0, cost: 0, error: describeError(error) })}\n`);
    process.stderr.write(`${error.stack ?? error}\n`);
    process.exitCode = 1;
  }
}
