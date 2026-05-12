# Carbon Voice plugin installed 🎙️

Your PAT is saved. Two short steps and the agent is live.

## 1. Install the Socket.IO client *(recommended)*

Without it the plugin still works — it just polls every 5 seconds instead of receiving push events. Hermes ships a uv-managed venv without `pip`, so this bootstraps pip first.

```bash
~/.hermes/hermes-agent/venv/bin/python -m ensurepip --upgrade > /dev/null && ~/.hermes/hermes-agent/venv/bin/python -m pip install 'python-socketio[asyncio_client]'
```

## 2. Start the gateway

```bash
hermes gateway run
```

Now DM the agent's Carbon Voice account from anywhere. It reacts with ✅ within a second and replies in-thread.

---

## Optional: restrict who can talk to the bot

By default the bot accepts messages from any Carbon Voice user (good for personal/dev setups). To limit access to specific people, set their `user_guid`s in your `.env`:

```bash
echo 'CARBONVOICE_ALLOWED_USERS=<your_user_guid>,<teammate_guid>' >> "$(hermes config env-path)"
```

Your own `user_guid` prints in the gateway logs on the first startup as `connected as <guid>`.

> 💡 **Prefer a GUI to edit `.env`?** Run `open $(hermes config env-path)` to open it in your default editor, or `hermes dashboard` for the web UI at <http://127.0.0.1:9119>.

---

Full docs, troubleshooting, all env vars: <https://github.com/PhononX/hermes-plugin-carbonvoice#readme>
