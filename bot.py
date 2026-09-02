import os
import json
import random
import asyncio
import re
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

# ============================================================
# LADY — VERSION PROPRE
# ============================================================

TIMEZONE = ZoneInfo("Europe/Paris")

SALON_SESSIONS_ID = 1521853338918977558
SALON_VENTES_ID = 1528658847806390382
SALON_JEU_ID = 1541713193762820106
SALON_DISCUSSION_ID = 1528130797029167134
SALON_ANNIVERSAIRES_ID = 1541680943583068200

ROLE_ROSE = "🌸 SEMAINE À FAIRE"
ROLE_VERT = "✅ À JOUR"
ROLE_ORANGE = "Semaine non validée"
ROLE_ROUGE = "Supprimer"

DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "lady_data.json"

BASE_DIR = Path(__file__).resolve().parent

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("La variable DISCORD_TOKEN est absente.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="lady_", intents=intents, help_command=None)

VINTED = re.compile(r"(?:https?://)?(?:www\.)?vinted\.[^\s/]+/[^\s]+", re.I)
data_lock = asyncio.Lock()
session_lock = asyncio.Lock()

# ============================================================
# DONNEES
# ============================================================

def fresh_data():
    return {
        "members": {},
        "sales_messages": [],
        "absences": {},
        "session_claims": [],
    }


def load_data():
    if not DATA_FILE.exists():
        return fresh_data()

    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        if not isinstance(raw, dict):
            return fresh_data()

        raw.setdefault("members", {})
        raw.setdefault("sales_messages", [])
        raw.setdefault("absences", {})
        raw.setdefault("session_claims", [])

        return raw

    except Exception as exc:
        print("Erreur lecture lady_data.json:", exc)
        return fresh_data()


DATA = load_data()


def save():
    tmp = DATA_FILE.with_suffix(".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(DATA, f, ensure_ascii=False, indent=2)

    tmp.replace(DATA_FILE)


def md(uid):
    uid = str(uid)

    m = DATA["members"].setdefault(uid, {})

    defaults = {
        "pp_week": 0,
        "pp_record": 0,
        "warnings": 0,
        "gifts": 0,
        "bows": 0,
        "sales_week": 0,
        "crown_until": None,
        "diamond_until": None,
        "birthday_until": None,
    }

    for k, v in defaults.items():
        m.setdefault(k, v)

    return m


def now():
    return datetime.now(TIMEZONE)


def parse_dt(value):
    if not value:
        return None

    try:
        d = datetime.fromisoformat(value)

        if d.tzinfo is None:
            d = d.replace(tzinfo=TIMEZONE)

        return d.astimezone(TIMEZONE)

    except Exception:
        return None


def active_until(value):
    d = parse_dt(value)
    return bool(d and d > now())


def remaining(value):
    d = parse_dt(value)

    if not d or d <= now():
        return "inactif"

    delta = d - now()
    total = max(0, int(delta.total_seconds()))

    h, rem = divmod(total, 3600)
    minutes = rem // 60

    return f"{h}h {minutes:02d}min"


# ============================================================
# SESSIONS
# ============================================================

FIXED = [
    ("🌙 Nocturne", time(0, 30), time(8, 0), "normal"),
    ("☕ Petit déjeuner", time(9, 0), time(9, 30), "normal"),
    ("🍹 Apéro", time(11, 30), time(12, 0), "normal"),
    ("🍽️ Repas", time(12, 0), time(12, 30), "normal"),
    ("🚀 Méga Boost", time(14, 0), time(14, 30), "mega"),
    ("🍹 Apéro", time(18, 30), time(19, 0), "normal"),
    ("🍽️ Repas", time(19, 0), time(19, 30), "normal"),
    ("🚀 Méga Boost", time(21, 0), time(21, 30), "mega"),
]

FREE_SESSIONS = [
    "🛍️ Offre sur article",
    "🔗 2 liens",
    "👗 Dressing",
    "👗 Article",
]

SESSION_IMAGES = {
    "🌙 Nocturne": ("NOCTURNE.jpg", "STOP NOCTURNE.jpg"),
    "☕ Petit déjeuner": ("PETIT DEJEUNER.jpg", "STOP PETIT DEJEUNER.jpg"),
    "🍹 Apéro": ("APERO.jpg", "APERO STOP.jpg"),
    "🍽️ Repas": ("REPAS.jpg", "REPAS STOP.jpg"),
    "🚀 Méga Boost": ("MEGA BOOST.jpg", "STOP MEGA BOOST.jpg"),
    "🛍️ Offre sur article": ("OFFRE.jpg", "STOP OFFRE.jpg"),
    "🔗 2 liens": ("2 LIENS.jpg", "STOP 2 LIENS.jpg"),
    "👗 Dressing": ("DRESSING.jpg", "STOP DRESSING.jpg"),
    "👗 Article": ("SESSION ARTICLE.jpg", "STOP SESSION ARTICLES.jpg"),
}

session = None
last_free_session = None


def dt_today(t):
    n = now()
    return datetime.combine(n.date(), t, tzinfo=TIMEZONE)


def current_fixed(n=None):
    n = n or now()

    for name, start_t, end_t, kind in FIXED:
        start = datetime.combine(n.date(), start_t, tzinfo=TIMEZONE)
        end = datetime.combine(n.date(), end_t, tzinfo=TIMEZONE)

        if start <= n < end:
            return name, start, end, kind

    return None


def next_fixed_start(n=None):
    n = n or now()

    candidates = []

    for _, start_t, _, _ in FIXED:
        d = datetime.combine(n.date(), start_t, tzinfo=TIMEZONE)

        if d > n:
            candidates.append(d)

    if candidates:
        return min(candidates)

    tomorrow = n.date() + timedelta(days=1)

    return datetime.combine(
        tomorrow,
        FIXED[0][1],
        tzinfo=TIMEZONE
    )


def claim_key(name, start):
    return f"{name}|{start.strftime('%Y-%m-%dT%H:%M')}"


async def claim_session(name, start):
    key = claim_key(name, start)

    async with data_lock:
        claims = DATA.setdefault("session_claims", [])

        if key in claims:
            return False

        claims.append(key)

        DATA["session_claims"] = claims[-100:]

        save()

    return True


async def send_image(channel, filename):
    path = BASE_DIR / filename

    if path.exists():
        try:
            await channel.send(file=discord.File(path))
            return True

        except Exception as exc:
            print(f"Image impossible {filename}: {exc}")

    else:
        print(f"Image absente: {path}")

    return False


async def begin_session(name, start, end, kind, is_free=False):
    global session

    channel = bot.get_channel(SALON_SESSIONS_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(SALON_SESSIONS_ID)

        except Exception as exc:
            print("Salon sessions introuvable:", exc)
            return

    dressing_count = random.randint(3, 10) if name == "👗 Dressing" else None

    session = {
        "name": name,
        "start": start,
        "end": end,
        "kind": kind,
        "free": is_free,
        "normal": set(),
        "participants": set(),
        "dressing_count": dressing_count,
    }

    start_img = SESSION_IMAGES.get(name, (None, None))[0]

    if start_img:
        await send_image(channel, start_img)

    if name == "👗 Dressing":
        text = (
            f"👗 **SESSION DRESSING**\n"
            f"Lady a choisi *{dressing_count} articles*.\n"
            f"⏰ Fin à *{end.strftime('%H:%M')}*."
        )

    elif name == "🔗 2 liens":
        text = (
            f"🔗 **SESSION 2 LIENS**\n"
            f"Vous pouvez envoyer *2 liens*.\n"
            f"⏰ Fin à *{end.strftime('%H:%M')}*."
        )

    else:
        text = (
            f"✨ **{name}**\n"
            f"⏰ Fin à *{end.strftime('%H:%M')}*."
        )

    await channel.send(text)

    if kind == "mega":
        discussion = bot.get_channel(SALON_DISCUSSION_ID)

        if discussion:
            try:
                await discussion.send(
                    "@everyone 🚀 *Le Méga Boost vient de commencer !*"
                )
            except Exception:
                pass

    print(f"Session lancée: {name} jusqu'à {end:%H:%M}")


async def finish_session():
    global session

    if not session:
        return

    old = session
    session = None

    channel = bot.get_channel(SALON_SESSIONS_ID)

    if channel is None:
        return

    stop_img = SESSION_IMAGES.get(old["name"], (None, None))[1]

    if stop_img:
        await send_image(channel, stop_img)

    participants = list(old["participants"])

    await channel.send(
        f"⛔ **Fin de {old['name']}**\n"
        f"👥 *{len(participants)} participante(s)*."
    )

    if old["kind"] == "mega" and participants:

        if len(participants) >= 3:
            winners = random.sample(participants, 3)

            rewards = [
                "gift",
                "bow",
                "crown"
            ]

            random.shuffle(rewards)

            lines = []

            async with data_lock:

                for uid, reward in zip(winners, rewards):

                    member = channel.guild.get_member(uid)
                    m = md(uid)

                    if reward == "gift":
                        m["gifts"] += 1
                        label = "🎁 1 cadeau"

                    elif reward == "bow":
                        m["bows"] += 1
                        label = "🎀 1 nœud"

                    else:
                        m["crown_until"] = (
                            now() + timedelta(hours=24)
                        ).isoformat()

                        label = "👑 Couronne pendant 24 h"

                    mention = (
                        member.mention
                        if member
                        else f"<@{uid}>"
                    )

                    lines.append(
                        f"{mention} → *{label}*"
                    )

                save()

            await channel.send(
                "🎉 **Gagnantes du Méga Boost :**\n"
                + "\n".join(lines)
            )

        else:
            await channel.send(
                "💗 Il faut au moins *3 participantes différentes* "
                "pour le tirage des 3 bonus du Méga Boost."
            )


async def launch_free(n=None):
    global last_free_session

    n = n or now()

    choices = [
        x for x in FREE_SESSIONS
        if x != last_free_session
    ]

    if not choices:
        choices = FREE_SESSIONS[:]

    name = random.choice(choices)

    end = min(
        n + timedelta(minutes=10),
        next_fixed_start(n)
    )

    if (end - n).total_seconds() < 30:
        return

    if not await claim_session(
        name,
        n.replace(second=0, microsecond=0)
    ):
        return

    last_free_session = name

    await begin_session(
        name,
        n,
        end,
        "normal",
        is_free=True
    )


@tasks.loop(seconds=20)
async def scheduler():
    global session

    n = now()

    async with session_lock:

        if session and n >= session["end"]:
            await finish_session()

        fixed = current_fixed(n)

        if fixed:
            name, start, end, kind = fixed

            if (
                session
                and session["name"] == name
                and session["start"] == start
            ):
                return

            if session:
                await finish_session()

            if await claim_session(name, start):
                await begin_session(
                    name,
                    start,
                    end,
                    kind,
                    is_free=False
                )

            return

        if session is None:
            await launch_free(n)


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


# ============================================================
# MESSAGES TEMPORAIRES
# ============================================================

async def temp_message(channel, text, seconds=15):
    try:
        msg = await channel.send(text)

        await asyncio.sleep(seconds)

        try:
            await msg.delete()
        except Exception:
            pass

    except Exception:
        pass


# ============================================================
# ROLES
# ============================================================

async def update_week_role(member):

    if not isinstance(member, discord.Member):
        return

    m = md(member.id)

    rose = discord.utils.get(
        member.guild.roles,
        name=ROLE_ROSE
    )

    green = discord.utils.get(
        member.guild.roles,
        name=ROLE_VERT
    )

    try:
        if m["pp_week"] >= 6:

            if green and green not in member.roles:
                await member.add_roles(
                    green,
                    reason="Semaine Lady validée"
                )

            if rose and rose in member.roles:
                await member.remove_roles(
                    rose,
                    reason="Semaine Lady validée"
                )

        else:
            if rose and rose not in member.roles:
                await member.add_roles(
                    rose,
                    reason="Semaine Lady en cours"
                )

    except discord.Forbidden:
        print("Lady ne peut pas modifier les rôles.")

    except Exception as exc:
        print("Erreur rôle:", exc)


# ============================================================
# PP ET BONUS
# ============================================================

async def award_pp(member):

    async with data_lock:

        m = md(member.id)

        before = int(m["pp_week"])

        m["pp_week"] = before + 1

        m["pp_record"] = max(
            int(m.get("pp_record", 0)),
            m["pp_week"]
        )

        if m["pp_week"] // 6 > before // 6:
            m["gifts"] += 1

        if m["pp_week"] // 20 > before // 20:
            m["diamond_until"] = (
                now() + timedelta(hours=24)
            ).isoformat()

        current = m["pp_week"]

        save()

    await update_week_role(member)

    return current


def has_bonus_marker(content, marker):
    return marker in (content or "")


async def participation(msg):
    global session

    if not session:
        return

    uid = msg.author.id
    content = msg.content or ""
    kind = session["kind"]

    # MEGA BOOST
    if kind == "mega":

        if any(
            x in content
            for x in ("🎁", "🎀", "👑", "💎")
        ):
            await temp_message(
                msg.channel,
                f"🚀 {msg.author.mention} aucun bonus 🎁 🎀 👑 💎 "
                f"n'est utilisable pendant le Méga Boost."
            )
            return

        if uid not in session["normal"]:

            session["normal"].add(uid)
            session["participants"].add(uid)

            current = await award_pp(msg.author)

            await temp_message(
                msg.channel,
                f"💗 {msg.author.mention} *+1 PP* — "
                f"tu es maintenant à *{current}/6 PP* cette semaine."
            )

        return

    async with data_lock:

        m = md(uid)

        # NOEUD
        if has_bonus_marker(content, "🎀"):

            if m["bows"] <= 0:
                asyncio.create_task(
                    temp_message(
                        msg.channel,
                        f"🎀 {msg.author.mention} "
                        f"tu n'as pas de nœud disponible."
                    )
                )
                return

            m["bows"] -= 1
            save()

            asyncio.create_task(
                temp_message(
                    msg.channel,
                    f"🎀 {msg.author.mention} "
                    f"ton lien sans rendre est validé. "
                    f"Il te reste *{m['bows']} nœud(s)*."
                )
            )

            return

        # CADEAU
        if has_bonus_marker(content, "🎁"):

            if m["gifts"] <= 0:
                asyncio.create_task(
                    temp_message(
                        msg.channel,
                        f"🎁 {msg.author.mention} "
                        f"tu n'as pas de cadeau disponible."
                    )
                )
                return

            m["gifts"] -= 1
            save()

            asyncio.create_task(
                temp_message(
                    msg.channel,
                    f"🎁 {msg.author.mention} "
                    f"ton lien supplémentaire est validé. "
                    f"Il te reste *{m['gifts']} cadeau(x)*."
                )
            )

            return

        # COURONNE
        if has_bonus_marker(content, "👑"):

            if not active_until(
                m.get("crown_until")
            ):
                asyncio.create_task(
                    temp_message(
                        msg.channel,
                        f"👑 {msg.author.mention} "
                        f"ta couronne n'est pas active."
                    )
                )
                return

            asyncio.create_task(
                temp_message(
                    msg.channel,
                    f"👑 {msg.author.mention} "
                    f"lien bonus Couronne validé."
                )
            )

            return

        # DIAMANT
        if has_bonus_marker(content, "💎"):

            if not active_until(
                m.get("diamond_until")
            ):
                asyncio.create_task(
                    temp_message(
                        msg.channel,
                        f"💎 {msg.author.mention} "
                        f"ton diamant n'est pas actif."
                    )
                )
                return

            asyncio.create_task(
                temp_message(
                    msg.channel,
                    f"💎 {msg.author.mention} "
                    f"lien bonus Diamant validé."
                )
            )

            return

    # PREMIER LIEN NORMAL = +1 PP
    if uid not in session["normal"]:

        session["normal"].add(uid)
        session["participants"].add(uid)

        current = await award_pp(msg.author)

        await temp_message(
            msg.channel,
            f"💗 {msg.author.mention} *+1 PP* — "
            f"tu es maintenant à *{current}/6 PP* cette semaine."
        )


# ============================================================
# VENTES
# ============================================================

async def sale(msg):

    mid = str(msg.id)

    async with data_lock:

        sales_messages = DATA.setdefault(
            "sales_messages",
            []
        )

        if mid in sales_messages:
            return

        sales_messages.append(mid)

        DATA["sales_messages"] = (
            sales_messages[-5000:]
        )

        m = md(msg.author.id)

        m["sales_week"] += 1
        m["bows"] += 1

        sales = m["sales_week"]
        bows = m["bows"]

        save()

    await temp_message(
        msg.channel,
        f"🎀 {msg.author.mention} **+1 vente !**\n"
        f"🛍️ Tu es maintenant à*{sales} vente(s)** cette semaine.\n"
        f"🎀 *+1 nœud gagné* — tu en as *{bows}*."
    )


# ============================================================
# STATS
# ============================================================

async def send_stats(member):

    m = md(member.id)

    text = (
        "*TES STATS LADY*** 🌸\n\n"
        f"💗 Participations :*{m['pp_week']} PP** cette semaine\n"
        f"🏆 Record : **{m['pp_record']} PP**\n"
        f"⚠️ Avertissements : **{m['warnings']}/3**\n"
        f"🎁 Cadeaux : **{m['gifts']}**\n"
        f"🎀 Nœuds : **{m['bows']}**\n"
        f"👑 Couronne : **{remaining(m.get('crown_until'))}**\n"
        f"💎 Diamant : **{remaining(m.get('diamond_until'))}**\n"
        f"🎂 Anniversaire : **{remaining(m.get('birthday_until'))}**\n"
        f"🛍️ Ventes semaine : **{m['sales_week']}**\n\n"
        f"🌷 Semaine : "
       f"*{'VALIDÉE ✅' if m['pp_week'] >= 6 else 'À FAIRE 🌸'}*"

    )

    try:
        await member.send(text)

    except discord.Forbidden:
        pass


# ============================================================
# COMMANDES ADMIN
# ============================================================

def admin(ctx):
    return bool(
        ctx.guild
        and ctx.author.guild_permissions.administrator
    )


@bot.command()
async def troc(
    ctx,
    member: discord.Member,
    gifts: int
):

    if not admin(ctx):
        return

    if gifts != 6:
        await ctx.send(
            "Troc prévu *6 🎁 = 1 🎀***."
        )
        return

    async with data_lock:

        m = md(member.id)

        if m["gifts"] < 6:
            await ctx.send(
                "Pas assez de 🎁."
            )
            return

        m["gifts"] -= 6
        m["bows"] += 1

        save()

    await ctx.send(
        f"🎀 {member.mention} *6 🎁 → 1 🎀***."
    )


@bot.command()
async def absence(
    ctx,
    member: discord.Member,
    debut: str,
    fin: str
):

    if not admin(ctx):
        return

    async with data_lock:

        DATA.setdefault(
            "absences",
            {}
        )[str(member.id)] = {
            "debut": debut,
            "fin": fin,
        }

        save()

    await ctx.send(
        f"💌 Absence enregistrée pour {member.mention}."
    )


@bot.command()
async def absences(ctx):

    if not admin(ctx):
        return

    abs_data = DATA.setdefault(
        "absences",
        {}
    )

    if not abs_data:
        await ctx.send(
            "💌 Aucune absence enregistrée."
        )
        return

    lines = []

    for uid, info in abs_data.items():

        lines.append(
            f"<@{uid}> : "
            f"**{info.get('debut', '?')} → "
            f"{info.get('fin', '?')}**"
        )

    await ctx.send(
        "💌 **Absences enregistrées**\n"
        + "\n".join(lines)
    )


# ============================================================
# EVENEMENTS
# ============================================================

@bot.event
async def on_message(msg):

    if msg.author.bot:
        return

    # STATS
    if (
        (msg.content or "")
        .strip()
        .lower()
        == "lady_stat"
    ):
        await send_stats(msg.author)
        return

    # VENTES
    if (
        msg.channel.id == SALON_VENTES_ID
        and msg.attachments
    ):
        await sale(msg)

    # LIENS VINTED
    if (
        msg.channel.id == SALON_SESSIONS_ID
        and session
        and VINTED.search(msg.content or "")
    ):

        # Lady compte d'abord la participation / le bonus.
        await participation(msg)

        # Puis elle masque automatiquement
        # la photo / l'aperçu Vinted sous le lien.
        # Le lien et le message de la membre restent visibles.
        try:
            await msg.edit(suppress=True)

        except (
            discord.Forbidden,
            discord.HTTPException
        ):
            pass

    await bot.process_commands(msg)


@bot.event
async def on_ready():

    print(
        f"Lady connectée : "
        f"{bot.user} ({bot.user.id})"
    )

    print(
        f"Fichier de données : {DATA_FILE}"
    )

    if not scheduler.is_running():
        scheduler.start()


# ============================================================
# LANCEMENT
# ============================================================

bot.run(TOKEN)
Rédiger
