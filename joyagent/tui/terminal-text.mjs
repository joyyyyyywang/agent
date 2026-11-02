/** Remove terminal instructions from untrusted content, while keeping Chinese and Markdown. */
export function safeText(value, { styles = false } = {}) {
  const source = String(value ?? "").replace(/\r\n?/g, "\n");
  let result = "";
  for (let i = 0; i < source.length; i++) {
    const code = source.charCodeAt(i);
    const escape = code === 0x1b;
    const next = escape ? source.charCodeAt(i + 1) : code;
    const csi = (escape && next === 0x5b) || code === 0x9b;
    if (csi) {
      const start = i;
      i += escape ? 2 : 1;
      while (i < source.length && !(source.charCodeAt(i) >= 0x40 && source.charCodeAt(i) <= 0x7e)) i++;
      const sequence = source.slice(start, i + 1);
      if (styles && /^\x1b\[[0-9;:]*m$/.test(sequence)) result += sequence;
      continue;
    }
    // OSC, DCS, SOS, PM and APC carry arbitrary strings terminated by BEL or ST.
    if ((escape && [0x5d, 0x50, 0x58, 0x5e, 0x5f].includes(next)) ||
        [0x90, 0x98, 0x9d, 0x9e, 0x9f].includes(code)) {
      i += escape ? 2 : 1;
      while (i < source.length) {
        const current = source.charCodeAt(i);
        if (current === 0x07 || current === 0x9c) break;
        if (current === 0x1b && source[i + 1] === "\\") { i++; break; }
        i++;
      }
      continue;
    }
    if (escape) {
      i++;
      while (i < source.length && source.charCodeAt(i) >= 0x20 && source.charCodeAt(i) <= 0x2f) i++;
      continue;
    }
    if ((code < 0x20 && code !== 0x09 && code !== 0x0a) ||
        (code >= 0x7f && code <= 0x9f) ||
        (code >= 0x202a && code <= 0x202e) || (code >= 0x2066 && code <= 0x2069)) continue;
    result += source[i];
  }
  return result;
}

export function oneLine(value, maxLength = 180) {
  return safeText(value).replace(/\s+/g, " ").trim().slice(0, maxLength);
}

export function toolSummary(events) {
  if (!Array.isArray(events)) return "";
  return events.filter((event) => event && typeof event === "object").slice(0, 100).map((event) => {
    const duration = Number.isFinite(event.duration_ms) ? Math.max(0, Math.round(event.duration_ms)) : "?";
    return `${event.ok ? "✓" : "✗"} ${oneLine(event.name, 80)} · ${duration} ms`;
  }).join("\n");
}

export function parseCommand(text) {
  const match = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text.trim());
  return match ? { name: match[1].toLowerCase(), argument: (match[2] ?? "").trim() } : null;
}
