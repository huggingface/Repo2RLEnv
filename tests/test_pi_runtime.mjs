// Integration checks against the real pinned Pi SDK and a local Anthropic SSE
// bridge. No model credentials, paid API requests, or cloud operations are used.
// Run: node --test tests/test_pi_runtime.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const adapter = fileURLToPath(new URL("../src/repo2rlenv/curation/runtimes/pi.mjs", import.meta.url));
const token = "local-mock-bridge-token";
const tool = {
  type: "function",
  function: {
    name: "remote_exec", description: "Execute in the remote sandbox",
    parameters: { type: "object", properties: { command: { type: "string" } }, required: ["command"], additionalProperties: false },
  },
};

function replySSE(response, model, { name, args = { command: "echo remote" }, text = "Finished remotely." } = {}) {
  response.writeHead(200, { "Content-Type": "text/event-stream" });
  const emit = (type, fields) => response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
  emit("message_start", { message: { id: "msg_mock", type: "message", role: "assistant", model, content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 5, output_tokens: 0 } } });
  emit("content_block_start", { index: 0, content_block: name ? { type: "tool_use", id: "tool_mock", name, input: {} } : { type: "text", text: "" } });
  emit("content_block_delta", { index: 0, delta: name ? { type: "input_json_delta", partial_json: JSON.stringify(args) } : { type: "text_delta", text } });
  emit("content_block_stop", { index: 0 });
  emit("message_delta", { delta: { stop_reason: name ? "tool_use" : "end_turn", stop_sequence: null }, usage: { output_tokens: 10 } });
  emit("message_stop", {});
  response.end();
}

async function scenario(options = {}) {
  const dir = await mkdtemp(join(tmpdir(), "repo2reward-pi-test-"));
  const requests = [];
  const effects = [];
  let child;
  let handlerError;
  const server = createServer(async (request, response) => {
    try {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString());
      assert.ok(request.headers["x-api-key"] === token || request.headers.authorization === `Bearer ${token}`);
      if (request.url.startsWith("/v1/messages")) {
        requests.push(body);
        if (options.apiError) {
          response.writeHead(402, { "Content-Type": "application/json" });
          response.end(JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "budget exhausted" } }));
          return;
        }
        replySSE(response, body.model, requests.length === 1 || options.repeatTools
          ? { name: options.requestTool ?? options.tools?.[0]?.function.name ?? tool.function.name, ...(options.toolArgs ? { args: options.toolArgs } : {}) }
          : {});
      } else if (request.url === "/tool") {
        effects.push(body);
        if (options.cancel) {
          child.kill("SIGTERM");
          return;
        }
        if (options.delayedTool) {
          await new Promise((resolve) => setTimeout(resolve, 250));
          assert.equal(child.exitCode, null, "Pi exited before its final tool completed");
        }
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ output: "remote output" }));
      } else {
        throw new Error(`Unexpected request: ${request.url}`);
      }
    } catch (error) {
      handlerError = error;
      response.writeHead(500);
      response.end(String(error));
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    await mkdir(join(dir, ".pi", "extensions"), { recursive: true });
    await writeFile(join(dir, "AGENTS.md"), "UNWANTED_PROJECT_CONTEXT");
    await writeFile(join(dir, ".pi", "extensions", "unwanted.mjs"), "throw new Error('UNWANTED_EXTENSION_LOADED');");
    const config = {
      model: "claude-mock-not-in-any-catalog",
      system: "The exact shared system prompt.",
      prompt: "Inspect the remote environment.",
      bridge_url: `http://127.0.0.1:${server.address().port}`,
      bridge_token: token,
      tools: options.tools ?? [tool],
      session_dir: dir,
      max_turns: options.maxTurns ?? 3,
      max_tokens: 6000,
    };
    const configPath = join(dir, "config.json");
    await writeFile(configPath, JSON.stringify(config));
    const env = { PATH: process.env.PATH, PI_OFFLINE: "1" };
    child = spawn(process.execPath, [adapter, configPath], { cwd: dir, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    const timeout = setTimeout(() => child.kill("SIGKILL"), 20000);
    const exitCode = await new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code) => resolve(code));
    }).finally(() => clearTimeout(timeout));
    if (handlerError) throw handlerError;
    assert.ok(stdout.trim(), `No adapter result: ${stderr}`);
    const result = JSON.parse(stdout.trim());
    const events = await readFile(join(dir, "pi-events.jsonl"), "utf8");
    assert.ok(!events.includes(token), "Bridge token leaked into events");
    const sessions = (await readdir(dir)).filter((name) => name.endsWith(".jsonl") && name !== "pi-events.jsonl");
    return { result, requests, effects, events, sessions, config, exitCode, stderr };
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  }
}

test("Pi uses the exact model, prompt, and remote tool and persists its transcript", { timeout: 30000 }, async () => {
  const run = await scenario();
  assert.equal(run.exitCode, 0, JSON.stringify(run.result));
  assert.equal(run.result.turns, 2);
  assert.equal(run.result.cost, 0);
  assert.deepEqual(run.effects, [{ name: "remote_exec", arguments: { command: "echo remote" } }]);
  assert.equal(run.result.messages.at(-1).content, "Finished remotely.");
  assert.equal(run.result.messages.find((message) => message.role === "tool").content, "remote output");
  assert.equal(run.sessions.length, 1);
  assert.ok(run.events.includes('"type":"tool_execution_end"'));
  for (const request of run.requests) {
    assert.equal(request.model, run.config.model);
    assert.equal(request.max_tokens, 6000);
    assert.equal(request.stream, true);
    assert.deepEqual(request.tools.map((entry) => entry.name), ["remote_exec"]);
    assert.equal(request.system.map((block) => block.text).join("\n"), run.config.system);
  }
});

test("Pi honors max_turns after executing the last remote tool batch", { timeout: 30000 }, async () => {
  const run = await scenario({ repeatTools: true, maxTurns: 2 });
  assert.equal(run.exitCode, 0, JSON.stringify(run.result));
  assert.equal(run.requests.length, 2);
  assert.equal(run.effects.length, 2);
  assert.equal(run.result.turns, 2);
});

test("Pi cannot execute a builtin omitted from the bridge tool allowlist", { timeout: 30000 }, async () => {
  const run = await scenario({ requestTool: "bash" });
  assert.equal(run.exitCode, 0, JSON.stringify(run.result));
  assert.equal(run.effects.length, 0);
  assert.match(run.result.messages.find((message) => message.role === "tool").content, /not found/i);
});

test("Pi routes an explicitly supplied builtin name to the bridge", { timeout: 30000 }, async () => {
  const run = await scenario({ tools: [{ ...tool, function: { ...tool.function, name: "bash" } }] });
  assert.equal(run.exitCode, 0, JSON.stringify(run.result));
  assert.equal(run.effects[0].name, "bash");
});

test("Pi reports provider budget errors without retrying", { timeout: 30000 }, async () => {
  const run = await scenario({ apiError: true });
  assert.equal(run.exitCode, 1);
  assert.match(run.result.error, /budget exhausted/);
  assert.equal(run.requests.length, 1);
});

test("Pi cancellation interrupts a pending remote tool and retains the result", { timeout: 30000 }, async () => {
  const run = await scenario({ cancel: true });
  assert.equal(run.exitCode, 143, JSON.stringify(run.result));
  assert.equal(run.result.interrupted, "SIGTERM");
  assert.match(run.result.error, /interrupted/);
  assert.equal(run.requests.length, 1);
  assert.ok(run.events.includes('"type":"adapter_error"'));
});

test("Pi awaits the final validation callback before stopping at max_turns", { timeout: 30000 }, async () => {
  const run = await scenario({
    maxTurns: 1,
    delayedTool: true,
    toolArgs: {},
    tools: [{ type: "function", function: { name: "validate_candidate", description: "Validate remotely", parameters: { type: "object", properties: {}, additionalProperties: false } } }],
  });
  assert.equal(run.exitCode, 0, JSON.stringify(run.result));
  assert.equal(run.requests.length, 1);
  assert.equal(run.effects.length, 1);
  assert.equal(run.effects[0].name, "validate_candidate");
  assert.deepEqual(run.effects[0].arguments, {});
  assert.equal(run.result.messages.at(-1).content, "remote output");
  assert.ok(run.events.indexOf('"type":"tool_execution_end"') < run.events.indexOf('"type":"adapter_end"'));
});
