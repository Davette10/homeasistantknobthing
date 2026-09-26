"""Entry point.

    python -m assistant          run everything (web UI, Discord bot, reminder scheduler)
    python -m assistant check    verify Ollama + model + config, then exit
    python -m assistant chat     chat in the terminal (handy for testing over SSH)
"""
from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from .agent import Agent
from .config import load_settings
from .db import Store
from .scheduler import Notifier, Scheduler
from .web import create_app

log = logging.getLogger("assistant")


async def serve() -> None:
    settings = load_settings()
    store = Store(settings.db_path)
    agent = Agent(settings, store)
    notifier = Notifier()
    scheduler = Scheduler(store, settings, notifier)

    health = await agent.client.health()
    if not health["ok"]:
        log.warning("Ollama check failed: %s (will keep retrying on each message)", health["error"])

    app = create_app(settings, store, agent, notifier)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_level="info"))
    tasks = [asyncio.create_task(server.serve()), asyncio.create_task(scheduler.run())]

    if settings.discord_token and settings.discord_owner_id:
        from .discord_bot import DiscordBot

        bot = DiscordBot(settings, store, agent)
        notifier.add_sink(bot.send_reminder)
        tasks.append(asyncio.create_task(bot.start(settings.discord_token)))
    else:
        log.info("Discord not configured; reminders will only show in the web UI.")

    log.info("%s is up: http://%s:%s  (model %s)", settings.assistant_name, settings.web_host, settings.web_port, settings.model)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        t.result()  # surface the exception that stopped us, if any


async def check() -> int:
    settings = load_settings()
    ok = True
    print(f"Timezone:  {settings.timezone}")
    print(f"Model:     {settings.model} @ {settings.ollama_url}")
    agent = Agent(settings, Store(settings.db_path))
    health = await agent.client.health()
    print(f"Ollama:    {'OK' if health['ok'] else 'FAIL - ' + health['error']}")
    ok &= bool(health["ok"])
    print(f"Web UI:    port {settings.web_port}, password {'set' if settings.web_password else 'MISSING'}")
    ok &= bool(settings.web_password)
    if settings.discord_token and settings.discord_owner_id:
        print("Discord:   configured")
    else:
        print("Discord:   not configured (optional)")
    if health["ok"]:
        print("Test chat: ", end="", flush=True)
        reply = await agent.reply("Say hi in five words or fewer.", source="cli", save=False)
        print(reply)
    await agent.client.close()
    return 0 if ok else 1


async def cli_chat() -> None:
    settings = load_settings()
    agent = Agent(settings, Store(settings.db_path))
    print(f"Chatting with {settings.assistant_name}. Ctrl+D to quit.\n")
    loop = asyncio.get_running_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, input, "you> ")
        except EOFError:
            print()
            break
        if not text.strip():
            continue
        print(f"{settings.assistant_name.lower()}> ", end="", flush=True)
        async for ev in agent.chat(text, source="cli"):
            if ev["type"] == "token":
                print(ev["text"], end="", flush=True)
            elif ev["type"] == "tool":
                print(f"\n  [{ev['label']}: {ev['result']}]", flush=True)
            elif ev["type"] == "error":
                print(ev["text"], end="")
        print("\n")
    await agent.client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        asyncio.run(serve())
    elif cmd == "check":
        sys.exit(asyncio.run(check()))
    elif cmd == "chat":
        logging.getLogger().setLevel(logging.WARNING)
        asyncio.run(cli_chat())
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
