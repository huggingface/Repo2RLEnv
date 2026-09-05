import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { normalizeMessages, validateConfig } from "../src/repo2rlenv/curation/runtimes/opencode.mjs";
import { describeError, requestJSON, RUNTIME_REQUEST_TIMEOUT_MS } from "../src/repo2rlenv/curation/runtimes/local_http.mjs";

const adapter = fileURLToPath(new URL("../src/repo2rlenv/curation/runtimes/opencode.mjs", import.meta.url));
const depsReady = existsSync(new URL("../src/repo2rlenv/curation/runtimes/node_modules/@opencode-ai/plugin/package.json", import.meta.url));
const tool = {
  type: "function",
  function: {
    name: "remote_shell",
    description: "Execute a shell command in the remote sandbox.",
    parameters: {
      type: "object",
      properties: { command: { type: "string" }, timeout: { type: "integer", minimum: 1 } },
      required: ["command"],
      additionalProperties: false,
    },
  },
};

function config(directory, bridge_url = "http://127.0.0.1:1") {
  return {
    model: "claude-sonnet-5",
    system: "Test environment author. Only run the supplied remote tools.",
    prompt: "Run the test tool and report its result.",
    bridge_url,
    bridge_token: "test-local-token",
    tools: [tool],
    session_dir: directory,
    max_turns: 3,
    max_tokens: 16000,
    model_timeout_sec: 300,
    inference_options: { thinking: { type: "adaptive" }, output_config: { effort: "medium" } },
  };
}

function streamMessage(response, body, callIndex, alwaysTools = false, validation = false, options = {}) {
  response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  const event = (type, fields) => response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
  event("message_start", { message: {
    id: `msg_${callIndex}`, type: "message", role: "assistant", model: body.model,
    content: [], stop_reason: null, stop_sequence: null,
    usage: { input_tokens: 20, output_tokens: 0 },
  } });
  const useTool = !options.badResponse && (alwaysTools || callIndex <= (options.toolTurns ?? 1));
  let index = 0;
  if (options.opaqueThinking || options.badResponse === "thinking_only") {
    event("content_block_start", { index, content_block: { type: "thinking", thinking: "", signature: "" } });
    event("content_block_delta", { index, delta: { type: "signature_delta", signature: "opaque-signature" } });
    event("content_block_stop", { index });
    index += 1;
    event("content_block_start", { index, content_block: { type: "redacted_thinking", data: "opaque-redacted" } });
    event("content_block_stop", { index });
    index += 1;
  }
  if (useTool) {
    event("content_block_start", { index, content_block: { type: "tool_use", id: `tool_${callIndex}`, name: validation ? "validate_candidate" : "remote_shell", input: {} } });
    event("content_block_delta", { index, delta: { type: "input_json_delta", partial_json: JSON.stringify(validation ? {} : { command: "echo remote-only" }) } });
  } else if (options.badResponse !== "thinking_only") {
    event("content_block_start", { index, content_block: { type: "text", text: "" } });
    event("content_block_delta", { index, delta: { type: "text_delta", text: options.badResponse === "empty" ? "" : "The remote tool returned remote-only." } });
  }
  if (options.badResponse !== "thinking_only") event("content_block_stop", { index });
  event("message_delta", { delta: { stop_reason: options.badResponse === "max_tokens" ? "max_tokens" : useTool ? "tool_use" : "end_turn", stop_sequence: null }, usage: { output_tokens: 10 } });
  event("message_stop", {});
  response.end();
}

async function runMock(t, { alwaysTools = false, cancel = false, apiError = false, validation = false, opaqueThinking = false, badResponse, toolTurns = 1 } = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "repo2rlenv-opencode-test-"));
  t.after(async () => { if (!process.env.R2E_KEEP_OPENCODE_TEST_OUTPUT) await rm(directory, { recursive: true, force: true }); });
  const requests = [];
  const tools = [];
  let validationCompleted = false;
  let modelSeen;
  const modelStarted = new Promise((resolve) => { modelSeen = resolve; });
  const server = http.createServer(async (request, response) => {
    let raw = "";
    for await (const chunk of request) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    if (request.headers.authorization !== "Bearer test-local-token" && request.headers["x-api-key"] !== "test-local-token") {
      response.writeHead(401).end();
      return;
    }
    if (request.url.startsWith("/v1/messages")) {
      requests.push(body);
      modelSeen();
      if (apiError) {
        response.writeHead(402, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "mock provider budget exhausted" } }));
      } else if (!cancel) streamMessage(response, body, requests.length, alwaysTools, validation, { opaqueThinking, badResponse, toolTurns });
    } else if (request.url === "/tool") {
      tools.push(body);
      if (validation) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        assert.equal(child.exitCode, null, "Runtime exited before validation completed");
        validationCompleted = true;
      }
      response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ output: validation ? "final validation completed" : "remote-only" }));
    } else {
      response.writeHead(404).end();
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  const settings = config(directory, `http://127.0.0.1:${server.address().port}`);
  settings.max_turns = validation || badResponse ? 1 : alwaysTools ? 2 : toolTurns + 2;
  if (validation) settings.tools = [{ type: "function", function: { name: "validate_candidate", description: "Validate remotely", parameters: { type: "object", properties: {}, additionalProperties: false } } }];
  await writeFile(path.join(directory, "input.json"), JSON.stringify(settings));
  const child = spawn(process.execPath, [adapter, path.join(directory, "input.json")], { stdio: ["ignore", "pipe", "pipe"] });
  t.after(() => { if (child.exitCode === null) child.kill("SIGKILL"); });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const done = once(child, "exit");
  if (cancel) {
    await Promise.race([modelStarted, done.then(() => { throw new Error(stderr); })]);
    child.kill("SIGTERM");
  }
  const [code] = await done;
  assert.equal(code, cancel || apiError || badResponse ? 1 : 0, `${directory}\n${stderr}\n${stdout}`);
  const events = (await readFile(path.join(directory, "opencode-events.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
  const started = events.find((event) => event.type === "repo2rlenv.server");
  assert.ok(started);
  assert.throws(() => process.kill(started.properties.pid, 0), { code: "ESRCH" });
  await assert.rejects(fetch(`${started.properties.url}/global/health`));
  assert.ok(events.some((event) => event.type === "repo2rlenv.server_stopped"), stderr);
  return { requests, tools, events, result: JSON.parse(stdout), settings, validationCompleted, stderr };
}

test("rejects remote bridges and invalid tool contracts", () => {
  assert.throws(() => validateConfig(config("/tmp", "https://api.anthropic.com")), /local HTTP bridge/);
  assert.throws(() => validateConfig({ ...config("/tmp"), tools: [tool, tool] }), /Duplicate/);
  assert.throws(() => validateConfig({ ...config("/tmp"), max_turns: 0 }), /max_turns/);
});

test("normalizes native OpenCode messages including tool failures", () => {
  const normalized = normalizeMessages([{ info: { role: "assistant" }, parts: [
    { type: "text", text: "Running" },
    { type: "tool", tool: "remote_shell", callID: "call_1", state: { input: { command: "false" }, error: "exit 1", status: "error" } },
  ] }], "system");
  assert.equal(normalized[1].tool_calls[0].function.arguments, '{"command":"false"}');
  assert.deepEqual(normalized[2], { role: "tool", tool_call_id: "call_1", name: "remote_shell", content: "exit 1" });
});

test("local HTTP deadlines are explicit and preserve transport error causes", async (t) => {
  const server = http.createServer((request, response) => {
    if (request.url === "/delayed") setTimeout(() => response.end('{"done":true}'), 50);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal(RUNTIME_REQUEST_TIMEOUT_MS, 3600000);
  assert.deepEqual(await requestJSON(`${base}/delayed`, { timeoutMs: 1000 }), { done: true });
  await assert.rejects(requestJSON(`${base}/never`, { timeoutMs: 20 }), (error) => {
    assert.match(describeError(error), /ABORT_ERR/);
    assert.match(describeError(error), /timeout|timed out/i);
    return true;
  });
  const cause = Object.assign(new Error("Headers Timeout Error"), { code: "UND_ERR_HEADERS_TIMEOUT" });
  assert.match(describeError(new TypeError("fetch failed", { cause })), /fetch failed.*UND_ERR_HEADERS_TIMEOUT.*Headers Timeout Error/);
});

test("OpenCode only sees exact remote tools and shared model settings", { skip: !depsReady, timeout: 60000 }, async (t) => {
  const { requests, tools, events, result, settings } = await runMock(t);
  assert.equal(requests.length, 2);
  assert.equal(result.turns, 2);
  assert.equal(result.cost, 0);
  for (const body of requests) {
    assert.equal(body.model, settings.model);
    assert.equal(body.max_tokens, 16000);
    assert.deepEqual(body.thinking, settings.inference_options.thinking);
    assert.deepEqual(body.output_config, settings.inference_options.output_config);
    assert.equal(body.stream, true);
    assert.ok(!body.tool_choice || body.tool_choice.type === "auto", JSON.stringify(body.tool_choice));
    assert.deepEqual(body.tools.map((entry) => entry.name), [tool.function.name]);
    assert.deepEqual(body.tools[0].input_schema, tool.function.parameters);
    assert.deepEqual(body.system.map((part) => part.text), [settings.system]);
  }
  assert.deepEqual(tools, [{ name: "remote_shell", arguments: { command: "echo remote-only" } }]);
  assert.ok(result.messages.some((message) => message.role === "tool" && message.content === "remote-only"));
  assert.match(result.messages.at(-1).content, /remote-only/);
  assert.ok(events.some((event) => event.type === "message.part.updated"));
});

test("OpenCode permits a text finish after a long sequence of tool calls", { skip: !depsReady, timeout: 60000 }, async (t) => {
  // A live author claimed it had to call defer_candidate at turn 12 because a
  // schema forced a tool. Exercise that turn with the actual pinned runtime.
  const { requests, tools, result } = await runMock(t, { toolTurns: 11 });
  assert.equal(requests.length, 12);
  assert.equal(tools.length, 11);
  assert.equal(result.turns, 12);
  for (const body of requests) {
    assert.ok(!body.tool_choice || body.tool_choice.type === "auto", JSON.stringify(body.tool_choice));
  }
  assert.equal(result.messages.at(-1).role, "assistant");
  assert.equal(result.messages.at(-1).tool_calls, undefined);
  assert.match(result.messages.at(-1).content, /remote-only/);
});

test("OpenCode stops its loop at the configured turn budget", { skip: !depsReady, timeout: 60000 }, async (t) => {
  const { requests, result } = await runMock(t, { alwaysTools: true });
  assert.equal(requests.length, 2);
  assert.equal(result.turns, 2);
});

test("OpenCode cancellation terminates its private server", { skip: !depsReady, timeout: 60000 }, async (t) => {
  const { requests, events, result } = await runMock(t, { cancel: true });
  assert.equal(requests.length, 1);
  assert.equal(events.find((event) => event.type === "repo2rlenv.server_stopped").properties.cancelled, true);
  assert.equal(result.interrupted, true);
  assert.ok(result.error);
});

test("OpenCode exposes provider failure in its stdout result", { skip: !depsReady, timeout: 60000 }, async (t) => {
  const { result, requests, stderr } = await runMock(t, { apiError: true });
  assert.match(result.error, /mock provider budget exhausted/);
  assert.match(stderr, /mock provider budget exhausted/);
  assert.equal(requests.length, 1);
  assert.equal(result.turns, 1);
});

test("OpenCode awaits validation on its final allowed turn", { skip: !depsReady, timeout: 60000 }, async (t) => {
  const { result, requests, tools, validationCompleted, events } = await runMock(t, { validation: true });
  assert.equal(requests.length, 1);
  assert.equal(result.turns, 1);
  assert.equal(validationCompleted, true);
  assert.deepEqual(tools, [{ name: "validate_candidate", arguments: {} }]);
  assert.ok(result.messages.some((message) => message.role === "tool" && message.content === "final validation completed"));
  const finished = events.findIndex((event) => event.type === "message.part.updated" && event.properties.part.tool === "validate_candidate" && event.properties.part.state.status === "completed");
  const stopped = events.findIndex((event) => event.type === "repo2rlenv.server_stopped");
  assert.ok(finished >= 0 && stopped > finished);
});

test("OpenCode roundtrips opaque thinking unchanged", { skip: !depsReady, timeout: 60000 }, async (t) => {
  const { requests, events } = await runMock(t, { opaqueThinking: true });
  const previous = requests[1].messages.find((message) => message.role === "assistant");
  assert.deepEqual(previous.content.filter((block) => ["thinking", "redacted_thinking"].includes(block.type)), [
    { type: "thinking", thinking: "", signature: "opaque-signature" },
    { type: "redacted_thinking", data: "opaque-redacted" },
  ]);
  const policy = events.find((event) => event.type === "repo2rlenv.model_turn").properties;
  assert.equal(policy.max_tokens, 16000);
  assert.equal(policy.model_timeout_sec, 300);
  assert.deepEqual(policy.inference_options, { thinking: { type: "adaptive" }, output_config: { effort: "medium" } });
});

for (const [badResponse, pattern] of [
  ["max_tokens", /truncated/],
  ["empty", /no text or tool calls/],
  ["thinking_only", /no text or tool calls/],
]) {
  test(`OpenCode fails on ${badResponse} provider responses`, { skip: !depsReady, timeout: 60000 }, async (t) => {
    const { result, requests, tools } = await runMock(t, { badResponse });
    assert.match(result.error, pattern);
    assert.equal(requests.length, 1);
    assert.equal(tools.length, 0);
  });
}
