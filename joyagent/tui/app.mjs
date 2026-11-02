import { randomUUID } from "node:crypto";
import {
  Container, Editor, Loader, Markdown, ProcessTerminal, Text, TuiMainScreen,
  matchesKey, setCapabilityOverrides,
} from "@earendil-works/pi-tui";
import { oneLine, parseCommand, safeText, toolSummary } from "./terminal-text.mjs";

const paint = (code) => (text) => `\x1b[${code}m${text}\x1b[0m`;
const color = { accent: paint("36"), dim: paint("90"), bold: paint("1"), green: paint("32"), red: paint("31") };
const theme = {
  heading: color.accent, link: color.accent, linkUrl: color.dim,
  code: paint("33"), codeBlock: (text) => text, codeBlockBorder: color.dim,
  quote: color.dim, quoteBorder: color.dim, hr: color.dim, listBullet: color.accent,
  bold: color.bold, italic: paint("3"), strikethrough: paint("9"), underline: paint("4"),
};
const editorTheme = {
  borderColor: color.accent,
  selectList: { selectedPrefix: color.accent, selectedText: color.bold, description: color.dim, scrollInfo: color.dim, noMatch: color.dim },
};

// Strip controls once before parsing, and once after parsing; only our SGR styles survive.
export class SafeMarkdown extends Markdown {
  constructor(value) { super(safeText(value), 1, 0, theme); }
  render(width) { return super.render(width).map((line) => safeText(line, { styles: true })); }
}

const commands = [
  { value: "/help", label: "/help", description: "查看快捷键和命令" },
  { value: "/session", label: "/session NAME", description: "恢复或切换会话" },
  { value: "/new", label: "/new", description: "开始新会话" },
  { value: "/clear", label: "/clear", description: "清空显示，保留记忆" },
  { value: "/exit", label: "/exit", description: "退出" },
];

// Slash commands only: no implicit filesystem inspection while completing text.
const commandCompletion = {
  triggerCharacters: ["/"],
  async getSuggestions(lines, row, column) {
    if (row !== 0 || lines.length !== 1) return null;
    const prefix = lines[0].slice(0, column);
    if (!/^\/\w*$/.test(prefix)) return null;
    const items = commands.filter((item) => item.value.startsWith(prefix));
    return items.length ? { items, prefix } : null;
  },
  applyCompletion(lines, row, column, item, prefix) {
    const updated = [...lines];
    const inserted = item.value + (item.value === "/session" ? " " : "");
    updated[row] = updated[row].slice(0, column - prefix.length) + inserted + updated[row].slice(column);
    return { lines: updated, cursorLine: row, cursorCol: column - prefix.length + inserted.length };
  },
};

export function createApplication({ client, terminal = new ProcessTerminal(), initialSession = "default" }) {
  setCapabilityOverrides({ hyperlinks: false });
  const tui = new TuiMainScreen(terminal);
  const header = new Text("", 1, 1);
  const transcript = new Container();
  const pending = new Container();
  const editor = new Editor(tui, editorTheme, { paddingX: 1 });
  const footer = new Text("", 1, 0);
  let session = initialSession;
  let info = { provider: "…", model: "…" };
  let busy = null;
  let cancelling = false;
  let closing = false;
  let started = false;
  let closePromise;
  let loader;
  let finish;
  const finished = new Promise((resolve) => { finish = resolve; });
  for (const component of [header, transcript, pending, editor, footer]) tui.addChild(component);
  tui.setFocus(editor);
  editor.setAutocompleteProvider(commandCompletion);

  function refreshHeader() {
    header.setText(`${color.bold("joyagent")}  ${color.dim("/ pi-tui")}\n${color.dim("模型")} ${oneLine(info.provider, 50)} / ${oneLine(info.model, 100)}  ${color.dim("会话")} ${oneLine(session, 100)}`);
  }

  function note(text, error = false) {
    if (closing) return;
    transcript.addChild(new Text((error ? color.red : color.dim)(safeText(text)), 1, 1));
    tui.requestRender();
  }

  function addMessage(role, content) {
    if (closing || !content) return;
    transcript.addChild(new Text(role === "user" ? color.accent("你") : color.bold("joyagent"), 1, 1));
    transcript.addChild(role === "user" ? new Text(safeText(content), 1, 0) : new SafeMarkdown(content));
  }

  function setBusy(value, message = "") {
    loader?.stop();
    pending.clear();
    loader = undefined;
    busy = value;
    editor.disableSubmit = Boolean(value);
    if (value && !closing) {
      loader = new Loader(tui, color.accent, color.dim, message);
      pending.addChild(loader);
    }
    footer.setText(color.dim(value === "chat" ? "正在等待完整回复 · Ctrl+C / Esc 取消" : value ? "正在加载 · Ctrl+C 退出" : "Enter 发送 · Shift+Enter / Alt+Enter 换行 · ↑↓ 历史 · /help · Ctrl+C 退出"));
    if (!closing) tui.requestRender();
  }

  async function loadSession(nextSession) {
    setBusy("history", "正在读取会话…");
    try {
      const result = await client.request("history", { session: nextSession });
      if (closing) return;
      if (!result || !Array.isArray(result.messages)) throw new Error("会话历史格式错误。");
      session = nextSession;
      transcript.clear();
      for (const message of result.messages) {
        if (["user", "assistant"].includes(message.role) && typeof message.content === "string") {
          addMessage(message.role, message.content);
          if (message.role === "user") editor.addToHistory(safeText(message.content));
        }
      }
      refreshHeader();
      note(result.messages.length ? "已恢复会话。" : "开始聊天；/help 查看命令。");
    } catch (error) { note(error.message, true); }
    finally { if (!closing) setBusy(null); }
  }

  async function submit(rawText) {
    if (busy || closing) return;
    const text = safeText(rawText).trim();
    if (!text) return;
    if ([...text].length > 12000) {
      editor.setText(text);
      note("输入不能超过 12000 个字符；内容已留在编辑框中，请缩短后重试。", true);
      return;
    }
    editor.addToHistory(text);
    editor.setText("");
    const command = parseCommand(text);
    if (command) {
      if (command.name === "exit" || command.name === "quit") return shutdown();
      if (command.name === "help") {
        note("/session NAME  恢复或创建会话（与 CLI 共用）\n/new  开始新会话\n/clear  只清空屏幕中的对话显示，保留记忆和审计记录\n/exit  退出\n\nEnter 发送；Shift+Enter / Alt+Enter 换行（取决于终端）；↑↓ 浏览输入历史。\n回答完成后显示工具执行摘要。Ctrl+C / Esc 取消当前请求；再次 Ctrl+C 退出。");
      } else if (command.name === "clear") {
        transcript.clear();
        note("已清空显示；会话记忆和审计记录保留。");
      } else if (command.name === "new") {
        await loadSession(`tui-${randomUUID().slice(0, 8)}`);
      } else if (command.name === "session") {
        if (!command.argument) note(`当前会话：${oneLine(session)}\n用法：/session NAME`);
        else await loadSession(command.argument);
      } else note(`未知命令 /${oneLine(command.name)}；输入 /help 查看帮助。`, true);
      return;
    }
    addMessage("user", text);
    cancelling = false;
    setBusy("chat", "正在思考与执行工具…");
    const startedAt = Date.now();
    try {
      const result = await client.request("chat", { text, session });
      if (closing) return;
      if (!result || typeof result.text !== "string") throw new Error("回答格式错误。");
      const tools = toolSummary(result.tool_events);
      if (tools) note(tools);
      if (result.text) addMessage("assistant", result.text);
      if (result.cancelled) note("请求已取消。已完成的工具操作和已保存记录可能仍然保留。");
      else note(`${result.stopped ? "本轮已停止" : "已完成"} · ${Number.isInteger(result.steps) ? result.steps : "?"} 步 · ${((Date.now() - startedAt) / 1000).toFixed(1)} s`);
    } catch (error) { note(error.message, true); }
    finally {
      cancelling = false;
      if (!closing) setBusy(null);
    }
  }

  async function cancelOrExit() {
    if (closing) return;
    if (busy !== "chat" || cancelling) return shutdown();
    cancelling = true;
    loader?.setMessage("正在取消… 再次 Ctrl+C 退出");
    try {
      const result = await client.request("cancel");
      if (!result?.cancelled && busy === "chat") loader?.setMessage("正在接收已完成的回复…");
    } catch (error) { note(error.message, true); }
  }

  function shutdown(code = 0, error = null) {
    if (closePromise) return closePromise;
    closing = true;
    loader?.stop();
    editor.disableSubmit = true;
    closePromise = (async () => {
      // Return the terminal to normal immediately, even if MCP cleanup takes several seconds.
      try { if (started) await terminal.drainInput(200, 30); } catch {}
      try { if (started) tui.stop(); }
      catch (stopError) {
        error ??= stopError;
        code ||= 1;
        try { terminal.stop(); } catch {}
      }
      try { await client.close(); }
      catch (closeError) { error ??= closeError; code ||= 1; }
      finally { finish({ code, error }); }
    })();
    return closePromise;
  }

  editor.onSubmit = (text) => { submit(text).catch((error) => shutdown(1, error)); };
  // Bracketed paste can contain control codes; never let pasted escapes become rendered instructions.
  editor.onChange = () => {
    const current = editor.getExpandedText();
    const clean = safeText(current);
    if (clean !== current) editor.setText(clean);
  };
  tui.addInputListener((data) => {
    if (closing) return { consume: true };
    if (matchesKey(data, "ctrl+c") || (busy === "chat" && matchesKey(data, "escape"))) {
      cancelOrExit().catch((error) => shutdown(1, error));
      return { consume: true };
    }
  });
  client.on("fatal", (error) => { shutdown(1, error); });

  async function start() {
    try {
      refreshHeader();
      setBusy("startup", "正在连接 Python Agent…");
      // Mark first so a partially successful terminal.start is also restored on failure.
      started = true;
      tui.start();
      info = await client.ready;
      refreshHeader();
      if (!closing) await loadSession(session);
    } catch (error) { await shutdown(1, error); }
  }

  return { start, submit, shutdown, cancelOrExit, finished, tui, editor, transcript,
    get session() { return session; }, get busy() { return busy; } };
}
