# Carbon Voice plugin installed 🎙️

Your PAT is saved. One command left — start the gateway and DM the agent from any Carbon Voice account.

## Start the gateway

```bash
hermes gateway run
```

The agent reacts with ✅ within a second and replies in-thread. On first startup the plugin will auto-install `python-socketio` (for real-time delivery) if it's missing — you'll see a one-line log and then the gateway starts normally.

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
