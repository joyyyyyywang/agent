import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";
import { BridgeClient, MAX_REQUEST_BYTES } from "../bridge-client.mjs";
import { createApplication, SafeMarkdown } from "../app.mjs";
import { parseCommand, safeText, toolSummary } from "../terminal-text.mjs";

const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function fakeProcess() {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.requests = [];
  child.kills = [];
  child.stdin = new Writable({ write(chunk, encoding, callback) {
    const request = JSON.parse(String(chunk));
    child.requests.push(request);
    callback();
    child.onRequest?.(request);
  } });
  child.kill = (signal) => { child.kills.push(signal); child.emit("close", null, signal); return true; };
  child.send = (message) => child.stdout.write(JSON.stringify(message) + "\n");
  child.onRequest = (request) => {
    if (request.method === "shutdown") {
      child.send({ id: request.id, ok: true, result: { shutdown: true } });
      setImmediate(() => child.emit("close", 0));
    }
  };
  return child;
}

function fakeBridge(child = fakeProcess(), options = {}) {
  let spawnArguments;
  const client = new BridgeClient({ python: "/fake/python", config: "/fake/config.toml", startupTimeout: 500,
    spawnProcess: (...args) => { spawnArguments = args; return child; }, ...options });
  const failures = [];
  client.on("fatal", (error) => failures.push(error));
  return { client, child, failures, spawnArguments };
}

class FakeTerminal {
  columns = 80;
  rows = 24;
  kittyProtocolActive = false;
  stopped = false;
  output = "";
  start(onInput) { this.onInput = onInput; }
  stop() { this.stopped = true; }
  async drainInput() {}
  write(value) { this.output += value; }
  moveBy() {}
  hideCursor() {}
  showCursor() {}
  clearLine() {}
  clearFromCursor() {}
  clearScreen() {}
  setTitle() {}
  setProgress() {}
  setProgramStatus() {}
}

class FakeClient extends EventEmitter {
  ready = Promise.resolve({ provider: "demo", model: "fake" });
  requests = [];
  constructor(handler) { super(); this.handler = handler; }
  request(method, params = {}) {
    this.requests.push({ method, params });
    return Promise.resolve(this.handler?.(method, params) ?? (method === "history" ? { session: params.session, messages: [] } : {}));
  }
  async close() {}
}

async function application(t, handler) {
  const client = new FakeClient(handler);
  const terminal = new FakeTerminal();
  const app = createApplication({ client, terminal, initialSession: "shared-cli" });
  t.after(() => app.shutdown());
  await app.start();
  return { app, client, terminal, text: () => safeText(app.transcript.render(80).join("\n")) };
}

test("terminal sanitation preserves Chinese/Markdown and removes ANSI, OSC, DCS and C1", () => {
  const dirty = "你好😀\r\n**bold**\t\x1b[31mred\x1b[0m\x1b]52;c;secret\x07\x1bPpayload\x1b\\\x9b2J\x9dtitle\x9c\u202eend\x00";
  assert.equal(safeText(dirty), "你好😀\n**bold**\tredend");
  assert.equal(safeText("before\x1b]unterminated"), "before");
  assert.equal(safeText("\x1b[31mred\x1b[2J\x1b[0m", { styles: true }), "\x1b[31mred\x1b[0m");
});

test("Markdown output never reintroduces cursor or hyperlink controls", () => {
  const markdown = new SafeMarkdown("# 你好\n\n```js\nconsole.log('x');\n```\n[link](https://example.com)\x1b]52;c;secret\x07\x1b[2J");
  for (const width of [10, 40, 80]) {
    const rendered = markdown.render(width).join("\n");
    assert.equal(rendered, safeText(rendered, { styles: true }));
    assert.ok(!rendered.includes("secret"));
    assert.match(safeText(rendered), /你好/);
  }
});

test("tool summaries expose names and outcomes, never tool result content", () => {
  assert.equal(toolSummary([{ name: "read\x1b[2J_file", ok: true, duration_ms: 12.6, result: "private credentials" }, null]), "✓ read_file · 13 ms");
  assert.deepEqual(parseCommand(" /session shared-cli "), { name: "session", argument: "shared-cli" });
  assert.equal(parseCommand("hello /help"), null);
});

test("bridge launches Python without a shell and handles chunked UTF-8 protocol", async () => {
  const { client, child, spawnArguments } = fakeBridge();
  assert.deepEqual(spawnArguments.slice(0, 2), ["/fake/python", ["-m", "joyagent.channels.tui_bridge", "--config", "/fake/config.toml"]]);
  assert.equal(spawnArguments[2].shell, false);
  const ready = Buffer.from(JSON.stringify({ event: "ready", protocol: 1, provider: "测试", model: "fake" }) + "\n");
  for (const byte of ready) child.stdout.write(Buffer.from([byte]));
  assert.equal((await client.ready).provider, "测试");
  const request = client.request("chat", { text: "hello", session: "same-cli" });
  child.send({ id: child.requests[0].id, ok: true, result: { text: "你好" } });
  assert.deepEqual(await request, { text: "你好" });
  await client.close();
  assert.deepEqual(child.kills, []);
});

test("backend startup error reaches the user and stderr is not reflected", async () => {
  const { client, child, failures } = fakeBridge();
  child.stderr.write("do not expose secret-api-key");
  child.send({ event: "error", error: "配置无效，请运行 doctor" });
  await assert.rejects(client.ready, /配置无效/);
  child.emit("close", 1);
  await client.close();
  assert.equal(failures.length, 1);
  assert.ok(!failures[0].message.includes("secret-api-key"));
});

test("a crashed subprocess rejects pending requests without publishing stderr", async () => {
  const { client, child, failures } = fakeBridge();
  child.send({ event: "ready", protocol: 1, provider: "demo", model: "fake" });
  await client.ready;
  const result = client.request("chat", { text: "hello" });
  child.stderr.write("private-secret");
  child.emit("close", 9);
  await assert.rejects(result, /意外退出/);
  assert.ok(!failures[0].message.includes("private-secret"));
  await client.close();
});

test("bridge bounds responses and oversized requests", async () => {
  const { client, child } = fakeBridge(undefined, { maxLineBytes: 150 });
  child.send({ event: "ready", protocol: 1, provider: "demo", model: "fake" });
  await client.ready;
  await assert.rejects(client.request("chat", { text: "x".repeat(MAX_REQUEST_BYTES) }), /大小限制/);
  assert.equal(child.requests.length, 0);
  const response = client.request("history");
  child.stdout.write("x".repeat(151));
  await assert.rejects(response, /大小限制/);
  child.emit("close", 1);
  await client.close();
});

test("bridge handles EPIPE as a connection failure", async () => {
  const { client, child } = fakeBridge();
  child.send({ event: "ready", protocol: 1, provider: "demo", model: "fake" });
  await client.ready;
  const response = client.request("chat", { text: "hello" });
  child.stdin.emit("error", new Error("EPIPE"));
  await assert.rejects(response, /EPIPE/);
  child.emit("close", 1);
  await client.close();
});

test("malformed JSON rejects pending work and clears the bounded buffer", async () => {
  const { client, child } = fakeBridge();
  child.send({ event: "ready", protocol: 1, provider: "demo", model: "fake" });
  await client.ready;
  const response = client.request("history");
  child.stdout.write("not-json\n");
  await assert.rejects(response, /无效 JSON/);
  assert.equal(client.buffer, "");
  assert.equal(client.pending.size, 0);
  child.emit("close", 1);
  await client.close();
});

test("a stalled child is terminated then killed after a bounded grace", async () => {
  const { client, child } = fakeBridge(undefined, { cleanupTimeout: 20 });
  child.send({ event: "error", error: "startup failure" });
  await assert.rejects(client.ready, /startup failure/);
  child.kill = (signal) => {
    child.kills.push(signal);
    if (signal === "SIGKILL") child.emit("close", null, signal);
    return true;
  };
  await client.close();
  assert.deepEqual(child.kills, ["SIGTERM", "SIGKILL"]);
});

test("TUI restores shared history, switches sessions and clears only display", async (t) => {
  const { app, client, text } = await application(t, (method, params) => method === "history" ? {
    session: params.session, messages: [{ role: "user", content: "历史问题" }, { role: "assistant", content: "**历史回答**" }],
  } : undefined);
  assert.equal(app.session, "shared-cli");
  assert.match(text(), /历史回答/);
  await app.submit("/session another");
  assert.equal(app.session, "another");
  assert.deepEqual(client.requests.at(-1), { method: "history", params: { session: "another" } });
  const count = client.requests.length;
  await app.submit("/clear");
  assert.equal(client.requests.length, count);
  assert.doesNotMatch(text(), /历史回答/);
  assert.match(text(), /审计记录保留/);
});

test("chat displays Markdown and tool summaries only after completion", async (t) => {
  const answer = deferred();
  const { app, text, client } = await application(t, (method) => method === "chat" ? answer.promise : undefined);
  const turn = app.submit("你好");
  assert.equal(app.busy, "chat");
  assert.equal(app.editor.disableSubmit, true);
  assert.doesNotMatch(text(), /read_file/);
  assert.deepEqual(client.requests.at(-1), { method: "chat", params: { text: "你好", session: "shared-cli" } });
  answer.resolve({ text: "**回答**\x1b]52;c;hidden\x07", steps: 2, stopped: false,
    tool_events: [{ name: "read_file", ok: true, duration_ms: 5, result: "not-for-display" }], provider_data: { reasoning_content: "private reasoning" } });
  await turn;
  assert.equal(app.busy, null);
  assert.match(text(), /回答/);
  assert.match(text(), /✓ read_file · 5 ms/);
  assert.doesNotMatch(text(), /hidden|not-for-display|private reasoning/);
});

test("oversized editor content stays editable and never calls the Agent", async (t) => {
  const { app, client } = await application(t);
  const count = client.requests.length;
  await app.submit("中".repeat(12001));
  assert.equal(app.editor.getExpandedText(), "中".repeat(12001));
  assert.equal(client.requests.length, count);
});

test("a request error removes the loader and leaves the editor usable", async (t) => {
  const { app, text } = await application(t, (method) => method === "chat" ? Promise.reject(new Error("fake model unavailable")) : undefined);
  await app.submit("hello");
  assert.equal(app.busy, null);
  assert.equal(app.editor.disableSubmit, false);
  assert.match(text(), /fake model unavailable/);
});

test("Ctrl+C cancels a running turn and keeps the TUI usable", async (t) => {
  const answer = deferred();
  const { app, client, text } = await application(t, (method) => method === "chat" ? answer.promise : method === "cancel" ? { cancelled: true } : undefined);
  const turn = app.submit("long request");
  await app.cancelOrExit();
  assert.equal(client.requests.at(-1).method, "cancel");
  answer.resolve({ text: "", steps: 0, cancelled: true, stopped: true, tool_events: [] });
  await turn;
  assert.equal(app.busy, null);
  assert.match(text(), /请求已取消/);
  assert.equal(app.editor.disableSubmit, false);
});

test("terminal restores before waiting for subprocess cleanup", async (t) => {
  const { app, client, terminal } = await application(t);
  const closed = deferred();
  client.close = () => closed.promise;
  const stopping = app.shutdown();
  await tick();
  assert.equal(terminal.stopped, true);
  closed.resolve();
  await stopping;
  assert.equal((await app.finished).code, 0);
});
