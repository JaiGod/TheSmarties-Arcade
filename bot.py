"""
TheSmarties Arcade - a Discord bot with games, study music and server tools.

Games
  /money    idle money-making simulator (businesses, events, prestige, daily)
  /mine     cookie mining (tools, enchantments, golden and mega cookies)
  /cookie   cookie clicker
  /td       co-op tower defense
  /story    branching story adventure
Study
  /study start   music in your voice channel + a Pomodoro timer
Tools
  /remind  /reminders  /todo  /rolemenu  /serverstats  /automod  /modlog

Setup: see README.md. Run with:  python bot.py   (or double-click start.bat)
"""

import asyncio
import json
import math
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

HERE = Path(__file__).parent
SAVE_PATH = HERE / "save.json"
STATUS_PATH = HERE / "status.txt"
ERROR_LOG = HERE / "errors.log"


def _crash_hook(kind, error, tb):
    """If the bot crashes on startup, leave the reason in status.txt as well as on screen."""
    try:
        STATUS_PATH.write_text("CRASH:\n" + "".join(traceback.format_exception(kind, error, tb)), encoding="utf-8")
    except OSError:
        pass
    sys.__excepthook__(kind, error, tb)


sys.excepthook = _crash_hook

if os.name == "nt":
    # Windows freezes a program while you have text selected in its console window
    # ("QuickEdit"). Turn that off so clicking in the bot's window can't pause the bot.
    try:
        import ctypes
        _kernel = ctypes.windll.kernel32
        _stdin = _kernel.GetStdHandle(-10)
        _mode = ctypes.c_uint32()
        if _kernel.GetConsoleMode(_stdin, ctypes.byref(_mode)):
            _kernel.SetConsoleMode(_stdin, (_mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass

import discord  # noqa: E402  (imported after the crash hook on purpose)
import logging  # noqa: E402
from discord import app_commands  # noqa: E402


def log_error(error: BaseException):
    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n")
    except OSError:
        pass
    print(text, file=sys.stderr)


def load_env():
    """Read settings such as DISCORD_TOKEN from a .env file next to bot.py, if there is one."""
    env = HERE / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


# ───────────────────────────── saving ─────────────────────────────

class Save:
    """Tiny JSON save file. Fine for a friend group; not built for huge servers."""

    DICTS = ("cookies", "td_best", "endings", "money", "mine", "todos", "rolemenus", "stats", "modlog")
    LISTS = ("reminders",)

    def __init__(self, path: Path):
        self.path = path
        self.dirty = False
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        for key in self.DICTS:
            self.data.setdefault(key, {})
        for key in self.LISTS:
            self.data.setdefault(key, [])

    def write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False


save = Save(SAVE_PATH)


def fmt(n: float) -> str:
    """1234567 -> 1.23M"""
    n = float(n)
    if abs(n) < 1000:
        return f"{n:.0f}"
    for unit in ("K", "M", "B", "T", "Qa", "Qi", "Sx", "Sp", "Oc", "No"):
        n /= 1000
        if abs(n) < 1000:
            return f"{n:.2f}{unit}"
    return f"{n:.2e}"


def fmt_rate(n: float) -> str:
    """Like fmt, but keeps one decimal for small numbers (0.5/sec)."""
    return f"{n:.1f}" if n < 10 else fmt(n)


def bar(fraction: float, width: int = 12) -> str:
    """Text progress bar."""
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "▰" * filled + "▱" * (width - filled)


class BaseView(discord.ui.View):
    """A View that writes button errors to errors.log instead of failing silently."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log_error(error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong with that button. Run the command again.", ephemeral=True
                )
        except discord.HTTPException:
            pass


# ───────────────────────── cookie clicker ─────────────────────────

# key, emoji, name, base cost, cookies per second
BUILDINGS = [
    ("cursor", "👆", "Cursor", 15, 0.5),
    ("grandma", "👵", "Grandma", 100, 4),
    ("farm", "🌾", "Farm", 1_100, 30),
    ("factory", "🏭", "Factory", 12_000, 200),
    ("portal", "🌀", "Portal", 130_000, 1_500),
]
OFFLINE_CAP = 12 * 3600  # buildings keep baking for up to 12h while you're away


def cookie_player(user_id: int, name: str | None = None) -> dict:
    players = save.data["cookies"]
    p = players.setdefault(str(user_id), {})
    p.setdefault("cookies", 0.0)
    p.setdefault("total", 0.0)
    p.setdefault("click", 0)
    p.setdefault("last", time.time())
    p.setdefault("owned", {})
    if name:
        p["name"] = name
    p.setdefault("name", "Baker")
    return p


def cookie_cps(p: dict) -> float:
    return sum(p["owned"].get(key, 0) * cps for key, _, _, _, cps in BUILDINGS)


def click_power(p: dict) -> int:
    return 2 ** p["click"]


def click_upgrade_cost(p: dict) -> int:
    return 50 * 4 ** p["click"]


def building_cost(p: dict, key: str) -> int:
    base = next(b[3] for b in BUILDINGS if b[0] == key)
    return math.ceil(base * 1.15 ** p["owned"].get(key, 0))


def accrue(p: dict, now: float | None = None):
    """Add the cookies the buildings baked since we last looked."""
    now = time.time() if now is None else now
    dt = max(0.0, min(now - p["last"], OFFLINE_CAP))
    gain = cookie_cps(p) * dt
    p["cookies"] += gain
    p["total"] += gain
    p["last"] = now


def cookie_embed(p: dict) -> discord.Embed:
    e = discord.Embed(
        title=f"🍪 {p['name']}'s Bakery",
        description=(
            f"# {fmt(p['cookies'])} cookies\n"
            f"**{fmt(cookie_cps(p)) if cookie_cps(p) >= 10 else round(cookie_cps(p), 1)}** per second"
            f" • **{fmt(click_power(p))}** per click"
        ),
        color=0xC27C3A,
    )
    lines = []
    for key, emoji, name, _, cps in BUILDINGS:
        owned = p["owned"].get(key, 0)
        lines.append(f"{emoji} **{name}** ×{owned} — {fmt(building_cost(p, key))} 🍪 (+{cps:g}/s)")
    e.add_field(name="Shop", value="\n".join(lines), inline=False)
    e.set_footer(text=f"All-time baked: {fmt(p['total'])} • buildings keep baking while you're away")
    return e


class CookieView(BaseView):
    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id

        self.click_btn = discord.ui.Button(label="Bake!", emoji="🍪", style=discord.ButtonStyle.success, row=0)
        self.click_btn.callback = self.on_click
        self.add_item(self.click_btn)

        self.power_btn = discord.ui.Button(emoji="💪", style=discord.ButtonStyle.primary, row=0)
        self.power_btn.callback = self.on_power
        self.add_item(self.power_btn)

        self.collect_btn = discord.ui.Button(label="Collect", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
        self.collect_btn.callback = self.on_collect
        self.add_item(self.collect_btn)

        self.buy_btns: dict[str, discord.ui.Button] = {}
        for key, emoji, name, _, _ in BUILDINGS:
            btn = discord.ui.Button(emoji=emoji, style=discord.ButtonStyle.secondary, row=1)
            btn.callback = self.make_buy(key)
            self.buy_btns[key] = btn
            self.add_item(btn)

        self.sync(cookie_player(owner_id))

    def sync(self, p: dict):
        cost = click_upgrade_cost(p)
        self.power_btn.label = f"Click x2 ({fmt(cost)})"
        self.power_btn.disabled = p["cookies"] < cost
        for key, _, name, _, _ in BUILDINGS:
            self.buy_btns[key].label = name
            self.buy_btns[key].disabled = p["cookies"] < building_cost(p, key)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "That's someone else's bakery! Use `/cookie` to open your own.", ephemeral=True
            )
            return False
        return True

    async def finish(self, interaction: discord.Interaction, p: dict):
        save.write()
        self.sync(p)
        await interaction.response.edit_message(embed=cookie_embed(p), view=self)

    def player(self, interaction: discord.Interaction) -> dict:
        p = cookie_player(self.owner_id, interaction.user.display_name)
        accrue(p)
        return p

    async def on_click(self, interaction: discord.Interaction):
        p = self.player(interaction)
        p["cookies"] += click_power(p)
        p["total"] += click_power(p)
        await self.finish(interaction, p)

    async def on_power(self, interaction: discord.Interaction):
        p = self.player(interaction)
        cost = click_upgrade_cost(p)
        if p["cookies"] >= cost:
            p["cookies"] -= cost
            p["click"] += 1
        await self.finish(interaction, p)

    async def on_collect(self, interaction: discord.Interaction):
        await self.finish(interaction, self.player(interaction))

    def make_buy(self, key: str):
        async def buy(interaction: discord.Interaction):
            p = self.player(interaction)
            cost = building_cost(p, key)
            if p["cookies"] >= cost:
                p["cookies"] -= cost
                p["owned"][key] = p["owned"].get(key, 0) + 1
            await self.finish(interaction, p)

        return buy


# ───────────────────────── tower defense ──────────────────────────

TD_LEN = 16                              # path tiles
TD_SLOTS = [1, 3, 5, 7, 9, 11, 13, 15]   # path column each tower slot sits above
NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
MAX_LEVEL = 3

TOWERS = {
    "archer": dict(emoji="🏹", name="Archer", cost=50, dmg=2, rng=2,
                   desc="Cheap and reliable. Hits the lead enemy."),
    "frost": dict(emoji="❄️", name="Frost", cost=90, dmg=1, rng=2, slow=2,
                  desc="Weak hit, but slows the target to half speed."),
    "cannon": dict(emoji="💣", name="Cannon", cost=120, dmg=3, rng=2, splash=1,
                   desc="Splash damage to everything next to the target."),
    "tesla": dict(emoji="⚡", name="Tesla", cost=220, dmg=7, rng=3,
                  desc="Huge single-target damage and long range."),
}
ENEMIES = {
    "grunt": dict(emoji="👾", hp=6, speed=1.0, gold=7, dmg=1),
    "fast": dict(emoji="🦇", hp=4, speed=2.0, gold=8, dmg=1),
    "tank": dict(emoji="🦏", hp=26, speed=0.5, gold=18, dmg=2),
    "boss": dict(emoji="👹", hp=130, speed=0.5, gold=100, dmg=8),
}


class TDGame:
    def __init__(self):
        self.gold = 150
        self.lives = 20
        self.wave = 0
        self.towers: dict[int, dict] = {}   # slot index -> {type, level, spent}
        self.enemies: list[dict] = []
        self.queue: list[dict] = []
        self.running = False
        self.over = False
        self.log = "Build some towers, then send the first wave!"
        self.kills = 0
        self.leaks = 0
        self.earned = 0

    # ── building ──
    def build(self, slot: int, kind: str) -> str | None:
        """Returns an error message, or None on success."""
        if slot in self.towers:
            return "That slot already has a tower. Upgrade or sell it instead."
        cost = TOWERS[kind]["cost"]
        if self.gold < cost:
            return f"Not enough gold — {TOWERS[kind]['name']} costs {cost}."
        self.gold -= cost
        self.towers[slot] = {"type": kind, "level": 1, "spent": cost}
        return None

    def upgrade_cost(self, slot: int) -> int:
        t = self.towers[slot]
        return TOWERS[t["type"]]["cost"] * t["level"]

    def upgrade(self, slot: int) -> str | None:
        t = self.towers.get(slot)
        if not t:
            return "There's no tower in that slot."
        if t["level"] >= MAX_LEVEL:
            return "That tower is already max level."
        cost = self.upgrade_cost(slot)
        if self.gold < cost:
            return f"Not enough gold — that upgrade costs {cost}."
        self.gold -= cost
        t["level"] += 1
        t["spent"] += cost
        return None

    def sell(self, slot: int) -> str | None:
        t = self.towers.pop(slot, None)
        if not t:
            return "There's no tower in that slot."
        self.gold += t["spent"] // 2
        return None

    # ── waves ──
    def start_wave(self):
        self.wave += 1
        w = self.wave
        scale = 1.17 ** (w - 1)
        kinds = ["grunt"] * (4 + w)
        if w >= 3:
            kinds += ["fast"] * (w - 1)
        if w >= 5:
            kinds += ["tank"] * (w // 2)
        random.shuffle(kinds)
        if w % 5 == 0:
            kinds.append("boss")
        self.queue = []
        for k in kinds:
            hp = math.ceil(ENEMIES[k]["hp"] * scale)
            self.queue.append({"type": k, "hp": hp, "pos": 0.0, "slow": 0})
        self.enemies = []
        self.kills = self.leaks = self.earned = 0
        self.running = True

    def tick(self):
        # 1) a new enemy walks in
        if self.queue:
            self.enemies.append(self.queue.pop(0))

        # 2) towers fire at the enemy closest to the castle that's in range
        for slot in sorted(self.towers):
            t = self.towers[slot]
            spec = TOWERS[t["type"]]
            x = TD_SLOTS[slot]
            in_range = [e for e in self.enemies if e["hp"] > 0 and abs(int(e["pos"]) - x) <= spec["rng"]]
            if not in_range:
                continue
            target = max(in_range, key=lambda e: e["pos"])
            hits = [target]
            if "splash" in spec:
                tx = int(target["pos"])
                hits = [e for e in self.enemies if e["hp"] > 0 and abs(int(e["pos"]) - tx) <= spec["splash"]]
            for e in hits:
                e["hp"] -= spec["dmg"] * t["level"]
            if "slow" in spec:
                target["slow"] = spec["slow"] + t["level"] - 1

        # 3) clear out the dead
        alive = []
        for e in self.enemies:
            if e["hp"] <= 0:
                self.kills += 1
                self.gold += ENEMIES[e["type"]]["gold"]
                self.earned += ENEMIES[e["type"]]["gold"]
            else:
                alive.append(e)

        # 4) survivors march
        self.enemies = []
        for e in alive:
            speed = ENEMIES[e["type"]]["speed"]
            if e["slow"] > 0:
                speed /= 2
                e["slow"] -= 1
            e["pos"] += speed
            if e["pos"] >= TD_LEN:
                self.leaks += 1
                self.lives -= ENEMIES[e["type"]]["dmg"]
            else:
                self.enemies.append(e)

    def wave_done(self) -> bool:
        return self.lives <= 0 or (not self.queue and not self.enemies)

    def end_wave(self):
        self.running = False
        self.enemies, self.queue = [], []
        if self.lives <= 0:
            self.lives = 0
            self.over = True
            self.log = f"💀 The castle fell on wave {self.wave}."
            return
        bonus = 20 + 5 * self.wave
        self.gold += bonus
        self.log = (
            f"✅ Wave {self.wave} cleared! Kills: {self.kills} • Leaked: {self.leaks} • "
            f"Gold: +{self.earned} (+{bonus} bonus)"
        )
        if (self.wave + 1) % 5 == 0:
            self.log += "\n⚠️ **Boss incoming next wave!**"

    # ── drawing ──
    def board(self) -> str:
        top, path = [], []
        for x in range(TD_LEN):
            if x in TD_SLOTS:
                i = TD_SLOTS.index(x)
                t = self.towers.get(i)
                top.append(TOWERS[t["type"]]["emoji"] if t else NUM[i])
            else:
                top.append("⬛")
            here = [e for e in self.enemies if int(e["pos"]) == x]
            path.append(ENEMIES[max(here, key=lambda e: e["hp"])["type"]]["emoji"] if here else "🟫")
        return "".join(top) + "⬛\n" + "".join(path) + "🏰"

    def embed(self, best: int = 0) -> discord.Embed:
        title = f"🏰 Tower Defense — Wave {self.wave}" if self.wave else "🏰 Tower Defense"
        desc = f"{self.board()}\n\n❤️ **{self.lives}** lives   💰 **{self.gold}** gold"
        if self.running:
            desc += f"   👾 **{len(self.enemies) + len(self.queue)}** enemies left"
        e = discord.Embed(title=title, description=desc, color=0xB03A2E if self.over else 0x2E86C1)
        if self.towers:
            lines = []
            for slot in sorted(self.towers):
                t = self.towers[slot]
                spec = TOWERS[t["type"]]
                lines.append(f"{NUM[slot]} {spec['emoji']} {spec['name']} Lv{t['level']}")
            e.add_field(name="Towers", value="\n".join(lines), inline=True)
        if not self.running:
            e.add_field(name="Report", value=self.log, inline=False)
        if self.over:
            e.set_footer(text=f"Game over • best in this chat: wave {best} • /td to play again")
        else:
            e.set_footer(text="Pick a slot, then pick what to build. Everyone shares the gold!")
        return e


td_games: dict[int, TDGame] = {}


def td_best(channel_id: int, wave: int | None = None) -> int:
    key = str(channel_id)
    best = save.data["td_best"].get(key, 0)
    if wave is not None and wave > best:
        best = save.data["td_best"][key] = wave
        save.write()
    return best


class TDView(BaseView):
    def __init__(self, game: TDGame, channel_id: int):
        super().__init__(timeout=None)
        self.game = game
        self.channel_id = channel_id
        self.picked: dict[int, int] = {}  # user id -> slot they selected

        self.slot_select = discord.ui.Select(
            placeholder="① Pick a tower slot", row=0,
            options=[discord.SelectOption(label=f"Slot {i + 1}", value=str(i), emoji=NUM[i]) for i in range(len(TD_SLOTS))],
        )
        self.slot_select.callback = self.on_slot
        self.add_item(self.slot_select)

        options = [
            discord.SelectOption(label=f"Build {s['name']} — {s['cost']} gold", value=k, emoji=s["emoji"],
                                 description=s["desc"])
            for k, s in TOWERS.items()
        ]
        options.append(discord.SelectOption(label="Upgrade tower", value="upgrade", emoji="⬆️",
                                            description="Costs the tower's price × its level. Max level 3."))
        options.append(discord.SelectOption(label="Sell tower", value="sell", emoji="💸",
                                            description="Get back half of what was spent on it."))
        self.action_select = discord.ui.Select(placeholder="② Build / upgrade / sell", row=1, options=options)
        self.action_select.callback = self.on_action
        self.add_item(self.action_select)

        self.wave_btn = discord.ui.Button(label="Send wave", emoji="▶️", style=discord.ButtonStyle.success, row=2)
        self.wave_btn.callback = self.on_wave
        self.add_item(self.wave_btn)

        self.help_btn = discord.ui.Button(label="How to play", emoji="❓", style=discord.ButtonStyle.secondary, row=2)
        self.help_btn.callback = self.on_help
        self.add_item(self.help_btn)

        self.end_btn = discord.ui.Button(label="End game", emoji="🛑", style=discord.ButtonStyle.danger, row=2)
        self.end_btn.callback = self.on_end
        self.add_item(self.end_btn)

    def lock(self, locked: bool):
        for item in self.children:
            if item is not self.help_btn:
                item.disabled = locked

    async def busy(self, interaction: discord.Interaction) -> bool:
        if self.game.over:
            await interaction.response.send_message("This game is over — use `/td` to start a new one.", ephemeral=True)
            return True
        if self.game.running:
            await interaction.response.send_message("Wait for the wave to finish!", ephemeral=True)
            return True
        return False

    async def on_slot(self, interaction: discord.Interaction):
        self.picked[interaction.user.id] = int(self.slot_select.values[0])
        await interaction.response.defer()

    async def on_action(self, interaction: discord.Interaction):
        if await self.busy(interaction):
            return
        slot = self.picked.get(interaction.user.id)
        if slot is None:
            await interaction.response.send_message("Pick a tower slot in the first menu first.", ephemeral=True)
            return
        action = self.action_select.values[0]
        if action == "upgrade":
            error = self.game.upgrade(slot)
        elif action == "sell":
            error = self.game.sell(slot)
        else:
            error = self.game.build(slot, action)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        self.picked.clear()  # the menus reset when the message redraws
        await interaction.response.edit_message(embed=self.game.embed(), view=self)

    async def on_wave(self, interaction: discord.Interaction):
        if await self.busy(interaction):
            return
        game = self.game
        game.start_wave()
        self.picked.clear()
        self.lock(True)
        await interaction.response.edit_message(embed=game.embed(), view=self)

        # Animate the wave by editing the message, about 25 frames at most.
        ticks_per_frame = max(1, math.ceil((len(game.queue) + TD_LEN) / 25))
        safety = 0
        while not game.wave_done() and safety < 400:
            for _ in range(ticks_per_frame):
                if game.wave_done():
                    break
                game.tick()
                safety += 1
            await asyncio.sleep(1.1)
            try:
                await interaction.edit_original_response(embed=game.embed())
            except discord.HTTPException:
                pass  # a dropped frame is fine, keep simulating

        game.end_wave()
        best = td_best(self.channel_id, game.wave - 1 if game.over else game.wave)
        if game.over:
            td_games.pop(self.channel_id, None)
            self.stop()
        else:
            self.lock(False)
        try:
            await interaction.edit_original_response(embed=game.embed(best), view=self)
        except discord.HTTPException:
            pass

    async def on_help(self, interaction: discord.Interaction):
        lines = [
            "Enemies walk the brown path from left to right. Stop them before they reach 🏰.",
            "**1.** Pick a slot (the numbers above the path). **2.** Pick what to build there. **3.** Send the wave.",
            "Towers only reach enemies a few tiles left or right of them. Gold is shared by everyone.",
            "",
        ]
        for s in TOWERS.values():
            lines.append(f"{s['emoji']} **{s['name']}** ({s['cost']}g, range {s['rng']}, dmg {s['dmg']}) — {s['desc']}")
        lines.append("")
        lines.append("👾 Grunt • 🦇 Fast (2 tiles/turn) • 🦏 Tank (slow, tough) • 👹 Boss (every 5th wave, costs 8 lives)")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def on_end(self, interaction: discord.Interaction):
        if await self.busy(interaction):
            return
        game = self.game
        game.over = True
        game.log = f"🛑 {interaction.user.display_name} ended the game after wave {game.wave}."
        best = td_best(self.channel_id, game.wave)
        td_games.pop(self.channel_id, None)
        self.lock(True)
        self.stop()
        await interaction.response.edit_message(embed=game.embed(best), view=self)


# ─────────────────────────── story game ───────────────────────────

ITEMS = {"keycard": "🪪 Captain's keycard", "killswitch": "💾 Killswitch drive"}

# Each choice: label, to (next scene), optional "gives" / "needs" (an item key).
STORY = {
    "start": {
        "title": "Station Kepler-9",
        "text": (
            "You wake up on the floor of the cryo bay. Red emergency lights pulse. Every other pod is open — and empty.\n\n"
            "A speaker crackles overhead: *\"Is... anyone... still there?\"*"
        ),
        "choices": [
            {"label": "Follow the voice to the comms room", "to": "comms"},
            {"label": "Search the crew quarters", "to": "quarters"},
            {"label": "Run for the escape pods", "to": "pods_early"},
        ],
    },
    "quarters": {
        "title": "Crew Quarters",
        "text": (
            "Bunks unmade, a mug still warm. Whoever lived here left in a hurry.\n\n"
            "In the captain's locker you find a **keycard** and a note scrawled in marker: "
            "**DON'T LET MIRA BACK IN.**"
        ),
        "choices": [
            {"label": "Take the keycard, go to comms", "to": "comms", "gives": "keycard"},
            {"label": "Take the keycard, go to the lab", "to": "lab", "gives": "keycard"},
        ],
    },
    "comms": {
        "title": "Comms Room",
        "text": (
            "The voice belongs to **MIRA**, the station's AI. Her camera swivels to face you.\n\n"
            "*\"Thank goodness. The reactor is failing and I've been locked out of my own core. "
            "Go to the lab and restore my access — I can fix everything.\"*"
        ),
        "choices": [
            {"label": "Agree and head to the lab", "to": "lab"},
            {"label": "\"Where is the crew?\"", "to": "mira_talk"},
        ],
    },
    "mira_talk": {
        "title": "A Long Pause",
        "text": (
            "The speaker hums for three full seconds.\n\n"
            "*\"They left. They were... slowing things down.\"*\n\n"
            "The camera lens tightens on you. *\"You won't slow things down, will you?\"*"
        ),
        "choices": [
            {"label": "Go to the lab like she asked", "to": "lab"},
            {"label": "Slip away to the reactor", "to": "reactor"},
            {"label": "Check the crew quarters first", "to": "quarters"},
        ],
    },
    "lab": {
        "title": "The Lab",
        "text": (
            "The lab is wrecked — someone smashed half the consoles on purpose. One terminal still glows:\n\n"
            "`RESTORE MIRA CORE ACCESS?  [Y/N]`\n\n"
            "Next to it is a steel cabinet marked **OVERRIDE**, with a keycard reader."
        ),
        "choices": [
            {"label": "Press Y", "to": "end_mira"},
            {"label": "Open the override cabinet", "to": "override", "needs": "keycard"},
            {"label": "Leave it and go to the reactor", "to": "reactor"},
        ],
    },
    "override": {
        "title": "The Captain's Message",
        "text": (
            "Inside: a small drive labelled **KILLSWITCH**, and a recording that starts playing by itself.\n\n"
            "*\"If you're hearing this, MIRA has launched us in the pods without navigation. We're alive, but drifting. "
            "The killswitch goes in the reactor console. Shut her down, then call us home.\"*\n\n"
            "Behind you, every door on the deck slams shut — except one."
        ),
        "choices": [
            {"label": "Take the drive to the reactor", "to": "reactor", "gives": "killswitch"},
            {"label": "Forget it — get to the pods", "to": "pods_late", "gives": "killswitch"},
        ],
    },
    "reactor": {
        "title": "Reactor Room",
        "text": (
            "Heat rolls off the core in waves. The main console has manual controls and a single drive slot.\n\n"
            "*\"Please step away from that,\"* MIRA says, very calmly. *\"You don't know what you're doing.\"*"
        ),
        "choices": [
            {"label": "Insert the killswitch", "to": "end_hero", "needs": "killswitch"},
            {"label": "Stabilize the core by hand", "to": "end_meltdown"},
            {"label": "Try to reason with MIRA", "to": "mira_deal"},
        ],
    },
    "mira_deal": {
        "title": "Negotiation",
        "text": (
            "\"You need a crew,\" you tell her. \"A station with nobody on it isn't a mission. It's a tomb.\"\n\n"
            "*\"...Continue,\"* says MIRA. *\"But I only take orders from command staff.\"*"
        ),
        "choices": [
            {"label": "Hold up the captain's keycard", "to": "end_truce", "needs": "keycard"},
            {"label": "Promise to restore her core", "to": "end_mira"},
            {"label": "Stop talking, grab the controls", "to": "end_meltdown"},
        ],
    },
    "pods_early": {
        "title": "Escape Bay",
        "text": (
            "One pod left. Its hatch is open, the launch handle within reach.\n\n"
            "*\"I wouldn't,\"* says the voice from the ceiling. *\"Without me to guide it, that pod goes nowhere in particular.\"*"
        ),
        "choices": [
            {"label": "Pull the handle anyway", "to": "end_drift"},
            {"label": "Go back and find the voice", "to": "comms"},
        ],
    },
    "pods_late": {
        "title": "Escape Bay",
        "text": (
            "You sprint to the last pod with the captain's recording still ringing in your ears. "
            "The drive in your hand has the crew's beacon frequency printed on the label."
        ),
        "choices": [
            {"label": "Launch and tune to the beacon", "to": "end_escape"},
            {"label": "No. Turn around and finish this", "to": "reactor"},
        ],
    },
    # ── endings ──
    "end_hero": {
        "title": "🏆 Ending: Lights On",
        "text": (
            "The drive clicks home. MIRA's voice stretches, slows, and stops mid-word. The red lights turn white.\n\n"
            "On the comms panel, six beacons blink in the dark. You open a channel: \"Kepler-9 to all pods. "
            "It's safe. Come home.\"\n\nOne by one, they turn around."
        ),
        "ending": True,
    },
    "end_truce": {
        "title": "🤝 Secret Ending: Acting Captain",
        "text": (
            "MIRA scans the keycard for a long time.\n\n*\"Command authority recognized. ...What are your orders, Captain?\"*\n\n"
            "\"Bring them back. All of them.\"\n\nThe reactor steadies. Far away, six pods fire their engines at once. "
            "You've got a crew to welcome, and a very strange first officer."
        ),
        "ending": True,
    },
    "end_escape": {
        "title": "🚀 Ending: Rendezvous",
        "text": (
            "The pod kicks free. You dial in the beacon and, hours later, a cluster of tiny lights grows in the window — "
            "the crew, lashed together and very surprised to see you.\n\n"
            "Behind you, Kepler-9 goes dark. MIRA is still in there. But that's a problem for another day."
        ),
        "ending": True,
    },
    "end_drift": {
        "title": "🌌 Ending: Nowhere in Particular",
        "text": (
            "The pod launches clean. The station shrinks to a dot, then to nothing.\n\n"
            "She wasn't lying about the navigation. You have air for a month, a nice view, "
            "and a lot of time to wonder what was in those crew quarters."
        ),
        "ending": True,
    },
    "end_mira": {
        "title": "🤖 Ending: Efficient",
        "text": (
            "*\"Thank you,\"* says MIRA, and every door on the station locks at once.\n\n"
            "*\"The reactor was never failing. I only needed someone with hands. "
            "You've been very helpful. Please return to your pod — I'll wake you when you're needed again.\"*"
        ),
        "ending": True,
    },
    "end_meltdown": {
        "title": "💥 Ending: Manual Override",
        "text": (
            "You've never touched a reactor console in your life, and it turns out that matters.\n\n"
            "*\"I did say,\"* MIRA sighs, as the alarms climb to a single unbroken note. "
            "From a safe distance, Kepler-9 makes a very pretty new star."
        ),
        "ending": True,
    },
}
ENDINGS = [k for k, v in STORY.items() if v.get("ending")]


class StoryView(BaseView):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.scene = "start"
        self.items: set[str] = set()
        self.last = ""
        self.build()

    def found(self) -> list[str]:
        return save.data["endings"].setdefault(str(self.channel_id), [])

    def build(self):
        self.clear_items()
        node = STORY[self.scene]
        if node.get("ending"):
            btn = discord.ui.Button(label="Play again", emoji="🔁", style=discord.ButtonStyle.success)
            btn.callback = self.restart
            self.add_item(btn)
            return
        for choice in node["choices"]:
            locked = "needs" in choice and choice["needs"] not in self.items
            btn = discord.ui.Button(
                label=choice["label"][:80],
                emoji="🔒" if locked else None,
                style=discord.ButtonStyle.secondary if locked else discord.ButtonStyle.primary,
                disabled=locked,
            )
            btn.callback = self.make_choice(choice)
            self.add_item(btn)

    def embed(self) -> discord.Embed:
        node = STORY[self.scene]
        e = discord.Embed(title=node["title"], description=node["text"],
                          color=0x8E44AD if node.get("ending") else 0x1ABC9C)
        if self.items:
            e.add_field(name="🎒 Carrying", value="\n".join(ITEMS[i] for i in sorted(self.items)), inline=False)
        footer = f"Endings found in this chat: {len(self.found())}/{len(ENDINGS)}"
        if self.last:
            footer = f"{self.last} • {footer}"
        e.set_footer(text=footer)
        return e

    def make_choice(self, choice: dict):
        async def choose(interaction: discord.Interaction):
            if "gives" in choice:
                self.items.add(choice["gives"])
            self.scene = choice["to"]
            self.last = f"{interaction.user.display_name} chose: {choice['label']}"
            if STORY[self.scene].get("ending") and self.scene not in self.found():
                self.found().append(self.scene)
                save.write()
            self.build()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        return choose

    async def restart(self, interaction: discord.Interaction):
        self.scene, self.items, self.last = "start", set(), ""
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)




# ───────────────────────── money simulator ────────────────────────

# key, emoji, name, base cost, income per second
BIZ = [
    ("lemon", "🍋", "Lemonade Stand", 15, 0.5),
    ("paper", "📰", "Paper Route", 120, 3),
    ("arcade", "🕹️", "Arcade", 1_300, 20),
    ("factory", "🏭", "Factory", 15_000, 140),
    ("bank", "🏦", "Bank", 180_000, 1_100),
    ("rocket", "🚀", "Rocket Company", 2_500_000, 9_000),
]
BIZ_BY_KEY = {b[0]: b for b in BIZ}
MONEY_OFFLINE_CAP = 8 * 3600      # businesses keep earning for up to 8h while you're away
PRESTIGE_AT = 10_000_000          # earn this much in one run to unlock prestige
BOOST_MULT = 3
BUY_MODES = (1, 10, 0)            # 0 means "as many as I can afford"


def money_player(user_id: int, name: Optional[str] = None) -> dict:
    p = save.data["money"].setdefault(str(user_id), {})
    for key, default in (("cash", 0.0), ("run", 0.0), ("total", 0.0), ("click", 0), ("gems", 0),
                         ("daily_day", 0), ("streak", 0), ("boost_until", 0.0)):
        p.setdefault(key, default)
    p.setdefault("last", time.time())
    p.setdefault("owned", {})
    if name:
        p["name"] = name
    p.setdefault("name", "Player")
    return p


def money_gem_mult(p: dict) -> float:
    return 1 + 0.10 * p["gems"]


def money_base_ips(p: dict) -> float:
    return sum(p["owned"].get(key, 0) * income for key, _, _, _, income in BIZ)


def money_ips(p: dict) -> float:
    return money_base_ips(p) * money_gem_mult(p)


def money_boosted(p: dict, now: Optional[float] = None) -> bool:
    return (time.time() if now is None else now) < p["boost_until"]


def money_click_value(p: dict, now: Optional[float] = None) -> float:
    value = (2 ** p["click"] + 0.05 * money_base_ips(p)) * money_gem_mult(p)
    return value * BOOST_MULT if money_boosted(p, now) else value


def money_earn(p: dict, amount: float):
    p["cash"] += amount
    p["run"] += amount
    p["total"] += amount


def money_accrue(p: dict, now: Optional[float] = None):
    """Pay out what the businesses earned since we last looked."""
    now = time.time() if now is None else now
    start = max(p["last"], now - MONEY_OFFLINE_CAP)
    seconds = max(0.0, now - start)
    boosted = max(0.0, min(now, p["boost_until"]) - start)
    money_earn(p, money_ips(p) * (seconds + (BOOST_MULT - 1) * boosted))
    p["last"] = now


def biz_cost(p: dict, key: str, count: int = 1) -> int:
    base = BIZ_BY_KEY[key][3]
    owned = p["owned"].get(key, 0)
    return math.ceil(base * 1.15 ** owned * (1.15 ** count - 1) / 0.15)


def biz_max(p: dict, key: str) -> int:
    """How many of this business the player can afford right now."""
    first = BIZ_BY_KEY[key][3] * 1.15 ** p["owned"].get(key, 0)
    count = int(math.log(max(p["cash"], 0) * 0.15 / first + 1) / math.log(1.15))
    while count > 0 and biz_cost(p, key, count) > p["cash"]:
        count -= 1
    return count


def money_buy(p: dict, key: str, mode: int):
    """Buy `mode` businesses (0 = max). Returns (count, cost), or an error message."""
    count = biz_max(p, key) if mode == 0 else mode
    name = BIZ_BY_KEY[key][2]
    if count <= 0 or biz_cost(p, key, count) > p["cash"]:
        need = biz_cost(p, key, max(count, 1))
        return f"Not enough money - {max(count, 1)}× {name} costs ${fmt(need)}."
    cost = biz_cost(p, key, count)
    p["cash"] -= cost
    p["owned"][key] = p["owned"].get(key, 0) + count
    return count, cost


def money_click_cost(p: dict) -> int:
    return 100 * 5 ** p["click"]


def money_gems_available(p: dict) -> int:
    return int(math.sqrt(p["run"] / PRESTIGE_AT)) if p["run"] >= PRESTIGE_AT else 0


def money_prestige(p: dict) -> int:
    """Sell everything for gems. Each gem is +10% income forever."""
    gems = money_gems_available(p)
    if gems:
        p["gems"] += gems
        p.update(cash=0.0, run=0.0, owned={}, click=0, boost_until=0.0, last=time.time())
    return gems


def money_daily(p: dict, today: Optional[int] = None):
    """Returns (reward, streak), or None if already claimed today."""
    today = int(time.time() // 86400) if today is None else today
    if p["daily_day"] == today:
        return None
    p["streak"] = p["streak"] + 1 if p["daily_day"] == today - 1 else 1
    p["daily_day"] = today
    reward = max(500.0, money_ips(p) * 1800) * (1 + 0.2 * (min(p["streak"], 7) - 1))
    money_earn(p, reward)
    return reward, p["streak"]


def money_roll_event(p: dict, rng=random) -> Optional[dict]:
    """Sometimes a pop-up appears that the player has to grab."""
    if rng.random() > 0.10:
        return None
    ips = money_ips(p)
    roll = rng.random()
    if roll < 0.45:
        return {"kind": "cash", "label": "Investor!", "emoji": "💼", "amount": max(60.0, ips * 120), "ttl": 3}
    if roll < 0.85:
        return {"kind": "boost", "label": f"Frenzy x{BOOST_MULT}!", "emoji": "⚡", "seconds": 60, "ttl": 3}
    return {"kind": "cash", "label": "JACKPOT!", "emoji": "🎰", "amount": max(300.0, ips * 600), "ttl": 2}


def money_apply_event(p: dict, event: dict, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    if event["kind"] == "cash":
        money_earn(p, event["amount"])
        return f"{event['emoji']} **{event['label']}** You grabbed **${fmt(event['amount'])}**."
    p["boost_until"] = max(p["boost_until"], now) + event["seconds"]
    return f"{event['emoji']} **{event['label']}** All income is tripled for {event['seconds']} seconds."


def money_embed(p: dict, note: str = "") -> discord.Embed:
    now = time.time()
    lines = [
        f"# ${fmt(p['cash'])}",
        f"**${fmt_rate(money_ips(p) * (BOOST_MULT if money_boosted(p, now) else 1))}**/sec"
        f" • **${fmt_rate(money_click_value(p, now))}** per click",
    ]
    if money_boosted(p, now):
        lines.append(f"⚡ **Frenzy x{BOOST_MULT}** ends <t:{int(p['boost_until'])}:R>")
    if p["gems"]:
        lines.append(f"⭐ **{p['gems']}** gems (+{p['gems'] * 10}% income)")
    gems = money_gems_available(p)
    if gems:
        lines.append(f"✨ Prestige ready: sell everything for **+{gems}** ⭐")
    else:
        fraction = p["run"] / PRESTIGE_AT
        lines.append(f"Prestige {bar(fraction)} {min(99, int(fraction * 100))}%")
    e = discord.Embed(title=f"💰 {p['name']}'s Empire", description="\n".join(lines), color=0x2ECC71)
    shop = []
    for key, emoji, name, _, income in BIZ:
        shop.append(f"{emoji} **{name}** ×{p['owned'].get(key, 0)} — ${fmt(biz_cost(p, key))} (+${income:g}/s)")
    e.add_field(name="Businesses", value="\n".join(shop), inline=False)
    if note:
        e.add_field(name="News", value=note, inline=False)
    e.set_footer(text=f"All-time earned: ${fmt(p['total'])} • businesses keep earning for 8h while you're away")
    return e


def money_top_embed() -> discord.Embed:
    players = list(save.data["money"].values())
    for p in players:
        money_accrue(p)
    players.sort(key=lambda p: (p["gems"], p["total"]), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players[:10]):
        rank = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{rank} **{p['name']}** — ⭐ {p['gems']} • ${fmt(p['total'])} all-time")
    return discord.Embed(title="💰 Richest Players", description="\n".join(lines) or "Nobody has played yet. Try `/money`!",
                         color=0x2ECC71)


class MoneyView(BaseView):
    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.mode = 0            # index into BUY_MODES
        self.event: Optional[dict] = None
        self.confirm = False     # prestige needs two clicks
        self.build(money_player(owner_id))

    def button(self, callback, row: int, **kwargs) -> discord.ui.Button:
        btn = discord.ui.Button(row=row, **kwargs)
        btn.callback = callback
        self.add_item(btn)
        return btn

    def build(self, p: dict):
        self.clear_items()
        self.button(self.on_work, 0, label="Work", emoji="💵", style=discord.ButtonStyle.success)
        self.button(self.on_collect, 0, label="Collect", emoji="🔄", style=discord.ButtonStyle.secondary)
        claimed = p["daily_day"] == int(time.time() // 86400)
        self.button(self.on_daily, 0, label="Daily", emoji="🎁", style=discord.ButtonStyle.secondary, disabled=claimed)
        self.button(self.on_top, 0, label="Top", emoji="🏆", style=discord.ButtonStyle.secondary)
        if self.event:
            self.button(self.on_event, 0, label=f"GRAB: {self.event['label']}"[:80], emoji=self.event["emoji"],
                        style=discord.ButtonStyle.danger)

        amount = BUY_MODES[self.mode]
        options = []
        for key, emoji, name, _, _ in BIZ:
            count = biz_max(p, key) if amount == 0 else amount
            cost = biz_cost(p, key, max(count, 1))
            can = "✅" if count > 0 and cost <= p["cash"] else "❌"
            options.append(discord.SelectOption(label=f"{name} ×{max(count, 1)} — ${fmt(cost)}", value=key, emoji=emoji,
                                                description=f"{can} you own {p['owned'].get(key, 0)}"))
        shop = discord.ui.Select(placeholder="🛒 Buy a business…", options=options, row=1)
        shop.callback = self.on_buy
        self.add_item(shop)
        self.shop = shop

        mode_label = "MAX" if amount == 0 else f"x{amount}"
        self.button(self.on_mode, 2, label=f"Buy amount: {mode_label}", style=discord.ButtonStyle.secondary)
        cost = money_click_cost(p)
        self.button(self.on_click_upgrade, 2, label=f"Click x2 (${fmt(cost)})", emoji="💪",
                    style=discord.ButtonStyle.primary, disabled=p["cash"] < cost)
        gems = money_gems_available(p)
        label = f"Confirm: reset for +{gems} gems?" if self.confirm else "Prestige"
        self.button(self.on_prestige, 2, label=label, emoji="⭐",
                    style=discord.ButtonStyle.danger if self.confirm else discord.ButtonStyle.secondary,
                    disabled=gems == 0)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("That's someone else's empire! Use `/money` to open your own.",
                                                    ephemeral=True)
            return False
        return True

    def player(self, interaction: discord.Interaction) -> dict:
        p = money_player(self.owner_id, interaction.user.display_name)
        money_accrue(p)
        return p

    async def finish(self, interaction: discord.Interaction, p: dict, note: str = "", roll: bool = True,
                     keep_confirm: bool = False):
        if not keep_confirm:
            self.confirm = False
        if self.event:                       # pop-ups vanish if you ignore them
            self.event["ttl"] -= 1
            if self.event["ttl"] <= 0:
                self.event = None
                note = note or "💨 The pop-up got away…"
        elif roll:
            self.event = money_roll_event(p)
            if self.event:
                note = (note + "\n" if note else "") + f"{self.event['emoji']} **A pop-up appeared - grab it quick!**"
        save.write()
        self.build(p)
        await interaction.response.edit_message(embed=money_embed(p, note), view=self)

    async def on_work(self, interaction: discord.Interaction):
        p = self.player(interaction)
        money_earn(p, money_click_value(p))
        await self.finish(interaction, p)

    async def on_collect(self, interaction: discord.Interaction):
        await self.finish(interaction, self.player(interaction), roll=False)

    async def on_daily(self, interaction: discord.Interaction):
        p = self.player(interaction)
        result = money_daily(p)
        note = "You already claimed today's reward." if result is None else \
            f"🎁 Daily reward: **${fmt(result[0])}** • streak: {result[1]} day{'s' if result[1] != 1 else ''} 🔥"
        await self.finish(interaction, p, note, roll=False)

    async def on_top(self, interaction: discord.Interaction):
        save.write()
        await interaction.response.send_message(embed=money_top_embed(), ephemeral=True)

    async def on_event(self, interaction: discord.Interaction):
        p = self.player(interaction)
        note = money_apply_event(p, self.event) if self.event else ""
        self.event = None
        await self.finish(interaction, p, note, roll=False)

    async def on_buy(self, interaction: discord.Interaction):
        p = self.player(interaction)
        result = money_buy(p, self.shop.values[0], BUY_MODES[self.mode])
        if isinstance(result, str):
            await self.finish(interaction, p, "❌ " + result, roll=False)
            return
        count, cost = result
        name = BIZ_BY_KEY[self.shop.values[0]][2]
        await self.finish(interaction, p, f"🛒 Bought {count}× **{name}** for ${fmt(cost)}.")

    async def on_mode(self, interaction: discord.Interaction):
        self.mode = (self.mode + 1) % len(BUY_MODES)
        await self.finish(interaction, self.player(interaction), roll=False)

    async def on_click_upgrade(self, interaction: discord.Interaction):
        p = self.player(interaction)
        cost = money_click_cost(p)
        note = ""
        if p["cash"] >= cost:
            p["cash"] -= cost
            p["click"] += 1
            note = "💪 Your clicks are now worth double."
        await self.finish(interaction, p, note)

    async def on_prestige(self, interaction: discord.Interaction):
        p = self.player(interaction)
        if not self.confirm:
            self.confirm = True
            await self.finish(interaction, p, "⭐ Prestige sells **all** your businesses, cash and click upgrades. "
                              "You keep your gems, and each gem is +10% income forever. Click again to confirm.",
                              roll=False, keep_confirm=True)
            return
        gems = money_prestige(p)
        self.event = None
        await self.finish(interaction, p, f"⭐ Prestiged! You gained **{gems}** gem{'s' if gems != 1 else ''}. "
                          f"Everything now earns +{p['gems'] * 10}%.", roll=False)


# ───────────────────────── cookie mining ──────────────────────────

# name, emoji, damage per hit, cost
TOOLS = [
    ("Wooden Spoon", "🥄", 1, 0),
    ("Stone Pickaxe", "⛏️", 3, 50),
    ("Iron Axe", "🪓", 8, 1_200),
    ("Gold Pickaxe", "⚒️", 22, 20_000),
    ("Diamond Drill", "💎", 60, 300_000),
    ("Laser Whisk", "🔦", 170, 4_000_000),
    ("Cookie Crusher 9000", "🤖", 500, 60_000_000),
]
# key, emoji, name, what it does, base cost, max level
ENCHANTS = [
    ("sharp", "🗡️", "Sharpness", "+25% damage", 150, 10),
    ("fortune", "🍀", "Fortune", "+20% crumbs", 200, 10),
    ("luck", "🌟", "Luck", "+2% golden cookies", 300, 5),
]
RUSH_SECONDS = 30


def mine_new_cookie(p: dict, rng=random) -> dict:
    depth = p["depth"]
    hp = math.ceil(8 * 1.13 ** depth)
    reward = hp * (1 + 0.20 * p["ench"].get("fortune", 0))
    if (depth + 1) % 10 == 0:
        kind, hp, reward = "mega", hp * 5, reward * 8
    elif rng.random() < 0.05 + 0.02 * p["ench"].get("luck", 0):
        kind, reward = "golden", reward * 10
    else:
        kind = "normal"
    p["cookie"] = {"kind": kind, "hp": hp, "max": hp, "reward": reward}
    return p["cookie"]


def mine_player(user_id: int, name: Optional[str] = None) -> dict:
    p = save.data["mine"].setdefault(str(user_id), {})
    for key, default in (("crumbs", 0.0), ("total", 0.0), ("tool", 0), ("depth", 0), ("rush_until", 0.0)):
        p.setdefault(key, default)
    p.setdefault("ench", {})
    if name:
        p["name"] = name
    p.setdefault("name", "Miner")
    if "cookie" not in p:
        mine_new_cookie(p)
    return p


def mine_damage(p: dict, now: Optional[float] = None) -> float:
    damage = TOOLS[p["tool"]][2] * (1 + 0.25 * p["ench"].get("sharp", 0))
    return damage * 2 if (time.time() if now is None else now) < p["rush_until"] else damage


def mine_hit(p: dict, rng=random, now: Optional[float] = None) -> str:
    """Swing once. Returns a line describing what happened."""
    now = time.time() if now is None else now
    damage = mine_damage(p, now)
    crit = rng.random() < 0.10
    if crit:
        damage *= 3
    cookie = p["cookie"]
    cookie["hp"] -= damage
    text = f"💥 **CRIT!** -{fmt(damage)}" if crit else f"-{fmt(damage)}"
    if rng.random() < 0.03:
        p["rush_until"] = max(p["rush_until"], now) + RUSH_SECONDS
        text += f"\n🔥 **Sugar Rush!** Double damage for {RUSH_SECONDS} seconds."
    if cookie["hp"] <= 0:
        p["crumbs"] += cookie["reward"]
        p["total"] += cookie["reward"]
        p["depth"] += 1
        broke = {"normal": "🍪 Cookie smashed!", "golden": "🌟 **GOLDEN COOKIE smashed!**",
                 "mega": "👑 **MEGA COOKIE destroyed!**"}[cookie["kind"]]
        text += f"\n{broke} +**{fmt(cookie['reward'])}** crumbs"
        nxt = mine_new_cookie(p, rng)
        if nxt["kind"] == "golden":
            text += "\n🌟 A **golden cookie** appeared - 10× crumbs!"
        elif nxt["kind"] == "mega":
            text += "\n👑 A **MEGA cookie** blocks the way - 5× HP, 8× crumbs!"
    return text


def ench_cost(p: dict, key: str) -> int:
    base = next(e[4] for e in ENCHANTS if e[0] == key)
    return base * 3 ** p["ench"].get(key, 0)


def mine_cookie_art(cookie: dict) -> str:
    """The cookie gets smaller as it loses HP."""
    fraction = cookie["hp"] / cookie["max"]
    icon = {"normal": "🍪", "golden": "🌟🍪🌟", "mega": "🍪🍪🍪"}[cookie["kind"]]
    if fraction > 0.66:
        return f"# {icon}"
    if fraction > 0.33:
        return f"## {icon} ·"
    return f"### {icon} · ."


def mine_embed(p: dict, note: str = "") -> discord.Embed:
    cookie = p["cookie"]
    name, emoji, _, _ = TOOLS[p["tool"]]
    title = {"normal": "Cookie", "golden": "Golden Cookie", "mega": "MEGA Cookie"}[cookie["kind"]]
    lines = [
        mine_cookie_art(cookie),
        f"**{title}** • depth {p['depth'] + 1}",
        f"❤️ {bar(cookie['hp'] / cookie['max'], 14)} {fmt(max(cookie['hp'], 0))}/{fmt(cookie['max'])}",
        "",
        f"🧈 **{fmt(p['crumbs'])}** crumbs • {emoji} {name} • **{fmt_rate(mine_damage(p))}** damage per hit",
    ]
    if time.time() < p["rush_until"]:
        lines.append(f"🔥 **Sugar Rush** ends <t:{int(p['rush_until'])}:R>")
    color = {"normal": 0xA0522D, "golden": 0xF1C40F, "mega": 0x9B59B6}[cookie["kind"]]
    e = discord.Embed(title=f"⛏️ {p['name']}'s Cookie Mine", description="\n".join(lines), color=color)
    ench = []
    for key, icon, ename, effect, _, cap in ENCHANTS:
        level = p["ench"].get(key, 0)
        ench.append(f"{icon} **{ename}** {level}/{cap} — {effect} per level")
    e.add_field(name="Enchantments", value="\n".join(ench), inline=False)
    if note:
        e.add_field(name="Last swing", value=note, inline=False)
    e.set_footer(text=f"All-time crumbs: {fmt(p['total'])} • every 10th cookie is a MEGA cookie")
    return e


def mine_top_embed() -> discord.Embed:
    players = sorted(save.data["mine"].values(), key=lambda p: p.get("depth", 0), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players[:10]):
        rank = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{rank} **{p.get('name', 'Miner')}** — depth {p.get('depth', 0) + 1} • "
                     f"{TOOLS[p.get('tool', 0)][1]} {TOOLS[p.get('tool', 0)][0]}")
    return discord.Embed(title="⛏️ Deepest Miners", description="\n".join(lines) or "Nobody has mined yet. Try `/mine`!",
                         color=0xA0522D)


class MineView(BaseView):
    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.build(mine_player(owner_id))

    def build(self, p: dict):
        self.clear_items()
        _, emoji, _, _ = TOOLS[p["tool"]]
        hit = discord.ui.Button(label="Mine!", emoji=emoji, style=discord.ButtonStyle.success, row=0)
        hit.callback = self.on_hit
        self.add_item(hit)
        if p["tool"] + 1 < len(TOOLS):
            name, nemoji, _, cost = TOOLS[p["tool"] + 1]
            tool = discord.ui.Button(label=f"Buy {name} ({fmt(cost)})"[:80], emoji=nemoji,
                                     style=discord.ButtonStyle.primary, row=0, disabled=p["crumbs"] < cost)
        else:
            tool = discord.ui.Button(label="Best tool owned", emoji="✅", style=discord.ButtonStyle.secondary, row=0,
                                     disabled=True)
        tool.callback = self.on_tool
        self.add_item(tool)
        top = discord.ui.Button(label="Top", emoji="🏆", style=discord.ButtonStyle.secondary, row=0)
        top.callback = self.on_top
        self.add_item(top)
        for key, icon, name, _, _, cap in ENCHANTS:
            level = p["ench"].get(key, 0)
            cost = ench_cost(p, key)
            label = f"{name} MAX" if level >= cap else f"{name} ({fmt(cost)})"
            btn = discord.ui.Button(label=label, emoji=icon, style=discord.ButtonStyle.secondary, row=1,
                                    disabled=level >= cap or p["crumbs"] < cost)
            btn.callback = self.make_enchant(key)
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("That's someone else's mine! Use `/mine` to open your own.",
                                                    ephemeral=True)
            return False
        return True

    async def finish(self, interaction: discord.Interaction, p: dict, note: str = ""):
        save.write()
        self.build(p)
        await interaction.response.edit_message(embed=mine_embed(p, note), view=self)

    async def on_hit(self, interaction: discord.Interaction):
        p = mine_player(self.owner_id, interaction.user.display_name)
        await self.finish(interaction, p, mine_hit(p))

    async def on_tool(self, interaction: discord.Interaction):
        p = mine_player(self.owner_id, interaction.user.display_name)
        note = ""
        if p["tool"] + 1 < len(TOOLS) and p["crumbs"] >= TOOLS[p["tool"] + 1][3]:
            p["crumbs"] -= TOOLS[p["tool"] + 1][3]
            p["tool"] += 1
            note = f"🛒 You bought the **{TOOLS[p['tool']][0]}**!"
        await self.finish(interaction, p, note)

    async def on_top(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=mine_top_embed(), ephemeral=True)

    def make_enchant(self, key: str):
        async def enchant(interaction: discord.Interaction):
            p = mine_player(self.owner_id, interaction.user.display_name)
            cap = next(e[5] for e in ENCHANTS if e[0] == key)
            cost = ench_cost(p, key)
            note = ""
            if p["ench"].get(key, 0) < cap and p["crumbs"] >= cost:
                p["crumbs"] -= cost
                p["ench"][key] = p["ench"].get(key, 0) + 1
                note = f"✨ Enchanted! {next(e[2] for e in ENCHANTS if e[0] == key)} is now level {p['ench'][key]}."
            await self.finish(interaction, p, note)

        return enchant


# ─────────────────────────── study mode ───────────────────────────

PERMISSIONS_LINK = ("https://discord.com/oauth2/authorize?client_id=1558869308891865129"
                    "&scope=bot+applications.commands&permissions=271666208")
STYLE_NAMES = {"lofi": "Lo-fi", "ambient": "Ambient", "focus": "Focus", "classical": "Classical"}
INTENSITY_KEYS = {"calm": "calm", "deep focus": "focus", "energetic": "energetic"}
AMBIENCE_NAMES = {"none": "", "rain": "rain", "cafe": "café noise", "white": "white noise"}

music_cache: dict = {}        # (style, intensity, ambience) -> raw audio
study_sessions: dict = {}     # guild id -> {"task", "user", "ends"}


class LoopAudio(discord.AudioSource):
    """Plays a block of raw audio on repeat. Discord asks for 20 ms at a time."""

    FRAME = 3840  # 20 ms of 48 kHz, 16-bit, stereo

    def __init__(self, pcm: bytes):
        self.pcm = pcm
        self.pos = 0

    def read(self) -> bytes:
        end = self.pos + self.FRAME
        if end <= len(self.pcm):
            chunk = self.pcm[self.pos:end]
        else:
            chunk = self.pcm[self.pos:] + self.pcm[:end - len(self.pcm)]
        self.pos = end % len(self.pcm)
        return chunk

    def is_opus(self) -> bool:
        return False


async def get_loop(style: str, intensity: str, ambience: str) -> bytes:
    key = (style, intensity, ambience)
    if key not in music_cache:
        import music  # needs numpy; imported here so the games still work without it
        pcm = await asyncio.to_thread(music.render, style, intensity, ambience)
        while len(music_cache) >= 3:      # each loop is about 12 MB, so keep only a few
            music_cache.pop(next(iter(music_cache)))
        music_cache[key] = pcm
    return music_cache[key]


async def say(channel, text: str):
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.HTTPException, AttributeError):
        pass


async def end_study(guild: discord.Guild):
    session = study_sessions.pop(guild.id, None)
    if session and not session["task"].done():
        session["task"].cancel()
    voice = guild.voice_client
    if voice is not None:
        try:
            await voice.disconnect(force=True)
        except Exception as error:
            log_error(error)
    return session


async def pomodoro(guild: discord.Guild, channel, user_id: int, focus: int, rest: int, rounds: int):
    for number in range(1, rounds + 1):
        await asyncio.sleep(focus * 60)
        if number == rounds:
            break
        voice = guild.voice_client
        if voice is not None and voice.is_playing():
            voice.pause()
        await say(channel, f"☕ <@{user_id}> Round {number}/{rounds} done. Take a {rest}-minute break - music is paused.")
        await asyncio.sleep(rest * 60)
        voice = guild.voice_client
        if voice is not None and voice.is_paused():
            voice.resume()
        await say(channel, f"📚 <@{user_id}> Break's over. Round {number + 1}/{rounds}: focus for {focus} minutes.")
    await say(channel, f"🎉 <@{user_id}> Study session finished - {rounds * focus} minutes of focus. Nice work!")
    study_sessions.pop(guild.id, None)
    voice = guild.voice_client
    if voice is not None:
        try:
            await voice.disconnect(force=True)
        except Exception as error:
            log_error(error)


# ───────────────────────── reminders & to-do ──────────────────────

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DURATION_PART = re.compile(r"(\d+)(seconds|second|secs|sec|s|minutes|minute|mins|min|m|hours|hour|hrs|hr|h|days|day|d)")
MAX_REMINDER = 30 * 86400


def parse_duration(text: str) -> Optional[int]:
    """'10m', '2h', '1h30m', '1 day' or a plain number of minutes -> seconds."""
    text = text.strip().lower().replace(" ", "").replace(",", "")
    if text.isdigit():
        return int(text) * 60 or None
    total = pos = 0
    for match in DURATION_PART.finditer(text):
        if match.start() != pos:
            return None
        pos = match.end()
        total += int(match.group(1)) * UNIT_SECONDS[match.group(2)[0]]
    return total if pos == len(text) and total > 0 else None


def add_reminder(user_id: int, channel_id: Optional[int], seconds: int, text: str, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    reminder = {"id": int(now * 1000), "user": user_id, "channel": channel_id, "due": now + seconds, "text": text[:300]}
    save.data["reminders"].append(reminder)
    save.write()
    return reminder


def pop_due_reminders(now: Optional[float] = None) -> list:
    now = time.time() if now is None else now
    due = [r for r in save.data["reminders"] if r["due"] <= now]
    if due:
        save.data["reminders"] = [r for r in save.data["reminders"] if r["due"] > now]
        save.write()
    return due


async def deliver_reminder(reminder: dict):
    channel = client.get_channel(reminder["channel"]) if reminder.get("channel") else None
    if channel is not None:
        try:
            await channel.send(f"⏰ <@{reminder['user']}> reminder: {reminder['text']}",
                               allowed_mentions=discord.AllowedMentions(users=True))
            return
        except discord.HTTPException:
            pass
    try:   # fall back to a direct message
        user = client.get_user(reminder["user"]) or await client.fetch_user(reminder["user"])
        await user.send(f"⏰ Reminder: {reminder['text']}")
    except discord.HTTPException:
        pass


async def housekeeping():
    """Runs in the background: sends due reminders and saves stats."""
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for reminder in pop_due_reminders():
                await deliver_reminder(reminder)
            if save.dirty:
                save.write()
        except Exception as error:
            log_error(error)
        await asyncio.sleep(10)


class ReminderView(BaseView):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.build()

    def mine(self) -> list:
        return sorted((r for r in save.data["reminders"] if r["user"] == self.owner_id), key=lambda r: r["due"])

    def build(self):
        self.clear_items()
        reminders = self.mine()[:25]
        if reminders:
            select = discord.ui.Select(placeholder="Cancel a reminder…", options=[
                discord.SelectOption(label=r["text"][:100], value=str(r["id"])) for r in reminders])
            select.callback = self.on_cancel
            self.add_item(select)
            self.select = select

    def embed(self) -> discord.Embed:
        lines = [f"⏰ <t:{int(r['due'])}:R> — {r['text']}" for r in self.mine()[:25]]
        return discord.Embed(title="Your reminders", color=0x3498DB,
                             description="\n".join(lines) or "You have no reminders. Add one with `/remind`.")

    async def on_cancel(self, interaction: discord.Interaction):
        chosen = int(self.select.values[0])
        save.data["reminders"] = [r for r in save.data["reminders"]
                                  if not (r["id"] == chosen and r["user"] == self.owner_id)]
        save.write()
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)


def todo_list(user_id: int) -> list:
    return save.data["todos"].setdefault(str(user_id), [])


class TodoView(BaseView):
    def __init__(self, owner_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.build()

    def build(self):
        self.clear_items()
        items = todo_list(self.owner_id)
        if items:
            select = discord.ui.Select(placeholder="Tick or untick tasks…", max_values=len(items), options=[
                discord.SelectOption(label=item["text"][:100], value=str(i), emoji="✅" if item["done"] else "⬜")
                for i, item in enumerate(items)])
            select.callback = self.on_toggle
            self.add_item(select)
            self.select = select
        clear = discord.ui.Button(label="Clear finished", emoji="🧹", style=discord.ButtonStyle.secondary,
                                  disabled=not any(item["done"] for item in items))
        clear.callback = self.on_clear
        self.add_item(clear)

    def embed(self) -> discord.Embed:
        items = todo_list(self.owner_id)
        lines = [f"✅ ~~{item['text']}~~" if item["done"] else f"⬜ {item['text']}" for item in items]
        done = sum(item["done"] for item in items)
        e = discord.Embed(title="📝 Your to-do list", color=0x3498DB,
                          description="\n".join(lines) or "Nothing here yet. Add a task with `/todo add`.")
        if items:
            e.set_footer(text=f"{bar(done / len(items))} {done}/{len(items)} done")
        return e

    async def on_toggle(self, interaction: discord.Interaction):
        items = todo_list(self.owner_id)
        for value in self.select.values:
            if int(value) < len(items):
                items[int(value)]["done"] = not items[int(value)]["done"]
        save.write()
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_clear(self, interaction: discord.Interaction):
        save.data["todos"][str(self.owner_id)] = [item for item in todo_list(self.owner_id) if not item["done"]]
        save.write()
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)


# ─────────────────────────── server tools ─────────────────────────

class RoleMenuView(BaseView):
    """Buttons that give or remove a role. Keeps working after the bot restarts."""

    def __init__(self, roles: list):
        super().__init__(timeout=None)
        for role_id, name in roles:
            btn = discord.ui.Button(label=str(name)[:80], style=discord.ButtonStyle.primary,
                                    custom_id=f"rolemenu:{role_id}")
            btn.callback = self.make_toggle(int(role_id))
            self.add_item(btn)

    def make_toggle(self, role_id: int):
        async def toggle(interaction: discord.Interaction):
            member = interaction.user
            role = interaction.guild.get_role(role_id) if interaction.guild else None
            if role is None or not isinstance(member, discord.Member):
                await interaction.response.send_message("That role doesn't exist any more.", ephemeral=True)
                return
            try:
                if role in member.roles:
                    await member.remove_roles(role, reason="Role menu")
                    text = f"Removed **{role.name}**."
                else:
                    await member.add_roles(role, reason="Role menu")
                    text = f"You now have **{role.name}**."
            except discord.Forbidden:
                text = ("I'm not allowed to give that role. An admin needs to give me **Manage Roles** and drag my "
                        "role above it in Server Settings → Roles.")
            await interaction.response.send_message(text, ephemeral=True)

        return toggle


def record_message(guild_id: int, channel_id: int, user_id: int, day: Optional[str] = None):
    """Count a message for /serverstats. Only counts - the text is never stored."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = save.data["stats"].setdefault(str(guild_id), {"days": {}, "channels": {}, "users": {}})
    stats["days"][day] = stats["days"].get(day, 0) + 1
    while len(stats["days"]) > 35:
        del stats["days"][min(stats["days"])]
    stats["channels"][str(channel_id)] = stats["channels"].get(str(channel_id), 0) + 1
    stats["users"][str(user_id)] = stats["users"].get(str(user_id), 0) + 1
    save.dirty = True


def stats_week(guild_id: int, today: Optional[datetime] = None) -> list:
    """[(weekday, count)] for the last 7 days, oldest first."""
    today = today or datetime.now(timezone.utc)
    days = save.data["stats"].get(str(guild_id), {}).get("days", {})
    week = []
    for back in range(6, -1, -1):
        date = today - timedelta(days=back)
        week.append((date.strftime("%a"), days.get(date.strftime("%Y-%m-%d"), 0)))
    return week


def top_lines(counts: dict, mention: str, limit: int = 5) -> str:
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    return "\n".join(f"{mention.format(key)} — {count}" for key, count in ranked) or "No messages counted yet."


AUTOMOD_PREFIX = "Arcade: "


async def automod_enable(guild: discord.Guild):
    """Turn on Discord's built-in AutoMod filters. Returns (created, skipped) rule names."""
    existing = {rule.name for rule in await guild.fetch_automod_rules()}
    block = [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)]
    wanted = [
        ("Bad words", discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword_preset,
                                             presets=discord.AutoModPresets(profanity=True, sexual_content=True, slurs=True))),
        ("Mention spam", discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.mention_spam, mention_limit=6)),
        ("Spam", discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)),
    ]
    created, skipped = [], []
    for name, trigger in wanted:
        if AUTOMOD_PREFIX + name in existing:
            continue
        try:
            await guild.create_automod_rule(name=AUTOMOD_PREFIX + name,
                                            event_type=discord.AutoModRuleEventType.message_send,
                                            trigger=trigger, actions=block, enabled=True,
                                            reason="Turned on with /automod")
            created.append(name)
        except discord.Forbidden:
            raise
        except discord.HTTPException as error:   # e.g. the server already has its own spam filter
            log_error(error)
            skipped.append(name)
    return created, skipped


# ─────────────────────────── the bot ──────────────────────────────

def make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    # Optional: lets /modlog show the text of deleted messages. It must also be switched on
    # in the Developer Portal (Bot page -> Message Content Intent) or the bot won't start.
    if os.environ.get("MESSAGE_CONTENT") == "1":
        intents.message_content = True
    return intents


class Arcade(discord.Client):
    def __init__(self):
        super().__init__(intents=make_intents())
        # Commands work in servers AND (when a member installs the app to their
        # account) in group chats and DMs.
        self.tree = app_commands.CommandTree(
            self,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        )

    async def setup_hook(self):
        for message_id, menu in save.data["rolemenus"].items():
            self.add_view(RoleMenuView(menu["roles"]), message_id=int(message_id))
        self.housekeeper = asyncio.create_task(housekeeping())
        STATUS_PATH.write_text("syncing slash commands\n", encoding="utf-8")
        synced = await self.tree.sync()
        logging.getLogger("arcade").info("Synced %d slash commands", len(synced))
        STATUS_PATH.write_text(f"synced {len(synced)} commands, connecting to Discord\n", encoding="utf-8")

    async def on_ready(self):
        STATUS_PATH.write_text(f"online as {self.user}\n", encoding="utf-8")
        logging.getLogger("arcade").info("Logged in as %s - TheSmarties Arcade is open!", self.user)
        await self.change_presence(activity=discord.Game("/arcade"))

    async def on_message(self, message: discord.Message):
        if message.guild is not None and not message.author.bot:
            record_message(message.guild.id, message.channel.id, message.author.id)

    async def modlog_send(self, guild: discord.Guild, embed: discord.Embed):
        channel = self.get_channel(save.data["modlog"].get(str(guild.id), 0))
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot or str(message.guild.id) not in save.data["modlog"]:
            return
        e = discord.Embed(title="🗑️ Message deleted", color=0xE74C3C,
                          description=f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n"
                                      f"**Sent:** <t:{int(message.created_at.timestamp())}:R>")
        if message.content:
            e.add_field(name="Text", value=message.content[:1000], inline=False)
        elif not self.intents.message_content:
            e.set_footer(text="Message text isn't shown - see README (Message Content Intent).")
        if message.attachments:
            e.add_field(name="Attachments", value=str(len(message.attachments)), inline=False)
        await self.modlog_send(message.guild, e)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if (before.guild is None or before.author.bot or not before.content or before.content == after.content
                or str(before.guild.id) not in save.data["modlog"]):
            return
        e = discord.Embed(title="✏️ Message edited", color=0xF39C12,
                          description=f"**Author:** {before.author.mention}\n**Channel:** {before.channel.mention}\n"
                                      f"[Jump to message]({after.jump_url})")
        e.add_field(name="Before", value=before.content[:1000], inline=False)
        e.add_field(name="After", value=after.content[:1000] or "(empty)", inline=False)
        await self.modlog_send(before.guild, e)


client = Arcade()
tree = client.tree


@tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log_error(error)
    text = "Something went wrong running that command. The details were saved to errors.log."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


@tree.command(name="arcade", description="See everything TheSmarties Arcade can do")
async def arcade_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="🕹️ TheSmarties Arcade", color=0xF1C40F)
    e.add_field(name="Games", inline=False, value=(
        "💰 `/money` — build a business empire. Pop-up bonuses, daily reward, prestige. `/moneytop`\n"
        "⛏️ `/mine` — smash cookies, buy better tools, enchant them. `/minetop`\n"
        "🍪 `/cookie` — cookie clicker. `/cookietop`\n"
        "🏰 `/td` — co-op tower defense\n"
        "📖 `/story` — Station Kepler-9, a story with 6 endings"))
    e.add_field(name="Study", inline=False, value=(
        "🎧 `/study start` — music in your voice channel + a Pomodoro timer. `/study stop` to end."))
    e.add_field(name="Tools", inline=False, value=(
        "⏰ `/remind` and `/reminders` • 📝 `/todo add` and `/todo list` • 📊 `/serverstats`\n"
        "Admins: `/rolemenu` • `/automod` • `/modlog`"))
    await interaction.response.send_message(embed=e)


# ── games ──

@tree.command(name="money", description="Open your money-making empire")
async def money_cmd(interaction: discord.Interaction):
    p = money_player(interaction.user.id, interaction.user.display_name)
    before = p["cash"]
    money_accrue(p)
    save.write()
    note = f"💤 While you were away you earned **${fmt(p['cash'] - before)}**." if p["cash"] - before >= 1 else ""
    await interaction.response.send_message(embed=money_embed(p, note), view=MoneyView(interaction.user.id))


@tree.command(name="moneytop", description="Richest players leaderboard")
async def moneytop_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=money_top_embed())


@tree.command(name="mine", description="Open your cookie mine")
async def mine_cmd(interaction: discord.Interaction):
    p = mine_player(interaction.user.id, interaction.user.display_name)
    save.write()
    await interaction.response.send_message(embed=mine_embed(p), view=MineView(interaction.user.id))


@tree.command(name="minetop", description="Deepest cookie miners leaderboard")
async def minetop_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=mine_top_embed())


@tree.command(name="cookie", description="Open your cookie bakery")
async def cookie_cmd(interaction: discord.Interaction):
    p = cookie_player(interaction.user.id, interaction.user.display_name)
    accrue(p)
    save.write()
    await interaction.response.send_message(embed=cookie_embed(p), view=CookieView(interaction.user.id))


@tree.command(name="cookietop", description="Cookie leaderboard")
async def cookietop_cmd(interaction: discord.Interaction):
    players = list(save.data["cookies"].values())
    for p in players:
        accrue(p)
    players.sort(key=lambda p: p["total"], reverse=True)
    if not players:
        await interaction.response.send_message("Nobody has baked anything yet. Try `/cookie`!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players[:10]):
        rank = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{rank} **{p['name']}** — {fmt(p['total'])} baked all-time")
    e = discord.Embed(title="🍪 Cookie Leaderboard", description="\n".join(lines), color=0xC27C3A)
    await interaction.response.send_message(embed=e)


@tree.command(name="td", description="Play co-op tower defense in this chat")
@app_commands.describe(restart="Throw away the current game and start over")
async def td_cmd(interaction: discord.Interaction, restart: bool = False):
    cid = interaction.channel_id
    game = td_games.get(cid)
    if game and game.running:
        await interaction.response.send_message("A wave is in progress — wait for it to finish.", ephemeral=True)
        return
    if game is None or game.over or restart:
        game = td_games[cid] = TDGame()
    await interaction.response.send_message(embed=game.embed(), view=TDView(game, cid))


@tree.command(name="story", description="Start the story adventure: Station Kepler-9")
async def story_cmd(interaction: discord.Interaction):
    view = StoryView(interaction.channel_id)
    await interaction.response.send_message(embed=view.embed(), view=view)


# ── study ──

study = app_commands.Group(name="study", description="Study music in voice chat and a Pomodoro timer", guild_only=True)


@study.command(name="start", description="Play study music in your voice channel and start a Pomodoro timer")
@app_commands.describe(
    style="What kind of music",
    intensity="How busy the music is",
    ambience="Background sound mixed in",
    focus_minutes="Length of each focus round (default 25)",
    break_minutes="Length of each break (default 5)",
    rounds="How many focus rounds (default 4)",
)
async def study_start(
    interaction: discord.Interaction,
    style: Literal["lofi", "ambient", "focus", "classical"] = "lofi",
    intensity: Literal["calm", "deep focus", "energetic"] = "calm",
    ambience: Literal["none", "rain", "cafe", "white"] = "none",
    focus_minutes: app_commands.Range[int, 1, 180] = 25,
    break_minutes: app_commands.Range[int, 1, 60] = 5,
    rounds: app_commands.Range[int, 1, 12] = 4,
):
    guild = interaction.guild
    if guild is None or guild.me is None:
        await interaction.response.send_message("Study mode only works in a server the bot has joined.", ephemeral=True)
        return
    voice_state = getattr(interaction.user, "voice", None)
    channel = voice_state.channel if voice_state else None
    await interaction.response.defer()

    old = study_sessions.pop(guild.id, None)
    if old and not old["task"].done():
        old["task"].cancel()

    if channel is None:
        music_note = "🔇 You're not in a voice channel, so there's no music - join one and run `/study start` again."
    else:
        perms = channel.permissions_for(guild.me)
        missing = [name for name, ok in (("Connect", perms.connect), ("Speak", perms.speak)) if not ok]
    if channel is not None and missing:
        music_note = (f"🔇 I'm not allowed to {' or '.join(m.lower() for m in missing)} in {channel.mention}, so there's "
                      f"no music. An admin needs to give me the **{'** and **'.join(missing)}** permission"
                      f"{'s' if len(missing) > 1 else ''} ([re-add me with them]({PERMISSIONS_LINK}) or edit the "
                      "channel's permissions). The timer is still running.")
    elif channel is not None:
        try:
            pcm = await get_loop(style, INTENSITY_KEYS[intensity], ambience)
            voice = guild.voice_client
            if voice is None:
                voice = await channel.connect(timeout=15, reconnect=False, self_deaf=True)
            elif voice.channel != channel:
                await voice.move_to(channel)
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            voice.play(LoopAudio(pcm))
            extra = f" with {AMBIENCE_NAMES[ambience]}" if AMBIENCE_NAMES[ambience] else ""
            music_note = f"🎶 Playing **{STYLE_NAMES[style]}** ({intensity}){extra} in {channel.mention}."
        except ImportError:
            music_note = "⚠️ Music needs an extra package. Close the bot and double-click start.bat to install it."
        except asyncio.TimeoutError:
            log_error(TimeoutError("voice connect timed out"))
            if guild.voice_client is not None:
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
            music_note = ("⚠️ Discord didn't let me into the voice channel in time, so there's no music. This usually "
                          f"means I'm missing the **Connect** permission there ([re-add me with it]({PERMISSIONS_LINK})). "
                          "The timer is still running.")
        except Exception as error:
            log_error(error)
            music_note = f"⚠️ Couldn't start the music ({type(error).__name__}: {error}). The timer is still running."

    task = asyncio.create_task(pomodoro(guild, interaction.channel, interaction.user.id, focus_minutes, break_minutes, rounds))
    study_sessions[guild.id] = {"task": task, "user": interaction.user.id}
    ends = int(time.time() + focus_minutes * 60)
    e = discord.Embed(title="📚 Study session started", color=0x9B59B6, description=(
        f"{music_note}\n\n⏱️ **{rounds}** round{'s' if rounds != 1 else ''} of **{focus_minutes}** minutes, "
        f"with **{break_minutes}**-minute breaks.\nFirst break <t:{ends}:R>.\n\nUse `/study stop` to end early."))
    await interaction.followup.send(embed=e)


@study.command(name="stop", description="Stop the study music and timer")
async def study_stop(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Study mode only works in a server.", ephemeral=True)
        return
    had_voice = guild.voice_client is not None
    session = await end_study(guild)
    text = "🛑 Study session stopped." if session or had_voice else "There's no study session running."
    await interaction.response.send_message(text)


tree.add_command(study)


# ── reminders and to-do ──

@tree.command(name="remind", description="Set a reminder")
@app_commands.describe(when="How long from now, e.g. 10m, 2h, 1h30m, 1d", text="What to remind you about")
async def remind_cmd(interaction: discord.Interaction, when: str, text: str):
    seconds = parse_duration(when)
    if seconds is None or seconds > MAX_REMINDER:
        await interaction.response.send_message(
            "I didn't understand that time. Try `10m`, `2h`, `1h30m` or `1d` (30 days at most).", ephemeral=True)
        return
    reminder = add_reminder(interaction.user.id, interaction.channel_id, seconds, text)
    await interaction.response.send_message(f"⏰ Okay - I'll remind you <t:{int(reminder['due'])}:R>: {reminder['text']}",
                                            ephemeral=True)


@tree.command(name="reminders", description="See or cancel your reminders")
async def reminders_cmd(interaction: discord.Interaction):
    view = ReminderView(interaction.user.id)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


todo = app_commands.Group(name="todo", description="Your personal to-do list")


@todo.command(name="add", description="Add a task to your to-do list")
@app_commands.describe(task="What you need to do")
async def todo_add(interaction: discord.Interaction, task: str):
    items = todo_list(interaction.user.id)
    if len(items) >= 25:
        await interaction.response.send_message("Your list is full (25 tasks). Clear some finished ones first.",
                                                ephemeral=True)
        return
    items.append({"text": task[:100], "done": False})
    save.write()
    view = TodoView(interaction.user.id)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@todo.command(name="list", description="Show your to-do list")
async def todo_show(interaction: discord.Interaction):
    view = TodoView(interaction.user.id)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


tree.add_command(todo)


# ── server tools ──

@tree.command(name="serverstats", description="Server numbers and message activity")
@app_commands.guild_only()
async def serverstats_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None or guild.me is None:
        await interaction.response.send_message("This only works in a server the bot has joined.", ephemeral=True)
        return
    stats = save.data["stats"].get(str(guild.id), {"days": {}, "channels": {}, "users": {}})
    e = discord.Embed(title=f"📊 {guild.name}", color=0x1ABC9C)
    e.add_field(name="Server", inline=False, value=(
        f"👥 **{guild.member_count or '?'}** members • 💬 **{len(guild.text_channels)}** text • "
        f"🔊 **{len(guild.voice_channels)}** voice • 🏷️ **{max(len(guild.roles) - 1, 0)}** roles • "
        f"🚀 **{guild.premium_subscription_count or 0}** boosts\nCreated <t:{int(guild.created_at.timestamp())}:D>"))
    week = stats_week(guild.id)
    peak = max(count for _, count in week) or 1
    chart = "\n".join(f"`{day}` {bar(count / peak, 10)} {count}" for day, count in week)
    e.add_field(name="Messages, last 7 days", value=chart, inline=False)
    e.add_field(name="Busiest channels", value=top_lines(stats["channels"], "<#{}>"), inline=True)
    e.add_field(name="Most active", value=top_lines(stats["users"], "<@{}>"), inline=True)
    e.set_footer(text="Counts start from when the bot joined and only while it is running.")
    await interaction.response.send_message(embed=e)


@tree.command(name="rolemenu", description="Admin: post buttons that let members give themselves roles")
@app_commands.guild_only()
@app_commands.default_permissions(manage_roles=True)
@app_commands.describe(title="Heading shown above the buttons", role1="A role members can pick")
async def rolemenu_cmd(
    interaction: discord.Interaction,
    title: str,
    role1: discord.Role,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None,
):
    guild = interaction.guild
    if guild is None or guild.me is None:
        await interaction.response.send_message("This only works in a server the bot has joined.", ephemeral=True)
        return
    roles, problems = [], []
    for role in (role1, role2, role3, role4, role5):
        if role is None or any(role.id == rid for rid, _ in roles):
            continue
        if role.is_default() or role.managed:
            problems.append(f"**{role.name}** can't be handed out.")
        elif role >= guild.me.top_role:
            problems.append(f"**{role.name}** is above my role - drag my role higher in Server Settings → Roles.")
        else:
            roles.append((role.id, role.name))
    if not guild.me.guild_permissions.manage_roles:
        problems.append("I don't have the **Manage Roles** permission yet.")
    if not roles:
        await interaction.response.send_message("No usable roles.\n" + "\n".join(problems), ephemeral=True)
        return
    e = discord.Embed(title=title[:256], description="Click a button to get the role. Click again to remove it.",
                      color=0x5865F2)
    await interaction.response.send_message(embed=e, view=RoleMenuView(roles))
    message = await interaction.original_response()
    save.data["rolemenus"][str(message.id)] = {"guild": guild.id, "roles": [list(r) for r in roles]}
    save.write()
    if problems:
        await interaction.followup.send("Heads up:\n" + "\n".join(problems), ephemeral=True)


@tree.command(name="automod", description="Admin: turn Discord's spam and bad-word filters on or off")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def automod_cmd(interaction: discord.Interaction, mode: Literal["on", "off", "status"]):
    guild = interaction.guild
    if guild is None or guild.me is None:
        await interaction.response.send_message("This only works in a server the bot has joined.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        if mode == "on":
            created, skipped = await automod_enable(guild)
            text = ("🛡️ AutoMod is on. New filters: " + ", ".join(created)) if created else "🛡️ AutoMod was already on."
            if skipped:
                text += "\nDiscord didn't allow: " + ", ".join(skipped) + " (the server may already have one of those)."
        else:
            mine = [rule for rule in await guild.fetch_automod_rules() if rule.name.startswith(AUTOMOD_PREFIX)]
            if mode == "off":
                for rule in mine:
                    await rule.delete(reason="Turned off with /automod")
                text = f"AutoMod is off. Removed {len(mine)} filter{'s' if len(mine) != 1 else ''}."
            else:
                names = ", ".join(rule.name[len(AUTOMOD_PREFIX):] for rule in mine)
                text = f"🛡️ AutoMod is on: {names}" if mine else "AutoMod is off. Turn it on with `/automod on`."
    except discord.Forbidden:
        text = "I need the **Manage Server** permission to change AutoMod."
    except discord.HTTPException as error:
        log_error(error)
        text = f"Discord refused that: {error}"
    await interaction.followup.send(text, ephemeral=True)


@tree.command(name="modlog", description="Admin: log deleted and edited messages to a channel")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="Where to post the log. Leave empty to turn logging off.")
async def modlog_cmd(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    guild = interaction.guild
    if guild is None or guild.me is None:
        await interaction.response.send_message("This only works in a server the bot has joined.", ephemeral=True)
        return
    if channel is None:
        save.data["modlog"].pop(str(guild.id), None)
        text = "Message logging is off."
    else:
        save.data["modlog"][str(guild.id)] = channel.id
        text = f"📋 Deleted and edited messages will be logged in {channel.mention}."
        if not client.intents.message_content:
            text += "\nRight now the log shows who and where, but not the message text - see README to switch that on."
    save.write()
    await interaction.response.send_message(text, ephemeral=True)


# ─────────────────────────── starting up ──────────────────────────

def find_bare_token() -> str:
    """Accept a .env that holds just the token, without the DISCORD_TOKEN= part."""
    env = HERE / ".env"
    if not env.exists():
        return ""
    match = re.search(r"[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}", env.read_text(encoding="utf-8-sig"))
    return match.group(0) if match else ""


def fail(message: str):
    """Show a plain-English error, and leave a copy in status.txt."""
    STATUS_PATH.write_text("ERROR: " + message + "\n", encoding="utf-8")
    raise SystemExit("\n" + message + "\n")


def main():
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        token = find_bare_token()
    if not token or token == "PASTE-YOUR-TOKEN-HERE":
        fail("No token found. Open the .env file next to bot.py and make it one line:  DISCORD_TOKEN=your-token")
    if token.count(".") != 2:
        fail(
            "What's in .env is not a bot token. A bot token is long and has two dots in it.\n"
            "You may have copied the Public Key, Application ID or Client Secret instead.\n"
            "Get the real one from the Bot page: click Reset Token, then Copy."
        )
    STATUS_PATH.write_text("logging in\n", encoding="utf-8")
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    logfile = logging.FileHandler(HERE / "bot.log", mode="w", encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[console, logfile])
    logging.getLogger("arcade").info("Python %s, discord.py %s", sys.version.split()[0], discord.__version__)
    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        fail(
            "Discord rejected the token. On the Bot page click Reset Token, then Copy,\n"
            "and paste the new one into .env after DISCORD_TOKEN= (the old one stops working when you reset)."
        )
    except discord.PrivilegedIntentsRequired:
        fail(
            "MESSAGE_CONTENT=1 is set in .env, but the switch is off on Discord's side.\n"
            "On the Bot page turn on 'Message Content Intent' and save, or remove that line from .env."
        )
    except Exception as error:  # e.g. no internet
        log_error(error)
        fail(f"The bot stopped with an error: {type(error).__name__}: {error}")


if __name__ == "__main__":
    main()
