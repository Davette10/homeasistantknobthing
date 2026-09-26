# Juno: a private AI assistant for your Jetson Orin Nano

A personal AI agent in the spirit of Meta's Muse, running **entirely on your own Jetson Orin Nano**. No cloud AI, and nobody reads your chats. It doesn't just answer questions: it keeps you on top of things, turns goals into plans, and checks in on you.

- 🎯 **Goals → action plans.** "I want to run a 5K by November." It asks a question or two, builds a dated step-by-step plan, tracks your progress, and keeps you accountable.
- 🔔 **Check-ins (coach mode).** A morning brief with weather and your day, a midday nudge, and an evening "how'd it go?" It also follows up on overdue steps and ignored reminders, and checks on goals that have gone quiet. It respects quiet hours, and you control how often.
- 🔎 **Web search & weather.** "what time does Costco close?", "will it rain this weekend?"
- ⏰ **Reminders.** "remind me in 20 minutes to check the oven", "every weekday at 8am remind me to take my vitamins". They arrive on your phone as **Discord DMs** with Done / Snooze buttons.
- ✅ **To-do & shopping lists.** "add eggs and milk to groceries", "what's on my list?"
- 🧠 **Memory.** It learns about you as you chat ("my sister Maya's birthday is May 3") and uses what it knows later.
- 💡 **Ideas.** Brainstorms gift ideas, weekend plans, meals, and projects, tailored to you.
- 💬 **Two ways to chat.** A polished web app (installable on your phone's home screen) and Discord DMs. Both share one conversation and one memory.

Its name and personality are yours to change. The default is a friendly, casual coach.

```
 Phone / laptop ──► Web UI (http://jetson:8080) ─┐
                                                 ├─► Assistant service ──► Ollama (qwen3:4b on the GPU)
 Discord app ◄──► Discord bot (DMs) ─────────────┘        │
      ▲                                                   ├─► SQLite: reminders, lists, goals, memories, chat
      ├──────────── reminder pushes ◄── scheduler ────────┤
      └──────────── check-ins & nudges ◄── coach ─────────┘   (+ web search & weather when asked)
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
| `I want to read 12 books this year` | It asks a couple of questions, then saves a goal with a dated plan |
| `I did my run today!` | Checks off the goal step and tells you what's next |
| `what's the weather tomorrow?` / `search for easy weeknight dinners` | Weather / web search |

You can also add, check off, and delete reminders, list items, goal steps, and memories by hand in the side panel (the ☰ button on mobile).

---

## Check-ins: how it reaches out

In **coach** mode (the default), the assistant messages you on its own, in Discord and in the web app:

| When | What |
|---|---|
| **Morning brief** (8:00) | Weather, today's reminders, goal steps due, your to-dos, and a question to help you focus |
| **Midday nudge** (13:00) | A short push on the most important open item. Skipped if there's nothing open |
| **Evening check-in** (20:30) | "How did today go?" about the specific things on your plate. Tell it what's done and it checks things off |
| **Follow-ups** | A goal step whose day has passed ("did you get to it?"), or a reminder you never tapped Done on |
| **Goal check-ins** | A goal with no progress for 3 days gets a gentle "how's it going?" |

Guard rails: nothing during **quiet hours** (22:00–07:30), at most 6 check-ins a day, at least 75 minutes between nudges, and no nudging while you're in the middle of a conversation. Your reminders always come through regardless.

Change all of this in the web app: **⋮ → Check-in settings**. You can pick *Coach*, *Balanced* (morning + evening + follow-ups), *Light* (morning brief + follow-ups), or *Off*, and set the times, quiet hours, and your city for weather.

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
| `COACH_MODE`, `MORNING_TIME`, `MIDDAY_TIME`, `EVENING_TIME`, `QUIET_HOURS` | coach, 08:00, 13:00, 20:30, 22:00-07:30 | Starting values; editable in the web app |
| `WEATHER_LOCATION` | – | Your city, e.g. `Boston, MA` |
| `SEARXNG_URL` | – | Optional self-hosted search, see below |

**Personality:** edit `persona.md` in plain English ("be more sarcastic", "call me captain", "always answer in Spanish") and restart.

### Choosing a model

| Model | Size | Speed on Orin Nano | Notes |
|---|---|---|---|
| `qwen3:4b` *(default)* | 2.5 GB | good | Best at using tools (reminders, lists) reliably |
| `llama3.2:3b` | 2.0 GB | faster | Good fallback, a bit less reliable with tools |
| `qwen3:1.7b` | 1.4 GB | fastest | For the 4GB Nano, or if you want snappier replies |

Switch with `ollama pull <model>`, set `MODEL=` in `.env`, and restart. The model must support **tools** in Ollama.

### Web search

Search works out of the box using DuckDuckGo, and weather comes from [Open-Meteo](https://open-meteo.com). Neither needs an API key. DuckDuckGo sometimes rate-limits automated searches. For rock-solid search, run your own [SearxNG](https://docs.searxng.org) on the Jetson:

```bash
mkdir -p ~/searxng
printf 'use_default_settings: true\nserver:\n  secret_key: "%s"\nsearch:\n  formats: [html, json]\n' \
  "$(openssl rand -hex 16)" > ~/searxng/settings.yml
docker run -d --name searxng --restart unless-stopped -p 127.0.0.1:8888:8080 \
  -v ~/searxng/settings.yml:/etc/searxng/settings.yml searxng/searxng
```

Then set `SEARXNG_URL=http://127.0.0.1:8888` in `.env` and restart.

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
- **Too many / too few check-ins**: change the style in *⋮ → Check-in settings*. *Light* is the gentlest.
- **"search failed"**: DuckDuckGo is probably rate-limiting you. Set up SearxNG (above).
- **Reminders while the Jetson was off**: they're delivered when it comes back up, marked *late*. Repeating reminders skip the missed ones and continue on schedule.

---

## For tinkerers

```
assistant/
  __main__.py     entry point: serve / check / chat
  agent.py        prompt building + Ollama streaming + tool-call loop
  tools.py        the tools the model can call (reminders, lists, goals, memory, search, weather)
  coach.py        proactive check-ins: morning brief, midday, evening, follow-ups, goal nudges
  webtools.py     DuckDuckGo / SearxNG search, page text, Open-Meteo weather
  timeparse.py    "tomorrow at 5pm" -> datetime; repeating schedules (DST-safe)
  scheduler.py    fires due reminders every 10s; fans messages out to Discord + web
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
