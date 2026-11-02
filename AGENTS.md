# joyagent contributor notes

- Python 3.11+, standard library only for the agent runtime. Run from this directory with `python3 -m joyagent`. The optional `tui` channel uses Node.js 22.19+ and the pinned `@earendil-works/pi-tui` package; ordinary CLI/Web/IM/email channels do not require Node.js.
- For TUI changes also run `npm --prefix joyagent/tui test`. Keep its npm lockfile and Python package-data manifest in sync; never auto-install dependencies on a normal launch.
- Run `python3 -m unittest discover -s tests -v` after behavioral changes. Tests must use fake models and temporary directories; never call paid APIs or send real messages.
- `types.py` is the provider-neutral boundary. All channels call `Agent.chat`; do not duplicate the tool loop in channels.
- Treat SOUL.md, policy.toml and server configuration as owner-controlled files. Enforce permissions in code, never just in prompts.
- Built-in file tools stay inside workspace and cannot touch credentials, configuration, skills or memory state. No shell tool by default.
- Keep root example configs in sync with `joyagent/defaults/`. Keep Chinese setup documentation current.
- SQLite is authoritative; JSONL is a recoverable audit mirror. Do not imply that retrieval deletion erases audit history.
