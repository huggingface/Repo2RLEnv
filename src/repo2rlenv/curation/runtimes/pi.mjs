/**
 * Pi 0.85.0 adapter. Run: node pi.mjs CONFIG_JSON_PATH
 *
 * The parent owns credentials, billing, and all cloud effects. Only explicitly
 * supplied bridge tools are available; Pi does not discover local resources.
 */
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";
import { requestJSON, RUNTIME_REQUEST_TIMEOUT_MS } from "./local_http.mjs";

function validateConfig(config) {
  for (const field of ["model", "system", "prompt", "bridge_url", "bridge_token", "session_dir"]) {
    if (typeof config[field] !== "string" || !config[field].trim()) {
      throw new Error(`Pi config requires a nonempty ${field}`);
    }
  }
  const bridge = new URL(config.bridge_url);
  if (!["http:", "https:"].includes(bridge.protocol) || bridge.username || bridge.password ||
      bridge.search || bridge.hash || !["", "/"].includes(bridge.pathname)) {
    throw new Error("Pi bridge_url must be an HTTP(S) origin without credentials or a path");
  }
  if (!Number.isSafeInteger(config.max_turns) || config.max_turns < 1) {
    throw new Error("Pi max_turns must be a positive integer");
  }
  const maxTokens = config.max_tokens ?? 16000;
  if (!Number.isSafeInteger(maxTokens) || maxTokens < 1) {
    throw new Error("Pi max_tokens must be a positive integer");
  }
  if (!Number.isFinite(config.model_timeout_sec ?? 300) || (config.model_timeout_sec ?? 300) <= 0) {
    throw new Error("Pi model_timeout_sec must be positive");
  }
  if (!Array.isArray(config.tools)) throw new Error("Pi tools must be an array");
  const names = new Set();
  for (const tool of config.tools) {
    const fn = tool?.function;
    if (tool?.type !== "function" || typeof fn?.name !== "string" ||
        !/^[a-zA-Z0-9_-]{1,64}$/.test(fn.name) || names.has(fn.name) ||
        fn.parameters?.type !== "object") {
      throw new Error("Pi tools require unique OpenAI function schemas with object parameters");
    }
    names.add(fn.name);
  }
  return { bridge, maxTokens };
}

function textContent(content) {
  if (typeof content === "string") return content;
  return (content ?? []).filter((block) => block.type === "text").map((block) => block.text).join("\n");
}

function normalizedMessages(system, messages) {
  const output = [{ role: "system", content: system }];
  for (const message of messages) {
    if (message.role === "user") {
      output.push({ role: "user", content: textContent(message.content) });
    } else if (message.role === "assistant") {
      const toolCalls = message.content.filter((block) => block.type === "toolCall").map((block) => ({
        id: block.id,
        type: "function",
        function: { name: block.name, arguments: JSON.stringify(block.arguments) },
      }));
      output.push({
        role: "assistant",
        content: textContent(message.content),
        ...(toolCalls.length ? { tool_calls: toolCalls } : {}),
      });
    } else if (message.role === "toolResult") {
      output.push({
        role: "tool", tool_call_id: message.toolCallId, name: message.toolName,
        content: textContent(message.content),
      });
    }
  }
  return output;
}

function emptyResourceLoader(createExtensionRuntime, system) {
  const extensions = { extensions: [], errors: [], runtime: createExtensionRuntime() };
  return {
    getExtensions: () => extensions,
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => system,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => {},
    reload: async () => {},
  };
}

export async function runPi(config) {
  const { bridge, maxTokens } = validateConfig(config);
  const inference = config.inference_options ?? {};
  const adaptive = inference.thinking?.type === "adaptive";
  const requestTimeoutMs = (config.model_timeout_sec ?? 300) * 1000;
  const sessionDir = resolve(config.session_dir);
  mkdirSync(sessionDir, { recursive: true });
  const eventsPath = join(sessionDir, "pi-events.jsonl");
  const redact = (value) => JSON.stringify(value, (_key, item) => {
    if (item instanceof Error) return { name: item.name, message: item.message };
    return item;
  }).split(config.bridge_token).join("[bridge-token]");
  const log = (event) => appendFileSync(eventsPath, `${redact(event)}\n`, { mode: 0o600 });
  const controller = new AbortController();
  let session;
  let turns = 0;
  let interrupted;
  const onSignal = (signal) => {
    interrupted = signal;
    controller.abort(new Error(`Pi interrupted by ${signal}`));
    if (session) void session.abort();
  };
  const onSigint = () => onSignal("SIGINT");
  const onSigterm = () => onSignal("SIGTERM");
  process.on("SIGINT", onSigint);
  process.on("SIGTERM", onSigterm);
  const originalOffline = process.env.PI_OFFLINE;
  process.env.PI_OFFLINE = "1";

  // This also rejects redirects, so the local bridge token never follows a
  // redirect to a provider or a URL selected by model output.
  const bridgeFetch = async (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.origin !== bridge.origin || !["/v1/messages", "/tool"].includes(url.pathname)) {
      throw new Error("Pi attempted an HTTP request outside its configured bridge");
    }
    const signals = [controller.signal, AbortSignal.timeout(requestTimeoutMs)];
    if (init.signal) signals.push(init.signal);
    if (input instanceof Request && input.signal) signals.push(input.signal);
    return fetch(input, { ...init, redirect: "error", signal: AbortSignal.any(signals) });
  };

  try {
    let sdk;
    let ai;
    try {
      sdk = await import("@earendil-works/pi-coding-agent");
      ai = await import("@earendil-works/pi-ai");
    } catch (error) {
      throw new Error(
        `Pi SDK import failed; install @earendil-works/pi-coding-agent@0.85.0 and @earendil-works/pi-server@0.85.0 next to pi.mjs: ${error.message}`,
        { cause: error },
      );
    }
    const { createAgentSession, createExtensionRuntime, ModelRuntime, SessionManager, SettingsManager } = sdk;
    const credentials = new ai.InMemoryCredentialStore();
    await credentials.modify("anthropic", async () => ({ type: "api_key", key: config.bridge_token }));
    const modelRuntime = await ModelRuntime.create({
      credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false,
      signal: controller.signal,
    });
    const settingsManager = SettingsManager.inMemory({
      compaction: { enabled: false },
      retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
      enableAnalytics: false,
      enableInstallTelemetry: false,
      enableSkillCommands: false,
      transport: "sse",
    });
    const customTools = config.tools.map(({ function: fn }) => ({
      name: fn.name,
      label: fn.name,
      description: fn.description ?? "",
      parameters: fn.parameters,
      execute: async (_id, args, signal) => {
        const result = await requestJSON(new URL("/tool", bridge), {
          headers: { Authorization: `Bearer ${config.bridge_token}`, "Content-Type": "application/json" },
          body: { name: fn.name, arguments: args },
          signal: signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
          timeoutMs: RUNTIME_REQUEST_TIMEOUT_MS,
          label: `Pi tool bridge ${fn.name}`,
        });
        if (typeof result.output !== "string") throw new Error(`Pi tool bridge ${fn.name} returned no string output`);
        return { content: [{ type: "text", text: result.output }], details: {} };
      },
    }));
    const model = {
      id: config.model,
      name: config.model,
      provider: "anthropic",
      api: "anthropic-messages",
      baseUrl: bridge.origin,
      reasoning: adaptive,
      input: ["text"],
      // Parent computes actual usage/cost and rejects requests over its budget.
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 200000,
      maxTokens,
    };
    ({ session } = await createAgentSession({
      cwd: sessionDir,
      agentDir: sessionDir,
      model,
      modelRuntime,
      thinkingLevel: adaptive ? "medium" : "off",
      noTools: "builtin",
      tools: customTools.map((tool) => tool.name),
      customTools,
      resourceLoader: emptyResourceLoader(createExtensionRuntime, config.system),
      settingsManager,
      sessionManager: SessionManager.create(sessionDir, sessionDir),
    }));
    const activeTools = session.getActiveToolNames();
    if (activeTools.length !== customTools.length ||
        customTools.some((tool) => !activeTools.includes(tool.name) || session.getToolDefinition(tool.name) !== tool)) {
      throw new Error("Pi active tools differ from the supplied bridge tools");
    }
    const piStream = session.agent.streamFunction;
    session.agent.streamFunction = (currentModel, context, options) => {
      if (turns >= config.max_turns) throw new Error("Pi exceeded max_turns");
      turns += 1;
      // Pi appends its local cwd to custom prompts. Supply the exact common
      // system prompt at the provider boundary for the matched comparison.
      return piStream(currentModel, { ...context, systemPrompt: config.system }, {
        ...options, maxTokens, maxRetries: 0, fetch: bridgeFetch,
        // The parent selects the same policy for every runtime. Override Pi's
        // model-catalog defaults without touching signed conversation blocks.
        onPayload: (payload) => {
          for (const key of ["thinking", "temperature", "top_p", "top_k", "output_config"]) delete payload[key];
          Object.assign(payload, inference, { max_tokens: maxTokens });
          return payload;
        },
      });
    };
    session.agent.shouldStopAfterTurn = () => turns >= config.max_turns;
    session.subscribe(log);
    log({ type: "adapter_start", runtime: "pi", model: config.model, tools: activeTools, max_turns: config.max_turns, max_tokens: maxTokens, inference_options: inference, model_timeout_sec: requestTimeoutMs / 1000 });
    controller.signal.throwIfAborted();
    await session.prompt(config.prompt);
    if (interrupted) throw new Error(`Pi interrupted by ${interrupted}`);
    const lastAssistant = session.messages.findLast((message) => message.role === "assistant");
    if (lastAssistant?.stopReason === "error" || lastAssistant?.stopReason === "aborted") {
      throw new Error(`Pi provider failed: ${lastAssistant.errorMessage || lastAssistant.stopReason}`);
    }
    if (["length", "max_tokens"].includes(lastAssistant?.stopReason)) {
      throw new Error(`Pi provider response truncated: ${lastAssistant.stopReason}`);
    }
    if (!lastAssistant || (!textContent(lastAssistant.content).trim() &&
        !(lastAssistant.content ?? []).some((block) => block.type === "toolCall"))) {
      throw new Error("Pi provider response contained no text or tool calls (thinking-only or empty)");
    }
    const result = { messages: normalizedMessages(config.system, session.messages), turns, cost: 0 };
    log({ type: "adapter_end", turns, session_file: session.sessionFile });
    return JSON.parse(redact(result));
  } catch (error) {
    const result = {
      messages: normalizedMessages(config.system, session?.messages ?? []),
      turns, cost: 0, error: error instanceof Error ? error.message : String(error),
      ...(interrupted ? { interrupted } : {}),
    };
    log({ type: "adapter_error", error: result.error, interrupted, turns });
    return JSON.parse(redact(result));
  } finally {
    session?.dispose();
    process.off("SIGINT", onSigint);
    process.off("SIGTERM", onSigterm);
    if (originalOffline === undefined) delete process.env.PI_OFFLINE;
    else process.env.PI_OFFLINE = originalOffline;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    if (process.argv.length !== 3) throw new Error("Usage: node pi.mjs CONFIG_JSON_PATH");
    const result = await runPi(JSON.parse(readFileSync(process.argv[2], "utf8")));
    process.stdout.write(`${JSON.stringify(result)}\n`);
    process.exitCode = result.interrupted === "SIGINT" ? 130 : result.interrupted === "SIGTERM" ? 143 : result.error ? 1 : 0;
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ messages: [], turns: 0, cost: 0, error: error instanceof Error ? error.message : String(error) })}\n`);
    process.exitCode = 1;
  }
}
