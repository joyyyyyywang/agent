import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { oneLine } from "./terminal-text.mjs";

export const MAX_LINE_BYTES = 16 * 1024 * 1024;
export const MAX_REQUEST_BYTES = 128 * 1024;

async function within(promise, milliseconds) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((resolve) => { timer = setTimeout(() => resolve(null), milliseconds); })]);
  } finally { clearTimeout(timer); }
}

/** A private JSONL connection to the Python Agent, never a shell or model client. */
export class BridgeClient extends EventEmitter {
  constructor({ python, config, spawnProcess = spawn, startupTimeout = 60000,
    cleanupTimeout = 10000, maxLineBytes = MAX_LINE_BYTES }) {
    super();
    this.pending = new Map();
    this.nextId = 0;
    this.buffer = "";
    this.closed = false;
    this.closing = false;
    this.maxLineBytes = maxLineBytes;
    this.cleanupTimeout = cleanupTimeout;
    this.ready = new Promise((resolve, reject) => { this.resolveReady = resolve; this.rejectReady = reject; });
    // A spawn error can arrive before the caller starts awaiting readiness.
    this.ready.catch(() => {});
    this.exited = new Promise((resolve) => { this.resolveExited = resolve; });
    this.child = spawnProcess(python, ["-m", "joyagent.channels.tui_bridge", "--config", config], {
      stdio: ["pipe", "pipe", "pipe"], shell: false, env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });
    this.timer = setTimeout(() => this.fail(new Error("Python 后端启动超时，请检查配置。")), startupTimeout);
    this.child.stdout.setEncoding("utf8");
    this.child.stderr.setEncoding("utf8");
    this.child.stdout.on("data", (chunk) => this.receive(chunk));
    // Drain stderr without reflecting third-party logs (which may contain credentials).
    this.child.stderr.on("data", () => {});
    this.child.stdin.on("error", (error) => this.fail(new Error(`无法写入 Python 后端：${oneLine(error.message)}`)));
    this.child.on("error", (error) => this.fail(new Error(`无法启动 Python 后端：${oneLine(error.message)}`)));
    this.child.on("close", (code, signal) => {
      this.resolveExited(true);
      if (this.closing) this.fail(new Error("Python 后端已关闭。"), false);
      else this.fail(new Error(`Python 后端意外退出（${oneLine(signal ?? code ?? "unknown")}）。请运行 joyagent doctor 检查配置。`));
    });
  }

  receive(chunk) {
    if (this.closed) return;
    this.buffer += chunk;
    let newline;
    while ((newline = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (Buffer.byteLength(line) > this.maxLineBytes) return this.fail(new Error("Python 后端响应超过大小限制。"));
      if (!line.trim()) continue;
      let message;
      try { message = JSON.parse(line); }
      catch { return this.fail(new Error("Python 后端返回了无效 JSON。")); }
      if (!message || typeof message !== "object" || Array.isArray(message)) return this.fail(new Error("Python 后端协议格式错误。"));
      if (message.event === "ready") {
        if (this.info || message.protocol !== 1 || typeof message.provider !== "string" || typeof message.model !== "string") {
          return this.fail(new Error("Python 后端协议版本不兼容。"));
        }
        this.info = { provider: message.provider, model: message.model };
        clearTimeout(this.timer);
        this.resolveReady(this.info);
      } else if (message.event === "error" && typeof message.error === "string") {
        return this.fail(new Error(oneLine(message.error, 2000)));
      } else if (typeof message.id === "string" && typeof message.ok === "boolean") {
        const request = this.pending.get(message.id);
        if (!request) continue;
        this.pending.delete(message.id);
        if (message.ok) request.resolve(message.result);
        else request.reject(new Error(oneLine(message.error, 2000) || "请求失败。"));
      } else {
        return this.fail(new Error("Python 后端返回了无法识别的协议消息。"));
      }
    }
    if (Buffer.byteLength(this.buffer) > this.maxLineBytes) this.fail(new Error("Python 后端响应超过大小限制。"));
  }

  request(method, params = {}) {
    if (this.closed || (this.closing && method !== "shutdown")) return Promise.reject(new Error("Python 后端连接已关闭。"));
    if (this.pending.size >= 8) return Promise.reject(new Error("等待中的请求过多。"));
    const id = String(++this.nextId);
    const line = JSON.stringify({ id, method, params }) + "\n";
    if (Buffer.byteLength(line) > MAX_REQUEST_BYTES) return Promise.reject(new Error("输入超过大小限制。"));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      try {
        this.child.stdin.write(line, (error) => {
          if (error) this.fail(new Error(`无法写入 Python 后端：${oneLine(error.message)}`));
        });
      } catch (error) { this.fail(new Error(`无法写入 Python 后端：${oneLine(error.message)}`)); }
    });
  }

  fail(error, report = true) {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.timer);
    this.buffer = "";
    this.rejectReady(error);
    for (const request of this.pending.values()) request.reject(error);
    this.pending.clear();
    if (report && !this.closing) this.emit("fatal", error);
  }

  close() {
    if (!this.closePromise) this.closePromise = this.performClose();
    return this.closePromise;
  }

  async performClose() {
    this.closing = true;
    clearTimeout(this.timer);
    // The bridge acknowledges only after MCP clients and memory have been closed.
    if (!this.closed) await within(this.request("shutdown").catch(() => null), this.cleanupTimeout);
    this.child.stdin.end();
    if (await within(this.exited, 700)) return;
    this.child.kill("SIGTERM");
    // SIGTERM also asks the bridge to finish runtime/MCP cleanup, including startup failures.
    if (await within(this.exited, this.cleanupTimeout)) return;
    this.child.kill("SIGKILL");
    await within(this.exited, 500);
    this.fail(new Error("Python 后端已关闭。"), false);
  }
}
