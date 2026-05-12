# Carbon Voice plugin installed 🎙️

Your PAT is saved. Two more steps and the agent is live.

## 1. Decide who can talk to the bot

By default the bot rejects all senders. Pick one:

**Open it to anyone (dev / personal use):**

```bash
echo 'CARBONVOICE_ALLOW_ALL_USERS=true' >> "$(hermes config env-path)"
```

**Restrict to specific Carbon Voice users:**

```bash
echo 'CARBONVOICE_ALLOWED_USERS=<your_user_guid>,<teammate_guid>' >> "$(hermes config env-path)"
```

Your own `user_guid` prints in the gateway logs on the first startup as `connected as <guid>`.

> 💡 **Prefer a GUI?** `open $(hermes config env-path)` opens the file in your default editor, or run `hermes dashboard` for the web UI at <http://127.0.0.1:9119>.

## 2. (Recommended) Install the Socket.IO client for real-time delivery

Without it the plugin still works — it just polls every 5 seconds instead of getting push events.

```bash
~/.hermes/hermes-agent/venv/bin/python -m ensurepip --upgrade > /dev/null && \
  ~/.hermes/hermes-agent/venv/bin/python -m pip install 'python-socketio[asyncio_client]'
```

## 3. Start the gateway

```bash
hermes gateway run
```

Now DM the agent's Carbon Voice account from anywhere. It reacts with ✅ within a second and replies in-thread.

---

Full docs, troubleshooting, all env vars: <https://github.com/PhononX/hermes-plugin-carbonvoice#readme>
