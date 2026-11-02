#!/usr/bin/env node
import { isAbsolute } from "node:path";
import { BridgeClient } from "./bridge-client.mjs";
import { oneLine } from "./terminal-text.mjs";

function argumentsFor(argv) {
  const values = { session: "default" };
  for (let i = 0; i < argv.length; i += 2) {
    const name = argv[i];
    if (!["--python", "--config", "--session"].includes(name) || !argv[i + 1]) throw new Error("请使用 python3 -m joyagent tui 启动。");
    values[name.slice(2)] = argv[i + 1];
  }
  if (!isAbsolute(values.python ?? "") || !isAbsolute(values.config ?? "")) throw new Error("Python 和配置文件必须使用绝对路径。请使用 python3 -m joyagent tui。");
  return values;
}

let app;
let client;
const handlers = new Map();
try {
  const args = argumentsFor(process.argv.slice(2));
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error("TUI 需要交互式终端；管道输入请使用 joyagent chat -m。 ");
  const { createApplication } = await import("./app.mjs");
  client = new BridgeClient(args);
  app = createApplication({ client, initialSession: args.session });
  const watch = (event, handler) => { handlers.set(event, handler); process.on(event, handler); };
  watch("SIGINT", () => { app.cancelOrExit().catch((error) => app.shutdown(1, error)); });
  watch("SIGTERM", () => { app.shutdown(143); });
  watch("SIGHUP", () => { app.shutdown(129); });
  watch("uncaughtException", (error) => { app.shutdown(1, error); });
  watch("unhandledRejection", (error) => { app.shutdown(1, error instanceof Error ? error : new Error(String(error))); });
  await app.start();
  const result = await app.finished;
  if (result.error) process.stderr.write(`joyagent: ${oneLine(result.error.message, 2000)}\n`);
  process.exitCode = result.code;
} catch (error) {
  if (app) await app.shutdown(1, error);
  else if (client) await client.close();
  const message = error.code === "ERR_MODULE_NOT_FOUND" ? "TUI 依赖未安装，请先运行 python3 -m joyagent tui --install。" : error.message;
  process.stderr.write(`joyagent: ${oneLine(message, 2000)}\n`);
  process.exitCode = 1;
} finally {
  for (const [event, handler] of handlers) process.off(event, handler);
}
