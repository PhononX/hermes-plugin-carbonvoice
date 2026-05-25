# Carbon Voice plugin installed 🎙️

Your PAT is saved. **One more step before starting the gateway** — install the WebSocket client manually.

## Required: install `python-socketio`

This plugin needs `python-socketio` for real-time delivery. Hermes does **not** auto-install plugin dependencies (security boundary — runtime installs from arbitrary plugin repos bypass the same trust controls that protect Hermes itself), so install it explicitly:

```bash
python -m pip install 'python-socketio[asyncio_client]>=5'
```

## Start the gateway

```bash
hermes gateway run
```

The agent reacts with ✅ within a second and replies in-thread.

### If you skip the install above

The plugin still works — it falls back to **REST polling mode**, fetching new messages every 5 seconds instead of receiving them in real time via WebSocket. You'll see a warning at startup with the install command above.

For personal/dev setups polling is fine. For production where sub-second response matters, install the dep.

---

## Optional: restrict who can talk to the bot

By default the bot accepts messages from any Carbon Voice user (good for personal/dev setups). To limit access to specific people, set their `user_guid`s in your `.env`:

```bash
echo 'CARBONVOICE_ALLOWED_USERS=<your_user_guid>,<teammate_guid>' >> "$(hermes config env-path)"
```

Your own `user_guid` prints in the gateway logs on the first startup as `connected as <guid>`.

> 💡 **Prefer a GUI to edit `.env`?** Run `open $(hermes config env-path)` to open it in your default editor, or `hermes dashboard` for the web UI at <http://127.0.0.1:9119>.

---

Full docs, troubleshooting, all env vars, and mention-gate config: <https://github.com/PhononX/hermes-plugin-carbonvoice#readme>
