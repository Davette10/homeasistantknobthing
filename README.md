# Juno: a private AI assistant for your Jetson Orin Nano

A personal, text-based AI assistant that runs **entirely on your own Jetson Orin Nano**. No cloud AI, and nobody reads your chats.

- ⏰ **Reminders.** "remind me in 20 minutes to check the oven", "every weekday at 8am remind me to take my vitamins". They arrive on your phone as **Discord DMs** with Done / Snooze buttons.
- ✅ **To-do & shopping lists.** "add eggs and milk to groceries", "what's on my list?"
- 🧠 **Memory.** It learns about you as you chat ("my sister Maya's birthday is May 3") and uses what it knows later.
- 💡 **Ideas.** Brainstorms gift ideas, weekend plans, meals, and projects, tailored to you.
- 💬 **Two ways to chat.** A polished web app (installable on your phone's home screen) and Discord DMs. Both share one conversation and one memory.

Its name and personality are yours to change. The default is friendly and casual.

```
 Phone / laptop ──► Web UI (http://jetson:8080) ─┐
                                                 ├─► Assistant service ──► Ollama (qwen3:4b on the GPU)
 Discord app ◄──► Discord bot (DMs) ─────────────┘        │
      ▲                                                   ├─► SQLite: reminders, lists, memories, chat
      └──────────── reminder pushes ◄── scheduler ────────┘
```

---

## Install (about 15 minutes, mostly downloading)

**You need:** a Jetson Orin Nano 8GB (or Super) with **JetPack 6** flashed and internet access.

```bash
git clone https://github.com/davette10/homeasistantknobthing.git assistant
cd assistant
./install.sh
```

The installer:
1. Installs [Ollama](https://ollama.com) with Jetson GPU support and tunes it for 8GB (flash attention, 8-bit KV cache, keeps the model loaded).
2. Asks what to call your assistant, your name, a **web UI password**, and optionally your Discord details.
3. Creates a Python venv, installs dependencies, and downloads the model (`qwen3:4b`, ~2.5GB).
4. Installs a `systemd` service, so it starts on boot and restarts if it crashes.

When it finishes, open **`http://<jetson-ip>:8080`** on your phone or computer (same Wi-Fi) and log in.

> 📱 **Tip:** On your phone, use *Share → Add to Home Screen* (iPhone) or *⋮ → Add to Home screen* (Android). It then opens like a normal app.

---

## Discord setup (for reminders on your phone)

Reminders are pushed to you as Discord DMs, so your phone buzzes even when the web app is closed. You can also chat with the assistant right in Discord.

1. Go to <https://discord.com/developers/applications> → **New Application** → give it your assistant's name.
2. Open the **Bot** tab → **Reset Token** → copy the token. *(Keep it secret.)*
3. Open the **Installation** tab (or **OAuth2 → URL Generator**), select the `bot` scope, and open the generated link to add the bot to **a server you own**. A new empty private server is perfect. The bot needs to share a server with you before it can DM you.
4. In Discord: **Settings → Advanced → Developer Mode: on**. Then right-click your own name → **Copy User ID**.
5. Put both into `.env` on the Jetson:
   ```ini
   DISCORD_TOKEN=paste-token-here
   DISCORD_OWNER_ID=123456789012345678
   ```
6. `sudo systemctl restart assistant`, then send your bot a DM ("hi!").

The bot only listens to **your** user ID. Anyone else who DMs it is ignored.

---

## Things to try

| You say | What happens |
|---|---|
| `remind me to call mom tomorrow at 6pm` | One-time reminder |
| `every weekday at 7:30am remind me to stretch` | Repeating reminder (Mon–Fri) |
| `remind me every other sunday to water the plants` | Every 2 weeks |
| `what reminders do I have?` / `cancel the plant one` | List / cancel |
| `add bread, eggs and coffee to groceries` | Adds 3 items to the *groceries* list |
| `I finished the taxes` | Checks the item off |
| `my dog's name is Biscuit and I'm allergic to shellfish` | Saved to memory |
| `give me date night ideas` | Personalized ideas using what it knows about you |

You can also add, check off, and delete reminders, list items, and memories by hand in the side panel (the ☰ button on mobile).

---

## Configuration

Everything lives in `.env` (created by the installer, and documented in `.env.example`). After editing, run `sudo systemctl restart assistant`.

| Setting | Default | Notes |
|---|---|---|
| `ASSISTANT_NAME` | `Juno` | What it calls itself |
| `USER_NAME` | – | Your first name |
| `TIMEZONE` | `America/New_York` | US Eastern |
| `MODEL` | `qwen3:4b` | See "Choosing a model" below |
| `NUM_CTX` | `8192` | Context window (tokens) |
| `THINK` | `false` | Let qwen3 reason before answering. Smarter but slow on a Jetson |
| `WEB_PORT` | `8080` | |
| `WEB_PASSWORD` | – | Required for the web UI |
| `DISCORD_TOKEN`, `DISCORD_OWNER_ID` | – | Optional, see above |

**Personality:** edit `persona.md` in plain English ("be more sarcastic", "call me captain", "always answer in Spanish") and restart.

### Choosing a model

| Model | Size | Speed on Orin Nano | Notes |
|---|---|---|---|
| `qwen3:4b` *(default)* | 2.5 GB | good | Best at using tools (reminders, lists) reliably |
| `llama3.2:3b` | 2.0 GB | faster | Good fallback, a bit less reliable with tools |
| `qwen3:1.7b` | 1.4 GB | fastest | For the 4GB Nano, or if you want snappier replies |

Switch with `ollama pull <model>`, set `MODEL=` in `.env`, and restart. The model must support **tools** in Ollama.

### Squeezing more speed out of the Jetson

```bash
sudo nvpmodel -m 0          # max performance power mode (MAXN / MAXN SUPER on JetPack 6.2)
sudo jetson_clocks          # pin clocks high
sudo systemctl set-default multi-user.target   # boot without the desktop GUI: frees ~1GB RAM (reboot to apply)
```

---

## Everyday commands

```bash
journalctl -u assistant -f                  # live logs
sudo systemctl restart assistant            # restart after changing .env / persona.md
.venv/bin/python -m assistant check         # health check: Ollama, model, config, test reply
.venv/bin/python -m assistant chat          # chat in the terminal (great over SSH)
git pull && ./install.sh                    # update (safe to re-run, keeps your .env and data)
```

**Your data** lives in `data/assistant.db` (one SQLite file). Back it up by copying it.

### Using it away from home

The web UI is only reachable on your home network, which is the safe default. Discord works anywhere. If you want the web UI on the go too, install [Tailscale](https://tailscale.com) on the Jetson and your phone, then open `http://<jetson-tailscale-name>:8080`. Don't port-forward it to the open internet.

---

## Troubleshooting

- **"I couldn't reach my brain"**: Ollama isn't running or the model isn't pulled. Run `systemctl status ollama` and `ollama list`, then `.venv/bin/python -m assistant check`.
- **Reminders don't show up in Discord**: the bot must share a server with you (step 3 of the Discord setup), and `DISCORD_OWNER_ID` must be *your* ID, not the bot's. Check `journalctl -u assistant` for errors.
- **It said it set a reminder but nothing is listed**: small models occasionally skip a tool call. Say "did you actually set it?" or check the Reminders panel. `qwen3:4b` is much better at this than smaller models.
- **Wrong reminder time**: check `TIMEZONE` in `.env`. It's also helpful to say times explicitly ("tomorrow at 9am").
- **Out of memory / very slow**: switch to headless mode (above), or use a smaller model.
- **Reminders while the Jetson was off**: they're delivered when it comes back up, marked *late*. Repeating reminders skip the missed ones and continue on schedule.

---

## For tinkerers

```
assistant/
  __main__.py     entry point: serve / check / chat
  agent.py        prompt building + Ollama streaming + tool-call loop
  tools.py        the tools the model can call (reminders, lists, memory)
  timeparse.py    "tomorrow at 5pm" -> datetime; repeating schedules (DST-safe)
  scheduler.py    fires due reminders every 10s, fans out to Discord + web
  discord_bot.py  DM chat + reminder buttons
  web.py          FastAPI: login, chat streaming, REST for the side panel, SSE for live reminders
  db.py           SQLite storage
  static/         the web app (plain HTML/CSS/JS, no build step)
persona.md        personality prompt
install.sh        Jetson installer
tests/            pytest suite (uses a fake Ollama, so no GPU needed)
```

Run the tests with `pip install pytest && python -m pytest`.

To add a new ability, write a handler in `tools.py`, add its JSON schema to `TOOL_SPECS`, and mention it in the system prompt in `agent.py`.
