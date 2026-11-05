"""
TheSmarties Arcade - a Discord game bot.

Games:
  /td      co-op tower defense (everyone in the chat shares one base)
  /story   branching story adventure with 6 endings
  /cookie  cookie clicker with upgrades, saves and a leaderboard

Setup: see README.md. Run with:  python bot.py
"""

import asyncio
import json
import math
import os
import random
import time
from pathlib import Path

import discord
from discord import app_commands

HERE = Path(__file__).parent
SAVE_PATH = HERE / "save.json"


# ───────────────────────────── saving ─────────────────────────────

class Save:
    """Tiny JSON save file. Fine for a friend group; not built for huge servers."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        for key in ("cookies", "td_best", "endings"):
            self.data.setdefault(key, {})

    def write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)


save = Save(SAVE_PATH)


def fmt(n: float) -> str:
    """1234567 -> 1.23M"""
    n = float(n)
    if abs(n) < 1000:
        return f"{n:.0f}"
    for unit in ("K", "M", "B", "T", "Qa", "Qi"):
        n /= 1000
        if abs(n) < 1000:
            return f"{n:.2f}{unit}"
    return f"{n:.2e}"


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


class CookieView(discord.ui.View):
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


class TDView(discord.ui.View):
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


class StoryView(discord.ui.View):
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


# ─────────────────────────── the bot ──────────────────────────────

class Arcade(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        # Commands work in servers AND (when a member installs the app to their
        # account) in group chats and DMs.
        self.tree = app_commands.CommandTree(
            self,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        )

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user} - TheSmarties Arcade is open!")
        (HERE / "status.txt").write_text(f"online as {self.user}\n", encoding="utf-8")
        await self.change_presence(activity=discord.Game("/arcade"))


client = Arcade()
tree = client.tree


@tree.command(name="arcade", description="See every game in TheSmarties Arcade")
async def arcade_cmd(interaction: discord.Interaction):
    e = discord.Embed(
        title="🕹️ TheSmarties Arcade",
        description=(
            "🏰 `/td` — **Tower Defense.** Build towers together and survive as many waves as you can.\n"
            "📖 `/story` — **Station Kepler-9.** A story where the chat picks what happens. 6 endings.\n"
            "🍪 `/cookie` — **Cookie Clicker.** Bake, buy upgrades, get rich. `/cookietop` for the leaderboard."
        ),
        color=0xF1C40F,
    )
    await interaction.response.send_message(embed=e)


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


def load_env():
    """Read DISCORD_TOKEN from a .env file next to bot.py, if there is one."""
    env = HERE / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


STATUS_PATH = HERE / "status.txt"


def find_bare_token() -> str:
    """Accept a .env that holds just the token, without the DISCORD_TOKEN= part."""
    import re
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
    load_env()
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
    STATUS_PATH.write_text("starting\n", encoding="utf-8")
    try:
        client.run(token)
    except discord.LoginFailure:
        fail(
            "Discord rejected the token. On the Bot page click Reset Token, then Copy,\n"
            "and paste the new one into .env after DISCORD_TOKEN= (the old one stops working when you reset)."
        )
    except Exception as error:  # e.g. no internet
        fail(f"The bot stopped with an error: {type(error).__name__}: {error}")


if __name__ == "__main__":
    main()
