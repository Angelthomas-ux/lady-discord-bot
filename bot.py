import os
import json
import random
import asyncio
import re
from datetime import datetime, timedelta, time, date
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

# ============================================================
# LADY — BASE ACTUELLE + AJOUTS VALIDÉS
# ============================================================

TIMEZONE = ZoneInfo("Europe/Paris")

SALON_SESSIONS_ID = 1521853338918977558
SALON_VENTES_ID = 1528658847806390382
SALON_JEU_ID = 1541713193762820106
SALON_DISCUSSION_ID = 1528130797029167134
SALON_ANNIVERSAIRES_ID = 1541680943583068200
SALON_GROUPE_SESSION_ID = 1549453941367111690
SALON_TROC_ID = 1549331152601489448
SALON_ADMIN_ID = 1541683049320681523

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
intents.invites = True

bot = commands.Bot(command_prefix="lady_", intents=intents, help_command=None)

VINTED = re.compile(r"(?:https?://)?(?:www\.)?vinted\.[^\s/]+/[^\s]+", re.I)
data_lock = asyncio.Lock()
session_lock = asyncio.Lock()
weekly_lock = asyncio.Lock()
quiz_lock = asyncio.Lock()

# ============================================================
# DONNEES
# ============================================================

def fresh_data():
    return {
        "members": {},
        "sales_messages": [],
        "absences": {},
        "session_claims": [],
        "weekly_closures": [],
        "sponsor_joins": [],
        "quiz_claims": [],
        "sales_daily": {},
        "sales_monthly": {},
        "monthly_sales_announced": [],
        "group_session_chain": [],
        "sunday_reminders": [],
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
        raw.setdefault("weekly_closures", [])
        raw.setdefault("sponsor_joins", [])
        raw.setdefault("quiz_claims", [])
        raw.setdefault("sales_daily", {})
        raw.setdefault("sales_monthly", {})
        raw.setdefault("monthly_sales_announced", [])
        raw.setdefault("group_session_chain", [])
        return raw

    except Exception as exc:
        print("Erreur lecture lady_data.json:", exc)
        return fresh_data()


DATA = load_data()
DATA.setdefault("sunday_reminders", [])
DATA.setdefault("group_session_chain", [])
DATA.setdefault("birthdays", {})


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


def sunday_maintenance(n=None):
    n = n or now()
    return n.weekday() == 6 and n.time() >= time(23, 50)

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
    "🛍️ Lot et offre",
    "☀️ Printemps/Été",
    "🍂 Automne/Hiver",
    "🧸 Enfants",
    "🌈 Couleurs",
    "💄 Bijoux / Makeup & Accessoires",
    "👗 Femme",
    "👔 Homme",
    "⚠️ Retrait d'un avertissement",
    "💔 Retrait de favoris",
    "✖️ Multiplicateur x2",
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
    "🛍️ Lot et offre": ("IMG_9593.jpeg", "IMG_9594.jpeg"),
    "☀️ Printemps/Été": ("IMG_9591.jpeg", "IMG_9592.jpeg"),
    "🍂 Automne/Hiver": ("IMG_9589.jpeg", "IMG_9590.jpeg"),
    "🧸 Enfants": ("IMG_9587.jpeg", "IMG_9588.jpeg"),
    "🌈 Couleurs": ("IMG_9585.jpeg", "IMG_9586.jpeg"),
    "💄 Bijoux / Makeup & Accessoires": ("IMG_9603.jpeg", "IMG_9604.jpeg"),
    "👗 Femme": ("IMG_9601.jpeg", "IMG_9602.jpeg"),
    "👔 Homme": ("IMG_9599.jpeg", "IMG_9600.jpeg"),
    "⚠️ Retrait d'un avertissement": ("IMG_9597.jpeg", "IMG_9598.jpeg"),
    "💔 Retrait de favoris": ("IMG_9595.jpeg", "IMG_9596.jpeg"),
    "✖️ Multiplicateur x2": ("IMG_9654.jpeg", "B0B07086-0369-4BB5-9664-BBB886307BBF.png"),
}

session = None
last_free_session = None
pending_return_checks = set()


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
    return datetime.combine(tomorrow, FIXED[0][1], tzinfo=TIMEZONE)


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
    lot_count = random.randint(2, 10) if name == "🛍️ Lot et offre" else None
    chosen_color = random.choice(["rose", "rouge", "bleu", "vert", "jaune", "orange", "violet", "noir", "blanc", "beige", "gris", "marron"]) if name == "🌈 Couleurs" else None

    session = {
        "name": name,
        "start": start,
        "end": end,
        "kind": kind,
        "free": is_free,
        "normal": set(),
        "participants": set(),
        "dressing_count": dressing_count,
        "lot_count": lot_count,
        "links": {},
        "no_return_links": set(),
        "mega_10_unlocked": False,
        "mega_10_rewarded": set(),
    }

    # Garde la session active en mémoire persistante : si Railway redémarre,
    # Lady sait encore quelle image STOP envoyer.
    async with data_lock:
        DATA["active_session"] = {
            "name": name,
            "end": end.isoformat(),
            "kind": kind,
        }
        save()

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
    elif name == "🛍️ Lot et offre":
        text = (
            f"🛍️ **SESSION LOT ET OFFRE**\n"
            f"Lady a choisi un lot de **{lot_count} articles**.\n"
            f"Créez un lot de {lot_count} articles chez les autres participantes et faites une offre.\n"
            f"⏰ Fin à *{end.strftime('%H:%M')}*."
        )
    elif name == "🌈 Couleurs":
        text = (
            f"🌈 **SESSION COULEURS**\n"
            f"Lady a choisi la couleur : **{chosen_color.upper()}** 🎨\n"
            f"⏰ Fin à *{end.strftime('%H:%M')}*."
        )
    elif name == "💔 Retrait de favoris":
        text = (
            f"💔 **SESSION RETRAIT DE FAVORIS**\n"
            f"Posez le lien de votre dressing et retirez les favoris des autres dressings.\n"
            f"💗 Cette session compte bien **+1 PP**.\n"
            f"⏰ Fin à *{end.strftime('%H:%M')}*."
        )
    elif name == "✖️ Multiplicateur x2":
        text = (
            "@everyone\n"
            "✖️ **SESSION MULTIPLICATEUR ×2**\n"
            "💗 Chaque participation validée rapporte **+2 PP au lieu de +1 PP** !\n"
            "🚫 **Aucun bonus n'est autorisé** pendant cette session : 🎁 🎀 👑 💎 🎂\n"
            f"⏰ Fin à *{end.strftime('%H:%M')}*."
        )
    else:
        text = f"✨ **{name}**\n⏰ Fin à *{end.strftime('%H:%M')}*."

    await channel.send(text)

    if kind == "mega":
        discussion = bot.get_channel(SALON_DISCUSSION_ID)
        if discussion:
            try:
                await discussion.send("@everyone 🚀 *Le Méga Boost vient de commencer !*")
            except Exception:
                pass

    print(f"Session lancée: {name} jusqu'à {end:%H:%M}")


async def missing_return_messages(member, old_session, channel):
    no_return_links = old_session.get("no_return_links", set())
    links = old_session.get("links", {})
    if any(links.get(message_id) == member.id for message_id in no_return_links):
        return []

    missing = []
    for message_id, owner_id in links.items():
        if owner_id == member.id:
            continue
        try:
            message = await channel.fetch_message(message_id)
        except Exception:
            continue

        reacted = False
        for reaction in message.reactions:
            try:
                async for user in reaction.users():
                    if user.id == member.id:
                        reacted = True
                        break
            except Exception:
                pass
            if reacted:
                break
        if not reacted:
            missing.append(message)
    return missing


async def member_has_returned(member, old_session, channel):
    return not await missing_return_messages(member, old_session, channel)


async def return_check_after_10_minutes(old_session, channel):
    key = f"{old_session['name']}|{old_session['start'].isoformat()}"
    if key in pending_return_checks:
        return
    pending_return_checks.add(key)
    try:
        participants = list(old_session.get("participants", set()))
        if not participants or not old_session.get("links"):
            return

        waiting = {}
        for uid in participants:
            member = channel.guild.get_member(uid)
            if not member:
                continue
            missing = await missing_return_messages(member, old_session, channel)
            if not missing:
                continue

            waiting[uid] = member
            jump_links = "\\n".join(f"🔗 {message.jump_url}" for message in missing)
            try:
                if is_lady_admin(member):
                    await member.send(
                        f"⚠️ **{old_session['name']} terminée**\\n\\n"
                        f"Tu n'es pas encore à jour ! Il te manque **{len(missing)} lien(s)**.\\n"
                        "Tu as **10 minutes** pour terminer tes retours. **Aucun avertissement ne sera ajouté.**\\n\\n"
                        f"**Liste des liens manquants :**\\n{jump_links}"
                    )
                else:
                    await member.send(
                        f"⚠️ **{old_session['name']} terminée**\\n\\n"
                        f"Tu n'es pas encore à jour ! Il te manque **{len(missing)} lien(s)**.\\n"
                        "Tu as **10 minutes** pour te mettre à jour, sinon tu recevras un avertissement.\\n\\n"
                        f"**Liste des liens manquants :**\\n{jump_links}"
                    )
            except Exception:
                pass

        if not waiting:
            return

        deadline = now() + timedelta(minutes=10)
        while waiting and now() < deadline:
            await asyncio.sleep(15)
            for uid, member in list(waiting.items()):
                if await member_has_returned(member, old_session, channel):
                    try:
                        await member.send(
                            "✅ **Tu es à jour !**\\n"
                            "Tous les liens de la session ont bien été rendus.\\n"
                            "**Aucun avertissement.** 🌸"
                        )
                    except Exception:
                        pass
                    waiting.pop(uid, None)

        for uid, member in list(waiting.items()):
            if await member_has_returned(member, old_session, channel):
                try:
                    await member.send(
                        "✅ **Tu es à jour !**\\n"
                        "Tous les liens de la session ont bien été rendus.\\n"
                        "**Aucun avertissement.** 🌸"
                    )
                except Exception:
                    pass
                continue

            if is_lady_admin(member):
                continue

            async with data_lock:
                m = md(member.id)
                m["warnings"] = int(m.get("warnings", 0)) + 1
                warnings = m["warnings"]
                save()

            try:
                await member.send(
                    f"⚠️ Tu n'étais toujours pas à jour après les 10 minutes : "
                    f"**+1 avertissement ({warnings}/3)**."
                )
            except Exception:
                pass

            if warnings >= 3:
                try:
                    await member.timeout(
                        timedelta(hours=24),
                        reason="Lady : 3 avertissements — blocage 24 h"
                    )
                    try:
                        await member.send("⛔ Tu as atteint **3 avertissements** : blocage pendant **24 h**.")
                    except Exception:
                        pass
                except Exception as exc:
                    print(f"Impossible de timeout {member}: {exc}")
    finally:
        pending_return_checks.discard(key)


def is_lady_admin(member):
    """Admin Lady = permission Administrateur OU accès au salon privé admin."""
    if getattr(member.guild_permissions, "administrator", False):
        return True
    try:
        admin_channel = member.guild.get_channel(SALON_ADMIN_ID)
        return bool(admin_channel and admin_channel.permissions_for(member).view_channel)
    except Exception:
        return False


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

    async with data_lock:
        DATA.pop("active_session", None)
        save()

    participants = list(old["participants"])
    await channel.send(
        f"⛔ **Fin de {old['name']}**\n"
        f"👥 *{len(participants)} participante(s)*."
    )

    # Session spéciale : chaque participante gagne son PP normalement et,
    # si elle possède au moins un avertissement, Lady lui en retire 1 à la fin.
    if old["name"] == "⚠️ Retrait d'un avertissement" and participants:
        removed_lines = []
        async with data_lock:
            for uid in participants:
                m = md(uid)
                before = int(m.get("warnings", 0))
                if before > 0:
                    m["warnings"] = before - 1
                    member = channel.guild.get_member(uid)
                    mention = member.mention if member else f"<@{uid}>"
                    removed_lines.append(
                        f"{mention} → **-1 avertissement** ({m['warnings']}/3)"
                    )
            save()
        if removed_lines:
            await channel.send(
                "⚠️ **Retrait d'avertissement :**\n" + "\n".join(removed_lines)
            )

    # Contrôle des rendus sur les sessions normales, sauf Retrait de favoris :
    # cette session consiste justement à retirer des favoris et ne doit pas
    # déclencher le contrôle habituel des réactions.
    if old["kind"] != "mega" and old["name"] != "💔 Retrait de favoris":
        asyncio.create_task(return_check_after_10_minutes(old, channel))

    if old["kind"] == "mega" and participants:
        if len(participants) >= 3:
            winners = random.sample(participants, 3)
            rewards = ["gift", "bow", "crown"]
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
                        m["crown_until"] = (now() + timedelta(hours=24)).isoformat()
                        label = "👑 Couronne pendant 24 h"

                    mention = member.mention if member else f"<@{uid}>"
                    lines.append(f"{mention} → *{label}*")
                save()

            await channel.send("🎉 **Gagnantes du Méga Boost :**\n" + "\n".join(lines))
        else:
            await channel.send(
                "💗 Il faut au moins *3 participantes différentes* "
                "pour le tirage des 3 bonus du Méga Boost."
            )


async def launch_free(n=None):
    global last_free_session
    n = n or now()

    if sunday_maintenance(n):
        return

    choices = [x for x in FREE_SESSIONS if x != last_free_session]

    # Retrait de favoris : maximum 3 lancements par jour.
    today_prefix = n.date().isoformat()
    retrait_count = 0
    for claim in DATA.get("session_claims", []):
        if "💔 Retrait de favoris" in str(claim) and today_prefix in str(claim):
            retrait_count += 1
    if retrait_count >= 3:
        choices = [x for x in choices if x != "💔 Retrait de favoris"]

    if not choices:
        choices = [x for x in FREE_SESSIONS if x != "💔 Retrait de favoris" or retrait_count < 3]

    name = random.choice(choices)
    end = min(n + timedelta(minutes=10), next_fixed_start(n))

    # Le dimanche, aucune session ne doit dépasser 23h50.
    if n.weekday() == 6:
        maintenance_start = datetime.combine(n.date(), time(23, 50), tzinfo=TIMEZONE)
        if n < maintenance_start:
            end = min(end, maintenance_start)

    if (end - n).total_seconds() < 30:
        return

    if not await claim_session(name, n.replace(second=0, microsecond=0)):
        return

    last_free_session = name
    await begin_session(name, n, end, "normal", is_free=True)


@tasks.loop(seconds=20)
async def scheduler():
    global session
    n = now()

    async with session_lock:
        # Maintenance du dimanche : aucune session de 23h50 à minuit.
        if sunday_maintenance(n):
            if session:
                await finish_session()
            return

        if session and n >= session["end"]:
            await finish_session()

        fixed = current_fixed(n)
        if fixed:
            name, start, end, kind = fixed

            if session and session["name"] == name and session["start"] == start:
                return

            if session:
                await finish_session()

            if await claim_session(name, start):
                await begin_session(name, start, end, kind, is_free=False)
            return

        if session is None:
            await launch_free(n)


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()

# ============================================================
# MESSAGES TEMPORAIRES
# ============================================================

async def temp_message(channel, text, seconds=3):
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

async def set_week_roles(member, rose=False, green=False, orange=False, red=False):
    wanted = {
        ROLE_ROSE: rose,
        ROLE_VERT: green,
        ROLE_ORANGE: orange,
        ROLE_ROUGE: red,
    }

    for role_name, should_have in wanted.items():
        role = discord.utils.get(member.guild.roles, name=role_name)
        if not role:
            continue
        try:
            if should_have and role not in member.roles:
                await member.add_roles(role, reason="Mise à jour Lady")
            elif not should_have and role in member.roles:
                await member.remove_roles(role, reason="Mise à jour Lady")
        except Exception as exc:
            print(f"Erreur rôle {role_name} pour {member}: {exc}")


async def update_week_role(member):
    if not isinstance(member, discord.Member):
        return

    m = md(member.id)
    if m["pp_week"] >= 6:
        # À 6 PP, on force réellement le passage en vert pour tout le monde.
        await set_week_roles(member, green=True)
    else:
        # On ne retire pas un orange/rouge historique en pleine semaine.
        orange = discord.utils.get(member.guild.roles, name=ROLE_ORANGE)
        red = discord.utils.get(member.guild.roles, name=ROLE_ROUGE)
        if (orange and orange in member.roles) or (red and red in member.roles):
            return
        await set_week_roles(member, rose=True)

# ============================================================
# PP ET BONUS
# ============================================================

async def award_pp(member):
    async with data_lock:
        m = md(member.id)
        before = int(m["pp_week"])
        m["pp_week"] = before + 1
        m["pp_record"] = max(int(m.get("pp_record", 0)), m["pp_week"])

        if m["pp_week"] // 6 > before // 6:
            m["gifts"] += 1

        if m["pp_week"] // 20 > before // 20:
            m["diamond_until"] = (now() + timedelta(hours=24)).isoformat()

        current = m["pp_week"]
        save()

    await update_week_role(member)
    return current


def has_bonus_marker(content, marker):
    # Discord/mobile peut ajouter le sélecteur emoji U+FE0F.
    clean = (content or "").replace("\ufe0f", "")
    wanted = marker.replace("\ufe0f", "")
    return wanted in clean


async def participation(msg):
    global session
    if not session:
        return

    uid = msg.author.id
    content = msg.content or ""
    kind = session["kind"]

    if kind == "mega":
        if any(x in content for x in ("🎁", "🎀", "👑", "💎", "🎂")):
            await temp_message(
                msg.channel,
                f"🚀 {msg.author.mention} aucun bonus 🎁 🎀 👑 💎 🎂 n'est utilisable pendant le Méga Boost."
            )
            return

        if uid not in session["normal"]:
            session["normal"].add(uid)
            session["participants"].add(uid)
            current = await award_pp(msg.author)
            await temp_message(
                msg.channel,
                f"💗 {msg.author.mention} *+1 PP* — tu es maintenant à *{current}/6 PP* cette semaine."
            )

            if len(session["participants"]) >= 10:
                to_reward = [
                    participant_id
                    for participant_id in session["participants"]
                    if participant_id not in session["mega_10_rewarded"]
                ]
                if to_reward:
                    async with data_lock:
                        for participant_id in to_reward:
                            md(participant_id)["bows"] += 1
                            session["mega_10_rewarded"].add(participant_id)
                        save()

                    first_unlock = not session["mega_10_unlocked"]
                    session["mega_10_unlocked"] = True
                    if first_unlock:
                        await msg.channel.send(
                            "🎉 **10 participantes au Méga Boost !**\\n"
                            "Toutes les participantes de ce Méga gagnent **+1 🎀 lien sans rendre** !"
                        )
                    elif uid in to_reward:
                        await temp_message(
                            msg.channel,
                            f"🎀 {msg.author.mention} le Méga a déjà atteint 10 participantes : "
                            "tu gagnes **+1 🎀 lien sans rendre**."
                        )
        return

    # Session Multiplicateur ×2 : aucun bonus, un seul lien normal = +2 PP.
    if session["name"] == "✖️ Multiplicateur x2":
        if any(has_bonus_marker(content, marker) for marker in ("🎁", "🎀", "👑", "💎", "🎂")):
            try:
                await msg.author.send(
                    "🚫 **Session Multiplicateur ×2** : les bonus 🎁 🎀 👑 💎 🎂 sont interdits pendant cette session. "
                    "Envoie uniquement ton lien normal pour gagner +2 PP."
                )
            except Exception:
                pass
            await temp_message(
                msg.channel,
                f"🚫 {msg.author.mention} les bonus sont interdits pendant la Session Multiplicateur ×2."
            )
            return

        session["links"][msg.id] = uid
        if uid not in session["normal"]:
            session["normal"].add(uid)
            session["participants"].add(uid)
            current = await award_pp(msg.author)
            current = await award_pp(msg.author)
            await temp_message(
                msg.channel,
                f"✖️ {msg.author.mention} **+2 PP** — tu es maintenant à *{current}/6 PP* cette semaine."
            )
        return

    async with data_lock:
        m = md(uid)

        if has_bonus_marker(content, "🎀"):
            if m["bows"] <= 0:
                asyncio.create_task(temp_message(msg.channel, f"🎀 {msg.author.mention} tu n'as pas de nœud disponible."))
                return
            m["bows"] -= 1
            save()
            session["participants"].add(uid)
            session["links"][msg.id] = uid
            session["no_return_links"].add(msg.id)
            asyncio.create_task(temp_message(
                msg.channel,
                f"🎀 {msg.author.mention} ton lien sans rendre est validé. Il te reste *{m['bows']} nœud(s)*."
            ))
            return

        if has_bonus_marker(content, "🎁"):
            if m["gifts"] <= 0:
                asyncio.create_task(temp_message(msg.channel, f"🎁 {msg.author.mention} tu n'as pas de cadeau disponible."))
                return
            m["gifts"] -= 1
            save()
            session["links"][msg.id] = uid
            asyncio.create_task(temp_message(
                msg.channel,
                f"🎁 {msg.author.mention} ton lien supplémentaire est validé. Il te reste *{m['gifts']} cadeau(x)*."
            ))
            return

        if has_bonus_marker(content, "👑"):
            if not active_until(m.get("crown_until")):
                asyncio.create_task(temp_message(msg.channel, f"👑 {msg.author.mention} ta couronne n'est pas active."))
                return
            session["links"][msg.id] = uid
            asyncio.create_task(temp_message(msg.channel, f"👑 {msg.author.mention} lien bonus Couronne validé."))
            return

        if has_bonus_marker(content, "🎂"):
            if not active_until(m.get("birthday_until")):
                asyncio.create_task(temp_message(msg.channel, f"🎂 {msg.author.mention} ton bonus anniversaire n'est pas actif."))
                return
            session["links"][msg.id] = uid
            asyncio.create_task(temp_message(msg.channel, f"🎂 {msg.author.mention} lien bonus Anniversaire validé."))
            return

        if has_bonus_marker(content, "💎"):
            if not active_until(m.get("diamond_until")):
                asyncio.create_task(temp_message(msg.channel, f"💎 {msg.author.mention} ton diamant n'est pas actif."))
                return
            session["links"][msg.id] = uid
            asyncio.create_task(temp_message(msg.channel, f"💎 {msg.author.mention} lien bonus Diamant validé."))
            return

    # Tout lien normal est enregistré pour le contrôle des rendus.
    session["links"][msg.id] = uid

    if uid not in session["normal"]:
        session["normal"].add(uid)
        session["participants"].add(uid)
        current = await award_pp(msg.author)
        await temp_message(
            msg.channel,
            f"💗 {msg.author.mention} *+1 PP* — tu es maintenant à *{current}/6 PP* cette semaine."
        )

# ============================================================
# SALON SANS SESSION — CHAÎNE DES 3 LIENS
# ============================================================

def group_chain_entries():
    chain = DATA.setdefault("group_session_chain", [])
    if not isinstance(chain, list):
        chain = []
        DATA["group_session_chain"] = chain
    return chain


async def validate_group_session_entry(entry, channel):
    if entry.get("validated"):
        return False

    member = channel.guild.get_member(int(entry["user_id"])) if channel.guild else None
    if member is None:
        return False

    required_ids = [int(x) for x in entry.get("required_message_ids", [])]
    for message_id in required_ids:
        try:
            previous = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

        reacted = False
        for reaction in previous.reactions:
            try:
                async for user in reaction.users():
                    if user.id == member.id:
                        reacted = True
                        break
            except (discord.Forbidden, discord.HTTPException):
                pass
            if reacted:
                break
        if not reacted:
            return False

    async with data_lock:
        persistent = next(
            (e for e in group_chain_entries()
             if str(e.get("message_id")) == str(entry.get("message_id"))),
            None,
        )
        if not persistent or persistent.get("validated"):
            return False
        persistent["validated"] = True
        save()

    current = await award_pp(member)
    await temp_message(
        channel,
        f"💗 {member.mention} **participation validée ! +1 PP** — "
        f"tu es maintenant à **{current}/6 PP** cette semaine."
    )
    return True


async def group_session_post(msg):
    uid = msg.author.id
    content = msg.content or ""

    # Aucun bonus Lady dans ce salon.
    if any(has_bonus_marker(content, x) for x in ("🎁", "🎀", "👑", "💎", "🎂")):
        try:
            await msg.author.send(
                "❌ Aucun bonus Lady (🎁 🎀 👑 💎 🎂) n'est utilisable dans le salon sans session. "
                "Poste uniquement ton lien Vinted normal."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await msg.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        return

    async with data_lock:
        chain = group_chain_entries()
        last_three = chain[-3:]
        allowed = not any(int(e.get("user_id", 0)) == uid for e in last_three)

        if allowed:
            required_ids = [str(e["message_id"]) for e in last_three]
            entry = {
                "message_id": str(msg.id),
                "user_id": str(uid),
                "required_message_ids": required_ids,
                "validated": False,
                "created_at": now().isoformat(),
            }
            chain.append(entry)
            DATA["group_session_chain"] = chain[-1000:]
            save()
        else:
            required_ids = []
            entry = None

    if not allowed:
        try:
            await msg.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await temp_message(
            msg.channel,
            f"⏳ {msg.author.mention} tu dois attendre que **3 autres personnes** "
            "aient posté après ton dernier lien avant de pouvoir reposter."
        )
        return

    if not required_ids:
        # Première personne de la chaîne : rien à rendre au-dessus, donc +1 PP directement.
        await validate_group_session_entry(entry, msg.channel)
    else:
        await temp_message(
            msg.channel,
            f"🔗 {msg.author.mention} ton passage est enregistré. "
            f"Fais les **{len(required_ids)} lien(s) juste au-dessus** : "
            "Lady validera automatiquement ton **+1 PP** dès que tu seras à jour."
        )


async def check_group_session_pending_for(user_id, channel):
    async with data_lock:
        pending = [
            dict(e) for e in group_chain_entries()
            if int(e.get("user_id", 0)) == int(user_id) and not e.get("validated")
        ]
    for entry in pending:
        await validate_group_session_entry(entry, channel)


@bot.event
async def on_raw_reaction_add(payload):
    if bot.user and payload.user_id == bot.user.id:
        return
    if payload.channel_id != SALON_GROUPE_SESSION_ID:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return

    await check_group_session_pending_for(payload.user_id, channel)


# ============================================================
# VENTES
# ============================================================

async def sale(msg):
    mid = str(msg.id)
    n = now()
    day_key = n.date().isoformat()
    month_key = n.strftime("%Y-%m")
    uid = str(msg.author.id)

    async with data_lock:
        sales_messages = DATA.setdefault("sales_messages", [])
        if mid in sales_messages:
            return

        sales_messages.append(mid)
        DATA["sales_messages"] = sales_messages[-5000:]

        m = md(msg.author.id)
        m["sales_week"] += 1
        m["bows"] += 1
        bows = m["bows"]

        daily = DATA.setdefault("sales_daily", {}).setdefault(day_key, {})
        daily[uid] = int(daily.get(uid, 0)) + 1

        monthly = DATA.setdefault("sales_monthly", {}).setdefault(month_key, {})
        monthly[uid] = int(monthly.get(uid, 0)) + 1

        member_today = daily[uid]
        total_today = sum(int(v) for v in daily.values())
        ranking = sorted(daily.items(), key=lambda item: (-int(item[1]), item[0]))[:5]
        save()

    lines = [
        f"🎀 {msg.author.mention} **vente enregistrée ✅**",
        f"💶 Tes ventes aujourd’hui : **{member_today}**",
        f"🎀 Nœuds : **{bows}**",
        "",
        f"🤑 Total ventes aujourd’hui : **{total_today}**",
        "",
        "🏆 **Top ventes du jour**",
    ]
    for pos, (member_id, count) in enumerate(ranking, start=1):
        member = msg.guild.get_member(int(member_id)) if msg.guild else None
        name = member.display_name if member else f"<@{member_id}>"
        lines.append(f"**{pos}.** {name} — **{count} vente(s)**")

    # Le récap des ventes reste visible dans le salon des ventes.
    await msg.channel.send("\n".join(lines))


# ============================================================
# CLÔTURE HEBDOMADAIRE — DIMANCHE 23H50
# ============================================================

def is_absent_for_week(uid, sunday_date):
    info = DATA.setdefault("absences", {}).get(str(uid))
    if not info:
        return False

    def parse_day(value):
        if not value:
            return None
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m"):
            try:
                d = datetime.strptime(value, fmt)
                if fmt == "%d/%m":
                    d = d.replace(year=sunday_date.year)
                return d.date()
            except Exception:
                pass
        return None

    start = parse_day(info.get("debut"))
    end = parse_day(info.get("fin"))
    if not start or not end:
        return True

    monday = sunday_date - timedelta(days=6)
    return start <= sunday_date and end >= monday


def is_absent_this_week(uid):
    today = now().date()
    sunday = today + timedelta(days=(6 - today.weekday()))
    return is_absent_for_week(uid, sunday)


@tasks.loop(minutes=1)
async def sunday_reminder_scheduler():
    n = now()
    if n.weekday() != 6 or n.hour != 10:
        return
    day_key = n.date().isoformat()
    if day_key in DATA.setdefault("sunday_reminders", []):
        return

    for guild in bot.guilds:
        channel = guild.get_channel(SALON_DISCUSSION_ID)
        if channel is None:
            continue
        mentions = [
            member.mention for member in guild.members
            if not member.bot
            and not is_absent_this_week(member.id)
            and int(md(member.id).get("pp_week", 0)) < 6
        ]
        if mentions:
            chunks, current = [], ""
            for mention in mentions:
                if len(current) + len(mention) + 1 > 1500:
                    chunks.append(current.strip()); current = ""
                current += mention + " "
            if current.strip():
                chunks.append(current.strip())
            await channel.send(
                "⚠️ **DERNIER JOUR POUR VALIDER VOTRE SEMAINE !**\n"
                + chunks[0]
                + "\nIl ne vous reste plus qu’aujourd’hui pour atteindre vos **6 PP** "
                  "et valider votre semaine 🌸 Pensez à participer aux sessions de la journée 💗"
            )
            for chunk in chunks[1:]:
                await channel.send(chunk)

    async with data_lock:
        DATA.setdefault("sunday_reminders", []).append(day_key)
        DATA["sunday_reminders"] = DATA["sunday_reminders"][-12:]
        save()


@sunday_reminder_scheduler.before_loop
async def before_sunday_reminder_scheduler():
    await bot.wait_until_ready()


async def weekly_close(guild, closure_date):
    key = closure_date.isoformat()

    async with weekly_lock:
        async with data_lock:
            closures = DATA.setdefault("weekly_closures", [])
            if key in closures:
                return


        members_with_data = []
        for member in guild.members:
            if member.bot:
                continue
            m = md(member.id)
            members_with_data.append((member, int(m.get("pp_week", 0)), int(m.get("sales_week", 0))))

        # Récompenses hebdomadaires : uniquement si le meilleur score est > 0.
        max_pp = max((pp for _, pp, _ in members_with_data), default=0)
        max_sales = max((sales for _, _, sales in members_with_data), default=0)

        pp_winners = [member for member, pp, _ in members_with_data if max_pp > 0 and pp == max_pp]
        sales_winners = [member for member, _, sales in members_with_data if max_sales > 0 and sales == max_sales]


        # Exception demandée : dimanche 13/09/2026, aucun orange ni rouge.
        grace_night = closure_date == date(2026, 9, 13)

        for member, pp, _ in members_with_data:
            if grace_night:
                await set_week_roles(member, rose=True)
                continue

            if pp >= 6:
                # La semaine était validée. Nouvelle semaine : retour au rose.
                await set_week_roles(member, rose=True)
                continue

            # Absence enregistrée ou arrivée pendant cette semaine : aucune sanction.
            joined = member.joined_at.astimezone(TIMEZONE) if member.joined_at else None
            monday = datetime.combine(closure_date - timedelta(days=6), time(0, 0), tzinfo=TIMEZONE)
            new_this_week = bool(joined and joined >= monday)

            if is_absent_for_week(member.id, closure_date) or new_this_week:
                await set_week_roles(member, rose=True)
                continue

            orange_role = discord.utils.get(guild.roles, name=ROLE_ORANGE)
            already_orange = bool(orange_role and orange_role in member.roles)

            if already_orange:
                await set_week_roles(member, red=True)
            else:
                await set_week_roles(member, orange=True)

        # Reset hebdomadaire après calcul des gagnants et des rôles.
        # Le record PP est conservé.
        async with data_lock:
            for member, _, _ in members_with_data:
                m = md(member.id)
                m["pp_week"] = 0
                m["sales_week"] = 0
                m["warnings"] = 0
                m["gifts"] = 0
                m["bows"] = 0
                m["crown_until"] = None
                m["diamond_until"] = None

            # Seuls les 🎀 gagnés à cette clôture passent dans la nouvelle semaine.
            for member in pp_winners:
                md(member.id)["bows"] += 1
            for member in sales_winners:
                md(member.id)["bows"] += 1

            # La clôture n'est marquée comme faite qu'après les traitements réussis.
            closures = DATA.setdefault("weekly_closures", [])
            if key not in closures:
                closures.append(key)
            DATA["weekly_closures"] = closures[-60:]
            save()

        discussion = bot.get_channel(SALON_DISCUSSION_ID)
        if discussion:
            lines = ["🌙 **Mise à jour hebdomadaire Lady terminée.**"]
            if pp_winners:
                lines.append("🎀 Meilleure(s) participation(s) : " + ", ".join(m.mention for m in pp_winners))
            if sales_winners:
                lines.append("🎀 Meilleure(s) vendeuse(s) : " + ", ".join(m.mention for m in sales_winners))
            lines.append("🌸 Les compteurs de la nouvelle semaine sont prêts.")
            try:
                await discussion.send("\n".join(lines))
            except Exception:
                pass

        print(f"Clôture hebdomadaire terminée pour {key}", flush=True)


@tasks.loop(seconds=20)
async def weekly_scheduler():
    n = now()
    if n.weekday() != 6 or not (time(23, 50) <= n.time() < time(23, 59, 59)):
        return

    print(f"Clôture hebdomadaire déclenchée à {n.isoformat()}", flush=True)
    for guild in bot.guilds:
        await weekly_close(guild, n.date())


@weekly_scheduler.before_loop
async def before_weekly_scheduler():
    await bot.wait_until_ready()

# ============================================================
# BILAN MENSUEL DES VENTES
# ============================================================

def previous_month_key(n):
    first = n.replace(day=1)
    previous = first - timedelta(days=1)
    return previous.strftime("%Y-%m"), previous.strftime("%m/%Y")


@tasks.loop(minutes=1)
async def monthly_sales_scheduler():
    n = now()
    # À 00:01 le 1er du mois, le mois précédent est définitivement terminé.
    if n.day != 1 or n.hour != 0 or n.minute > 2:
        return

    month_key, label = previous_month_key(n)
    claim = f"monthly-sales|{month_key}"

    async with data_lock:
        announced = DATA.setdefault("monthly_sales_announced", [])
        if claim in announced:
            return

        month_data = dict(DATA.setdefault("sales_monthly", {}).get(month_key, {}))
        total = sum(int(v) for v in month_data.values())
        ranking = sorted(month_data.items(), key=lambda item: (-int(item[1]), item[0]))[:5]

        announced.append(claim)
        DATA["monthly_sales_announced"] = announced[-120:]
        save()

    channel = bot.get_channel(SALON_VENTES_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SALON_VENTES_ID)
        except Exception as exc:
            print("Salon ventes inaccessible pour bilan mensuel :", exc)
            return

    lines = [
        f"📅 **Bilan des ventes — {label}**",
        f"🛍️ Le groupe a réalisé **{total} vente(s)** pendant le mois.",
    ]
    if ranking:
        lines += ["", "🏆 **Top ventes du mois**"]
        guild = channel.guild
        for pos, (member_id, count) in enumerate(ranking, start=1):
            member = guild.get_member(int(member_id)) if guild else None
            name = member.display_name if member else f"<@{member_id}>"
            lines.append(f"**{pos}.** {name} — **{count} vente(s)**")

    await channel.send("\n".join(lines))


@monthly_sales_scheduler.before_loop
async def before_monthly_sales_scheduler():
    await bot.wait_until_ready()


# ============================================================
# PARRAINAGE — +1 🎀 PAR NOUVELLE ARRIVÉE
# ============================================================

invite_cache = {}


async def refresh_invites(guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}
    except Exception as exc:
        print(f"Invitations non accessibles sur {guild.name}: {exc}")


@bot.event
async def on_member_join(member):
    guild = member.guild

    discussion = guild.get_channel(SALON_DISCUSSION_ID)
    if discussion:
        try:
            await discussion.send(
                f"🌸 **Bienvenue parmi nous {member.mention} !** 🌸\n"
                "Nous sommes heureux de t’accueillir dans le groupe 💕\n"
                "Pense à prendre connaissance des règles et n’hésite pas à venir discuter avec nous.\n"
                "✨ Bonne aventure parmi nous !"
            )
        except Exception as exc:
            print(f"Bienvenue impossible pour {member}: {exc}", flush=True)

    join_key = f"{guild.id}:{member.id}"

    async with data_lock:
        if join_key in DATA.setdefault("sponsor_joins", []):
            return

    try:
        invites = await guild.invites()
    except Exception as exc:
        print(f"Impossible de lire les invitations: {exc}")
        return

    previous = invite_cache.get(guild.id, {})
    used_invite = None
    for invite in invites:
        old_uses = previous.get(invite.code, 0)
        if (invite.uses or 0) > old_uses:
            used_invite = invite
            break

    invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

    if not used_invite or not used_invite.inviter:
        return

    inviter = guild.get_member(used_invite.inviter.id)
    if not inviter:
        return

    async with data_lock:
        joins = DATA.setdefault("sponsor_joins", [])
        if join_key in joins:
            return
        joins.append(join_key)
        DATA["sponsor_joins"] = joins[-5000:]
        md(inviter.id)["bows"] += 1
        bows = md(inviter.id)["bows"]
        save()

    try:
        await inviter.send(
            f"🎀 **Parrainage !** {member.display_name} vient de rejoindre le serveur grâce à ton invitation.\n"
            f"Tu gagnes **+1 🎀 = 1 lien sans rendre** — tu as maintenant **{bows} 🎀**."
        )
    except Exception:
        pass

# ============================================================
# JEUX / QUIZ — 6 PAR JOUR
# ============================================================

QUIZ_TIMES = [time(9, 45), time(11, 0), time(15, 0), time(18, 0), time(20, 0), time(22, 0)]

QUIZ_THEMES = {
    "🎵 Musique": [
        ("Quel groupe chantait Bohemian Rhapsody ?", ["queen"]),
        ("Quel chanteur est surnommé le King of Pop ?", ["michael jackson"]),
        ("Quel groupe français a chanté L'Aventurier ?", ["indochine"]),
        ("Quelle chanteuse interprète Rolling in the Deep ?", ["adele", "adèle"]),
        ("Quel duo français est connu pour les casques de robots ?", ["daft punk"]),
        ("Quel chanteur belge interprète Alors on danse ?", ["stromae"]),
        ("Quel groupe a pour chanteur Bono ?", ["u2"]),
        ("Quelle chanteuse a sorti l'album 21 ?", ["adele", "adèle"]),
        ("Quel chanteur français interprétait Allumer le feu ?", ["johnny hallyday", "johnny"]),
        ("Quelle chanteuse canadienne interprète Pour que tu m'aimes encore ?", ["céline dion", "celine dion"]),
        ("Quel groupe britannique a chanté Yellow ?", ["coldplay"]),
        ("Quel artiste interprète Formidable ?", ["stromae"]),
        ("Quel chanteur français est connu pour La Bohème ?", ["charles aznavour", "aznavour"]),
        ("Quel groupe a chanté I Want to Break Free ?", ["queen"]),
        ("Quelle chanteuse est surnommée la Material Girl ?", ["madonna"]),
        ("Quel instrument possède généralement 88 touches ?", ["piano", "le piano"]),
        ("Combien de cordes possède une guitare classique standard ?", ["6", "six"]),
        ("Quel instrument joue principalement un batteur ?", ["batterie", "la batterie"]),
        ("Comment appelle-t-on une chanson chantée par deux personnes ?", ["duo", "un duo"]),
        ("Quel groupe suédois a chanté Dancing Queen ?", ["abba"]),
        ("Quelle chanteuse interprète Bad Romance ?", ["lady gaga"]),
        ("Quel chanteur interprète Shape of You ?", ["ed sheeran"]),
        ("Quel groupe a chanté Smells Like Teen Spirit ?", ["nirvana"]),
        ("Quelle chanteuse interprète I Will Always Love You dans Bodyguard ?", ["whitney houston"]),
        ("Quel chanteur français interprète Je te donne avec Michael Jones ?", ["jean-jacques goldman", "jean jacques goldman", "goldman"]),
        ("Quel groupe britannique avait Freddie Mercury comme chanteur ?", ["queen"]),
        ("Quel artiste français interprète Papaoutai ?", ["stromae"]),
        ("Quelle chanteuse interprète Like a Prayer ?", ["madonna"]),
        ("Quel chanteur interprète Thriller ?", ["michael jackson"]),
        ("Quel groupe irlandais interprète With or Without You ?", ["u2"]),
        ("Quel chanteur français interprète Les Lacs du Connemara ?", ["michel sardou", "sardou"]),
        ("Quelle chanteuse interprète Someone Like You ?", ["adele", "adèle"]),
        ("Quel artiste interprète Perfect ?", ["ed sheeran"]),
        ("Quel groupe français interprète J'ai demandé à la lune ?", ["indochine"]),
        ("Quelle chanteuse interprète Poker Face ?", ["lady gaga"]),
    ],
    "🎬 Films & cinéma": [
        ("Dans quel film trouve-t-on le personnage de Jack Sparrow ?", ["pirates des caraïbes", "pirates des caraibes"]),
        ("Comment s'appelle le sorcier à lunettes créé par J. K. Rowling ?", ["harry potter"]),
        ("Quel film met en scène un paquebot nommé Titanic ?", ["titanic"]),
        ("Dans Retour vers le futur, comment s'appelle le jeune héros ?", ["marty mcfly", "marty"]),
        ("Quel acteur incarne principalement Indiana Jones au cinéma ?", ["harrison ford"]),
        ("Dans quel film d'animation trouve-t-on Simba ?", ["le roi lion", "roi lion"]),
        ("Comment s'appelle l'ogre vert de DreamWorks ?", ["shrek"]),
        ("Dans quel film trouve-t-on le personnage de Rocky Balboa ?", ["rocky"]),
        ("Quel film met en scène un parc rempli de dinosaures clonés ?", ["jurassic park"]),
        ("Comment s'appelle le robot compacteur de Pixar ?", ["wall-e", "wall e", "walle"]),
        ("Quel film Disney met en scène une reine aux pouvoirs de glace ?", ["la reine des neiges", "reine des neiges", "frozen"]),
        ("Dans quel film trouve-t-on le personnage d'E.T. ?", ["e.t.", "et", "e.t"]),
        ("Quel super-héros est Bruce Wayne ?", ["batman"]),
        ("Quel super-héros est Peter Parker ?", ["spider-man", "spiderman", "spider man"]),
        ("Dans quel film trouve-t-on Neo et Morpheus ?", ["matrix", "the matrix"]),
        ("Comment s'appelle le cowboy de Toy Story ?", ["woody"]),
        ("Comment s'appelle l'astronaute-jouet de Toy Story ?", ["buzz l'éclair", "buzz l eclair", "buzz lightyear", "buzz"]),
        ("Quel film met en scène le poisson-clown Nemo ?", ["le monde de nemo", "le monde de némo", "finding nemo"]),
        ("Dans quel film Disney trouve-t-on Aladdin et Jasmine ?", ["aladdin"]),
        ("Quel film raconte l'histoire d'un boxeur joué par Sylvester Stallone ?", ["rocky"]),
        ("Quel acteur joue le capitaine Jack Sparrow ?", ["johnny depp"]),
        ("Quel film de science-fiction met en scène les Na'vi ?", ["avatar"]),
        ("Dans quel film d'animation trouve-t-on les émotions Joie et Tristesse ?", ["vice-versa", "vice versa", "inside out"]),
        ("Quel film Disney met en scène Vaiana ?", ["vaiana", "moana"]),
        ("Quel personnage dit souvent Vers l'infini et au-delà dans Toy Story ?", ["buzz l'éclair", "buzz l eclair", "buzz"]),
        ("Dans quel film trouve-t-on le personnage de Forrest Gump ?", ["forrest gump"]),
        ("Quel film met en scène un requin terrorisant une station balnéaire ?", ["les dents de la mer", "jaws"]),
        ("Quel héros porte un bouclier aux couleurs américaines ?", ["captain america"]),
        ("Quel film d'animation met en scène Rémy, un rat qui cuisine ?", ["ratatouille"]),
        ("Quel film Disney met en scène Belle et une Bête ?", ["la belle et la bête", "la belle et la bete"]),
        ("Quel film met en scène un extraterrestre bleu nommé Stitch ?", ["lilo et stitch", "lilo & stitch"]),
        ("Dans quel film trouve-t-on le personnage de Rambo ?", ["rambo"]),
        ("Quel acteur joue Iron Man dans l'univers Marvel ?", ["robert downey jr", "robert downey junior"]),
        ("Dans quel film d'animation trouve-t-on Miguel et le monde des morts ?", ["coco"]),
        ("Quel film met en scène les sœurs Anna et Elsa ?", ["la reine des neiges", "reine des neiges", "frozen"]),
    ],
    "📺 Séries & télévision": [
        ("Dans Friends, comment s'appelle le frère de Monica ?", ["ross", "ross geller"]),
        ("Dans Stranger Things, quel est le prénom de la jeune fille aux pouvoirs ?", ["onze", "eleven", "11"]),
        ("Quelle série suit la famille Shelby ?", ["peaky blinders"]),
        ("Dans La Casa de Papel, quel surnom porte le cerveau du braquage ?", ["le professeur", "professeur", "el profesor"]),
        ("Quelle série met en scène Walter White ?", ["breaking bad"]),
        ("Dans Friends, quel métier exerce Ross ?", ["paléontologue", "paleontologue"]),
        ("Quelle série met en scène les personnages Meredith Grey et Derek Shepherd ?", ["grey's anatomy", "greys anatomy", "grey anatomy"]),
        ("Dans The Walking Dead, quel est le prénom du shérif au début de la série ?", ["rick", "rick grimes"]),
        ("Quelle série met en scène Wednesday Addams ?", ["wednesday", "mercredi"]),
        ("Dans Game of Thrones, quelle famille a pour devise L'hiver vient ?", ["stark", "les stark"]),
        ("Quelle série française suit une agence artistique appelée ASK ?", ["dix pour cent", "10 pour cent"]),
        ("Dans Stranger Things, dans quelle ville vivent les héros ?", ["hawkins"]),
        ("Quelle série met en scène Lucifer Morningstar à Los Angeles ?", ["lucifer"]),
        ("Dans Friends, comment s'appelle le café où le groupe se retrouve ?", ["central perk"]),
        ("Quelle série met en scène un médecin nommé Gregory House ?", ["dr house", "docteur house", "house"]),
        ("Dans Buffy contre les vampires, comment s'appelle l'héroïne ?", ["buffy", "buffy summers"]),
        ("Quelle série met en scène les frères Sam et Dean Winchester ?", ["supernatural"]),
        ("Dans Charmed, quel est le nom de famille des trois sœurs principales ?", ["halliwell"]),
        ("Quelle série met en scène Dexter Morgan ?", ["dexter"]),
        ("Dans Les Simpson, comment s'appelle le père de famille ?", ["homer", "homer simpson"]),
        ("Quelle série met en scène le personnage de Sheldon Cooper ?", ["the big bang theory", "big bang theory"]),
        ("Dans Plus belle la vie, dans quelle ville se situe principalement l'action ?", ["marseille"]),
        ("Quelle série met en scène le personnage de Thomas Shelby ?", ["peaky blinders"]),
        ("Dans Prison Break, comment s'appelle le frère de Lincoln ?", ["michael", "michael scofield"]),
        ("Quelle série suit un groupe de survivants après une apocalypse zombie ?", ["the walking dead", "walking dead"]),
        ("Dans Malcolm, comment s'appelle la mère de famille ?", ["lois"]),
        ("Quelle série met en scène Carrie Bradshaw ?", ["sex and the city"]),
        ("Dans Vikings, quel personnage est joué par Travis Fimmel ?", ["ragnar", "ragnar lothbrok"]),
        ("Quelle série met en scène un tueur en série surnommé Trinity dans une saison ?", ["dexter"]),
        ("Dans Un gars, une fille, quels sont les prénoms du couple ?", ["alex et jean", "jean et alex", "alexandra et jean"]),
        ("Quelle série met en scène les personnages Elena, Stefan et Damon ?", ["vampire diaries", "the vampire diaries"]),
        ("Dans Desperate Housewives, comment s'appelle la rue principale ?", ["wisteria lane"]),
        ("Quelle série met en scène un professeur de chimie qui devient fabricant de drogue ?", ["breaking bad"]),
        ("Dans Kaamelott, quel roi est au centre de l'histoire ?", ["arthur", "roi arthur"]),
        ("Quelle série met en scène le personnage de Joe Goldberg ?", ["you"]),
    ],
    "🧠 Culture générale": [
        ("Quelle est la capitale de la France ?", ["paris"]),
        ("Quelle planète est surnommée la planète rouge ?", ["mars"]),
        ("Quel est le plus grand océan du monde ?", ["pacifique", "océan pacifique", "ocean pacifique"]),
        ("Combien de mois compte une année ?", ["12", "douze"]),
        ("Combien de côtés a un triangle ?", ["3", "trois"]),
        ("Dans quel pays se trouve Rome ?", ["italie", "l'italie"]),
        ("Quel est le satellite naturel de la Terre ?", ["lune", "la lune"]),
        ("Combien de minutes y a-t-il dans une heure ?", ["60", "soixante"]),
        ("Quelle est la capitale de l'Espagne ?", ["madrid"]),
        ("Quel métal précieux est symbolisé par Au ?", ["or", "l'or"]),
        ("Quelle fête a lieu le 25 décembre ?", ["noël", "noel"]),
        ("Quel est le plus grand mammifère du monde ?", ["baleine bleue", "la baleine bleue"]),
        ("Quelle est la capitale du Royaume-Uni ?", ["londres"]),
        ("Quel organe pompe le sang dans le corps ?", ["coeur", "cœur", "le coeur", "le cœur"]),
        ("Quelle est la capitale de la Belgique ?", ["bruxelles"]),
        ("Combien de continents compte-t-on généralement ?", ["7", "sept"]),
        ("Quelle est la capitale du Portugal ?", ["lisbonne"]),
        ("Quel est le plus grand désert chaud du monde ?", ["sahara", "le sahara"]),
        ("Quel pays a la forme d'une botte sur une carte ?", ["italie", "l'italie"]),
        ("Combien font 8 x 8 ?", ["64", "soixante-quatre", "soixante quatre"]),
        ("Quelle langue parle-t-on principalement au Brésil ?", ["portugais", "le portugais"]),
        ("Quel est le premier mois de l'année ?", ["janvier"]),
        ("Quelle planète est la plus proche du Soleil ?", ["mercure"]),
        ("Quel animal est le plus grand animal terrestre actuel ?", ["éléphant", "elephant", "éléphant d'afrique", "elephant d'afrique"]),
        ("Quelle est la capitale de l'Allemagne ?", ["berlin"]),
        ("Combien de secondes y a-t-il dans une minute ?", ["60", "soixante"]),
        ("Quel élément chimique a pour symbole O ?", ["oxygène", "oxygene"]),
        ("Quelle est la capitale des Pays-Bas ?", ["amsterdam"]),
        ("Combien font 100 divisé par 4 ?", ["25", "vingt-cinq", "vingt cinq"]),
        ("Quelle saison vient après l'été ?", ["automne", "l'automne"]),
        ("Quel animal produit naturellement de la laine utilisée pour les vêtements ?", ["mouton", "le mouton"]),
        ("Combien de pattes a une araignée ?", ["8", "huit"]),
        ("Quelle couleur obtient-on en mélangeant bleu et jaune ?", ["vert", "verte"]),
        ("Quelle est la capitale de la Grèce ?", ["athènes", "athenes"]),
        ("Quel gaz est indispensable à notre respiration ?", ["oxygène", "oxygene"]),
    ],
    "🧸 Disney & dessins animés": [
        ("Comment s'appelle le père de Simba dans Le Roi Lion ?", ["mufasa"]),
        ("Quel personnage Disney perd une chaussure de verre ?", ["cendrillon"]),
        ("Comment s'appelle la sœur d'Elsa dans La Reine des neiges ?", ["anna"]),
        ("Quel poisson est le père de Nemo ?", ["marin", "marlin"]),
        ("Comment s'appelle le dragon de Mulan ?", ["mushu"]),
        ("Quel personnage Disney a un nez qui s'allonge lorsqu'il ment ?", ["pinocchio"]),
        ("Comment s'appelle le compagnon crabe d'Ariel ?", ["sébastien", "sebastien"]),
        ("Dans Aladdin, comment s'appelle la princesse ?", ["jasmine"]),
        ("Quel animal est Dumbo ?", ["éléphant", "elephant", "un éléphant", "un elephant"]),
        ("Comment s'appelle le méchant lion dans Le Roi Lion ?", ["scar"]),
        ("Dans La Belle et la Bête, comment s'appelle l'héroïne ?", ["belle"]),
        ("Quel personnage vit dans un ananas sous la mer ?", ["bob l'éponge", "bob l eponge", "spongebob"]),
        ("Comment s'appelle le meilleur ami de Mickey ?", ["dingo", "goofy"]),
        ("Quel canard Disney porte une marinière bleue ?", ["donald", "donald duck"]),
        ("Dans Ratatouille, comment s'appelle le rat cuisinier ?", ["rémy", "remy"]),
        ("Comment s'appelle la petite fille dans Monstres & Cie ?", ["bouh", "boo"]),
        ("Dans Toy Story, quel jouet est un cow-boy ?", ["woody"]),
        ("Quel personnage de Disney est une fée minuscule amie de Peter Pan ?", ["clochette", "fée clochette", "fee clochette"]),
        ("Comment s'appelle l'héroïne de La Petite Sirène ?", ["ariel"]),
        ("Dans Vaiana, comment s'appelle le demi-dieu ?", ["maui"]),
        ("Quel ours adore le miel ?", ["winnie", "winnie l'ourson", "winnie l ourson"]),
        ("Comment s'appelle le caméléon de Raiponce ?", ["pascal"]),
        ("Quel personnage est le fils de Mufasa ?", ["simba"]),
        ("Dans Les Aristochats, comment s'appelle la maman chatte ?", ["duchesse"]),
        ("Comment s'appelle le chien de Mickey ?", ["pluto"]),
        ("Dans 101 Dalmatiens, comment s'appelle la méchante ?", ["cruella", "cruella d'enfer", "cruella d enfer"]),
        ("Quel personnage Disney est élevé par des gorilles ?", ["tarzan"]),
        ("Comment s'appelle la princesse de La Princesse et la Grenouille ?", ["tiana"]),
        ("Dans Lilo & Stitch, quel numéro d'expérience est Stitch ?", ["626", "six cent vingt-six", "six cent vingt six"]),
        ("Quel personnage Disney possède une lampe magique ?", ["aladdin"]),
        ("Comment s'appelle le cheval de Raiponce ?", ["maximus"]),
        ("Dans Encanto, comment s'appelle l'héroïne sans pouvoir magique au début ?", ["mirabel"]),
        ("Quel personnage est l'ami tigre de Winnie ?", ["tigrou", "tigger"]),
        ("Comment s'appelle la princesse qui dort après s'être piquée à un fuseau ?", ["aurore", "la belle au bois dormant"]),
        ("Dans Cars, quel est le numéro de Flash McQueen ?", ["95", "quatre-vingt-quinze", "quatre vingt quinze"]),
    ],
}



QUIZ_A_CONTRE_SENS = [
("Comment s'appelle l'héroïne principale d'À contre-sens ?", ["noah"]),
("Comment s'appelle le personnage masculin principal ?", ["nick","nick leister","nicholas leister"]),
("Quel est le titre original espagnol d'À contre-sens ?", ["culpa mia","culpa mía"]),
("Quel acteur joue Nick ?", ["gabriel guevara"]),
("Quelle actrice joue Noah ?", ["nicole wallace"]),
("Dans quel pays se déroule principalement l'histoire ?", ["espagne","en espagne"]),
("Sur quelle plateforme est sorti À contre-sens ?", ["prime video","amazon prime video","amazon prime","prime"]),
("Comment s'appelle la mère de Noah ?", ["raffaella","rafaella"]),
("Comment s'appelle le père de Nick ?", ["william","william leister"]),
("Nick et Noah deviennent-ils demi-frère et demi-sœur par le mariage de leurs parents ?", ["oui"]),
("Nick participe à quel type de courses clandestines ?", ["courses de voitures","course de voitures","courses automobiles","course automobile"]),
("Quel personnage est passionné par les voitures et la vitesse ?", ["nick","nick leister"]),
("Noah sait-elle conduire de façon sportive ?", ["oui"]),
("Comment s'appelle l'ex-petit ami violent de Noah ?", ["ronnie"]),
("Qui met Noah en danger à la fin du premier film ?", ["ronnie"]),
("Nick appartient-il à une famille aisée ?", ["oui"]),
("Noah accepte-t-elle facilement sa nouvelle vie au début ?", ["non"]),
("Nick et Noah commencent-ils par bien s'entendre ?", ["non"]),
("Leur relation commence plutôt par des disputes ou une amitié immédiate ?", ["disputes","des disputes"]),
("Comment s'appelle la nouvelle amie proche de Noah ?", ["jenna"]),
("Comment s'appelle l'ami de Nick proche de Noah ?", ["lion","lionel"]),
("Quelle autrice a écrit les romans dont le film est adapté ?", ["mercedes ron","mercedes rón"]),
("Quel est le nom de famille de Nick ?", ["leister"]),
("Quel est le nom de famille de Noah ?", ["morgan"]),
("En quelle année le premier film est-il sorti ?", ["2023"]),
("Comment s'intitule la suite en français ?", ["à contre-sens 2","a contre-sens 2","à contre sens 2","a contre sens 2"]),
("Quel est le titre espagnol du deuxième volet ?", ["culpa tuya"]),
("Nick et Noah doivent-ils cacher leur relation à leurs parents ?", ["oui"]),
("Quel couple est au centre de la saga ?", ["nick et noah","noah et nick"]),
("À contre-sens est-il adapté d'une saga littéraire ?", ["oui"]),
]



def normalize_answer(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def quiz_overlaps_mega(start_dt):
    # 30 questions x 30 s = jusqu'à 15 minutes.
    end_dt = start_dt + timedelta(minutes=15)
    for name, start_t, end_t, kind in FIXED:
        if kind != "mega":
            continue
        mega_start = datetime.combine(start_dt.date(), start_t, tzinfo=TIMEZONE)
        mega_end = datetime.combine(start_dt.date(), end_t, tzinfo=TIMEZONE)
        if start_dt < mega_end and end_dt > mega_start:
            return True
    return False


async def run_quiz(start_dt):
    channel = bot.get_channel(SALON_JEU_ID)
    if not channel:
        return

    if quiz_overlaps_mega(start_dt):
        return

    async with quiz_lock:
        if start_dt.date().isoformat() == "2026-09-19" and start_dt.hour == 22 and start_dt.minute == 0:
            theme, bank = "🎬 À contre-sens ❤️", QUIZ_A_CONTRE_SENS
        else:
            theme, bank = random.choice(list(QUIZ_THEMES.items()))
        questions = random.sample(bank, min(30, len(bank)))
        scores = {}

        await channel.send(
            f"🎮 **Le jeu Lady commence !**\n"
            f"Thème : **{theme}**\n"
            "**30 questions** — 30 secondes par question. Première bonne réponse = 🎁"
        )

        for index, (question, answers) in enumerate(questions, start=1):
            await channel.send(f"❓ **Question {index}/30** — {theme}\n{question}")

            normalized = {normalize_answer(a) for a in answers}

            def check(message):
                return (
                    message.channel.id == SALON_JEU_ID
                    and not message.author.bot
                    and normalize_answer(message.content) in normalized
                )

            try:
                winner_msg = await bot.wait_for("message", timeout=30, check=check)
            except asyncio.TimeoutError:
                await channel.send("⏱️ Temps écoulé !")
                continue

            scores[winner_msg.author.id] = scores.get(winner_msg.author.id, 0) + 1

            async with data_lock:
                m = md(winner_msg.author.id)
                m["gifts"] += 1
                gifts = m["gifts"]
                save()

            await channel.send(
                f"🎁 Bravo {winner_msg.author.mention} ! **+1 cadeau** — "
                f"bonne réponse n°**{scores[winner_msg.author.id]}** dans cette partie — "
                f"tu as maintenant **{gifts} 🎁**."
            )

        if scores:
            ranking = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for place, (uid, points) in enumerate(ranking, start=1):
                prefix = medals[place - 1] if place <= 3 else f"**{place}.**"
                lines.append(f"{prefix} <@{uid}> — **{points} bonne(s) réponse(s)**")
            await channel.send(
                "🏁 **Le jeu Lady est terminé !**\n\n"
                f"🏆 **Classement — {theme}**\n" + "\n".join(lines)
            )
        else:
            await channel.send("🏁 **Le jeu Lady est terminé !** Aucune bonne réponse cette fois-ci.")


@tasks.loop(seconds=20)
async def quiz_scheduler():
    n = now()

    # Pas de quiz pendant la maintenance du dimanche.
    if sunday_maintenance(n):
        return

    for quiz_time in QUIZ_TIMES:
        start_dt = datetime.combine(n.date(), quiz_time, tzinfo=TIMEZONE)
        announce_dt = start_dt - timedelta(minutes=5)

        announce_key = f"announce|{start_dt.isoformat()}"
        start_key = f"start|{start_dt.isoformat()}"

        # Fenêtre de 40 secondes pour survivre aux petites dérives de la boucle.
        if 0 <= (n - announce_dt).total_seconds() < 40:
            async with data_lock:
                claims = DATA.setdefault("quiz_claims", [])
                if announce_key not in claims:
                    claims.append(announce_key)
                    DATA["quiz_claims"] = claims[-200:]
                    save()
                    channel = bot.get_channel(SALON_JEU_ID)
                    if channel:
                        try:
                            await channel.send("@everyone 🎮 **Jeu Lady dans 5 minutes !**")
                        except Exception:
                            pass

        if 0 <= (n - start_dt).total_seconds() < 40:
            should_start = False
            async with data_lock:
                claims = DATA.setdefault("quiz_claims", [])
                if start_key not in claims:
                    claims.append(start_key)
                    DATA["quiz_claims"] = claims[-200:]
                    save()
                    should_start = True
            if should_start:
                asyncio.create_task(run_quiz(start_dt))


@quiz_scheduler.before_loop
async def before_quiz_scheduler():
    await bot.wait_until_ready()

# ============================================================
# ANNIVERSAIRES
# ============================================================

@tasks.loop(minutes=1)
async def birthday_scheduler():
    n = now()
    # Une seule vérification utile au début de la journée ; la clé évite tout doublon.
    if n.hour != 0 or n.minute > 2:
        return

    channel = bot.get_channel(SALON_ANNIVERSAIRES_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SALON_ANNIVERSAIRES_ID)
        except Exception as exc:
            print("Salon anniversaires inaccessible :", exc)
            return

    birthdays = dict(DATA.setdefault("birthdays", {}))
    for uid, info in birthdays.items():
        try:
            day = int(info.get("day"))
            month = int(info.get("month"))
        except Exception:
            continue
        if day != n.day or month != n.month:
            continue

        key = f"{n.date().isoformat()}|{uid}"
        async with data_lock:
            claims = DATA.setdefault("birthday_claims", [])
            if key in claims:
                continue
            claims.append(key)
            DATA["birthday_claims"] = claims[-500:]
            m = md(uid)
            m["birthday_until"] = (n + timedelta(hours=24)).isoformat()
            save()

        try:
            await channel.send(
                f"@everyone 🎂 **Joyeux anniversaire <@{uid}> !** 🥳\\n"
                "Ton bonus 🎂 est actif pendant **24 heures** : "
                "**1 lien supplémentaire par session normale**.\\n"
                "🚀 Le bonus anniversaire n'est pas utilisable pendant les Méga Boost."
            )
        except Exception as exc:
            print("Message anniversaire impossible :", exc)


@birthday_scheduler.before_loop
async def before_birthday_scheduler():
    await bot.wait_until_ready()


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
    return bool(ctx.guild and is_lady_admin(ctx.author))


@bot.command(name="pp_ajouter")
async def pp_ajouter(ctx, member: discord.Member, nombre: int = 1):
    if not admin(ctx):
        return
    if nombre < 1:
        await ctx.send("❌ Le nombre de PP doit être au moins 1.")
        return

    current = None
    for _ in range(nombre):
        current = await award_pp(member)

    await ctx.send(
        f"💗 {member.mention} : **+{nombre} PP** ajouté{'s' if nombre > 1 else ''} manuellement. "
        f"Total cette semaine : **{current} PP**."
    )


@bot.command(name="lien_ajouter")
async def lien_ajouter(ctx, member: discord.Member, nombre: int = 1):
    if not admin(ctx):
        return
    if nombre < 1:
        await ctx.send("❌ Le nombre de liens sans rendre doit être au moins 1.")
        return

    async with data_lock:
        m = md(member.id)
        m["bows"] += nombre
        bows = m["bows"]
        save()

    await ctx.send(
        f"🎀 {member.mention} : **+{nombre} lien{'s' if nombre > 1 else ''} sans rendre**. "
        f"Total : **{bows} 🎀**."
    )


@bot.command()
async def troc(ctx, member: discord.Member, nombre: int):
    # Commande disponible uniquement dans le salon Troc.
    if not ctx.guild or ctx.channel.id != SALON_TROC_ID:
        return
    if not is_lady_admin(ctx.author):
        return

    if nombre <= 0 or nombre % 6 != 0:
        await ctx.send("❌ Le nombre de 🎁 à échanger doit être un multiple de **6** (6, 12, 18, 24...).")
        return

    bows_to_add = nombre // 6

    async with data_lock:
        m = md(member.id)
        if m["gifts"] < nombre:
            await ctx.send(
                f"🎁 {member.mention} n'a pas assez de cadeaux : "
                f"**{m['gifts']} 🎁 disponibles**, il en faut **{nombre}**."
            )
            return
        m["gifts"] -= nombre
        m["bows"] += bows_to_add
        gifts_left = m["gifts"]
        bows = m["bows"]
        save()

    await ctx.send(
        f"🎀 {member.mention} : **{nombre} 🎁 → {bows_to_add} 🎀**. "
        f"Il reste **{gifts_left} 🎁** et **{bows} 🎀**."
    )


@bot.command(name="avertissement_enlever")
async def avertissement_enlever(ctx, member: discord.Member):
    if not admin(ctx):
        return

    async with data_lock:
        m = md(member.id)
        before = int(m.get("warnings", 0))
        m["warnings"] = max(0, before - 1)
        warnings = m["warnings"]
        save()

    await ctx.send(
        f"⚠️ {member.mention} : **-1 avertissement**. "
        f"Il/elle est maintenant à **{warnings}/3**."
    )


def parse_birthday_date(value):
    value = (value or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m", "%d-%m"):
        try:
            d = datetime.strptime(value, fmt)
            return d.day, d.month
        except ValueError:
            pass
    return None


@bot.command(name="anniversaire")
async def anniversaire(ctx, member: discord.Member, date_anniversaire: str):
    if not admin(ctx):
        return
    parsed = parse_birthday_date(date_anniversaire)
    if not parsed:
        await ctx.send("🎂 Format attendu : `lady_anniversaire @membre JJ/MM`.")
        return
    day, month = parsed
    async with data_lock:
        DATA.setdefault("birthdays", {})[str(member.id)] = {"day": day, "month": month}
        save()
    await ctx.send(f"🎂 Anniversaire de {member.mention} enregistré au **{day:02d}/{month:02d}**.")


@bot.command(name="anniversaire_enlever")
async def anniversaire_enlever(ctx, member: discord.Member):
    if not admin(ctx):
        return
    async with data_lock:
        existed = DATA.setdefault("birthdays", {}).pop(str(member.id), None)
        md(member.id)["birthday_until"] = None
        save()
    if existed:
        await ctx.send(f"🎂 Anniversaire de {member.mention} supprimé.")
    else:
        await ctx.send(f"🎂 Aucun anniversaire enregistré pour {member.mention}.")


@bot.command(name="anniversaires")
async def anniversaires(ctx):
    if not admin(ctx):
        return
    birthdays = DATA.setdefault("birthdays", {})
    if not birthdays:
        await ctx.send("🎂 Aucun anniversaire enregistré.")
        return
    rows = []
    for uid, info in birthdays.items():
        rows.append((int(info.get("month", 0)), int(info.get("day", 0)), uid))
    rows.sort()
    text = ["🎂 **Anniversaires enregistrés**"]
    for month, day, uid in rows:
        text.append(f"<@{uid}> — **{day:02d}/{month:02d}**")
    await ctx.send("\\n".join(text))


@bot.command()
async def absence(ctx, member: discord.Member, debut: str, fin: str):
    if not admin(ctx):
        return

    async with data_lock:
        DATA.setdefault("absences", {})[str(member.id)] = {"debut": debut, "fin": fin}
        save()

    await ctx.send(f"💌 Absence enregistrée pour {member.mention}.")


@bot.command()
async def absences(ctx):
    if not admin(ctx):
        return

    abs_data = DATA.setdefault("absences", {})
    if not abs_data:
        await ctx.send("💌 Aucune absence enregistrée.")
        return

    lines = []
    for uid, info in abs_data.items():
        lines.append(f"<@{uid}> : **{info.get('debut', '?')} → {info.get('fin', '?')}**")

    await ctx.send("💌 **Absences enregistrées**\n" + "\n".join(lines))

# ============================================================
# EVENEMENTS
# ============================================================

@bot.event
async def on_message(msg):
    if msg.author.bot:
        return

    if (msg.content or "").strip().lower() == "lady_stat":
        await send_stats(msg.author)
        return

    # Dans le salon Anniversaires, chaque membre peut simplement écrire sa date
    # (ex. 25/08 ou "25/08/1992"). Lady l'enregistre automatiquement.
    if msg.channel.id == SALON_ANNIVERSAIRES_ID:
        content = (msg.content or "").strip()
        match = re.search(r"(?<!\d)([0-3]?\d)[/-]([01]?\d)(?:[/-]\d{2,4})?(?!\d)", content)
        if match:
            try:
                day = int(match.group(1))
                month = int(match.group(2))
                # Validation réelle de la date (année bissextile pour accepter 29/02).
                datetime(2024, month, day)
            except ValueError:
                await temp_message(msg.channel, f"🎂 {msg.author.mention} cette date n'est pas valide.")
                return

            async with data_lock:
                DATA.setdefault("birthdays", {})[str(msg.author.id)] = {
                    "day": day,
                    "month": month,
                }
                save()

            await msg.channel.send(
                f"🎂 {msg.author.mention}, ton anniversaire du **{day:02d}/{month:02d}** "
                "est bien enregistré !"
            )
            return

    if msg.channel.id == SALON_VENTES_ID and msg.attachments:
        await sale(msg)

    if (
        msg.channel.id == SALON_GROUPE_SESSION_ID
        and VINTED.search(msg.content or "")
    ):
        await group_session_post(msg)
        try:
            await msg.edit(suppress=True)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return

    if (
        msg.channel.id == SALON_SESSIONS_ID
        and session
        and VINTED.search(msg.content or "")
    ):
        await participation(msg)

        try:
            await msg.edit(suppress=True)
        except (discord.Forbidden, discord.HTTPException):
            pass

    await bot.process_commands(msg)



async def recover_stop_after_restart():
    info = DATA.get("active_session")
    if not isinstance(info, dict):
        return
    name = info.get("name")
    end = parse_dt(info.get("end"))
    if not name or not end or end > now():
        return
    channel = bot.get_channel(SALON_SESSIONS_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SALON_SESSIONS_ID)
        except Exception:
            return
    stop_img = SESSION_IMAGES.get(name, (None, None))[1]
    if stop_img:
        await send_image(channel, stop_img)
    async with data_lock:
        DATA.pop("active_session", None)
        save()


# ============================================================
# CORRECTIF UNIQUE DU 13/09/2026 — REMISE À ZÉRO DES BONUS
# Conserve uniquement les 🎀 gagnés lors de la clôture qui vient d'avoir lieu.
# Le message de clôture dans le salon discussion permet de retrouver les gagnantes :
# 1 🎀 pour meilleure participation + 1 🎀 pour meilleure vente (cumul possible).
# ============================================================

async def cleanup_bonus_once_2026_09_13():
    cleanup_key = "bonus_cleanup_2026-09-13_done"

    async with data_lock:
        if DATA.get(cleanup_key):
            return

    discussion = bot.get_channel(SALON_DISCUSSION_ID)
    if discussion is None:
        try:
            discussion = await bot.fetch_channel(SALON_DISCUSSION_ID)
        except Exception as exc:
            print("Correctif bonus : salon discussion inaccessible :", exc)
            return

    fresh_bows = {}
    closure_message_found = False

    try:
        async for message in discussion.history(limit=100):
            if message.author.id != bot.user.id:
                continue
            content = message.content or ""
            if "Mise à jour hebdomadaire Lady terminée" not in content:
                continue

            closure_message_found = True

            for line in content.splitlines():
                if "Meilleure(s) participation(s)" in line or "Meilleure(s) vendeuse(s)" in line:
                    for member in message.mentions:
                        if member.mention in line:
                            fresh_bows[member.id] = fresh_bows.get(member.id, 0) + 1
            break
    except Exception as exc:
        print("Correctif bonus : lecture du message de clôture impossible :", exc)
        return

    # Sécurité : on ne touche pas aux nœuds si le message de clôture n'a pas été retrouvé.
    if not closure_message_found:
        print("Correctif bonus : message de clôture introuvable, aucun reset effectué.")
        return

    async with data_lock:
        for member in bot.get_all_members():
            if member.bot:
                continue
            m = md(member.id)

            # Tous les anciens bonus repartent à zéro.
            m["gifts"] = 0
            m["crown_until"] = None
            m["diamond_until"] = None

            # Les seuls nœuds conservés sont ceux gagnés à la clôture de ce soir.
            # Une même personne peut en conserver 2 si elle a gagné les deux classements.
            m["bows"] = fresh_bows.get(member.id, 0)

        DATA[cleanup_key] = True
        save()

    print("Correctif bonus du 13/09/2026 appliqué une seule fois.")



async def fix_new_week_green_roles_once():
    key = "fix_green_roles_2026-09-14_done"
    async with data_lock:
        if DATA.get(key):
            return

    changed = 0
    for guild in bot.guilds:
        green_role = discord.utils.get(guild.roles, name="✅ À JOUR")
        pink_role = discord.utils.get(guild.roles, name="🌸 SEMAINE À FAIRE")
        if green_role is None or pink_role is None:
            continue

        for member in guild.members:
            if member.bot:
                continue
            m = md(member.id)
            if int(m.get("pp_week", 0)) == 0 and green_role in member.roles:
                try:
                    await member.remove_roles(green_role, reason="Lady : nouvelle semaine")
                    await member.add_roles(pink_role, reason="Lady : nouvelle semaine")
                    changed += 1
                except Exception as exc:
                    print(f"Rôle nouvelle semaine impossible pour {member}: {exc}")

    async with data_lock:
        DATA[key] = True
        save()
    print(f"Correction nouvelle semaine : {changed} membre(s) vert -> rose.")


async def catch_up_missed_weekly_close():
    n = now()
    if n.weekday() != 0:
        return

    missed_sunday = n.date() - timedelta(days=1)
    key = missed_sunday.isoformat()

    async with data_lock:
        already_done = key in DATA.setdefault("weekly_closures", [])

    if already_done:
        print(f"Rattrapage inutile : clôture {key} déjà effectuée.", flush=True)
        return

    print(f"Rattrapage automatique de la clôture du {key}...", flush=True)
    for guild in bot.guilds:
        await weekly_close(guild, missed_sunday)


@bot.event
async def on_ready():
    print(f"Lady connectée : {bot.user} ({bot.user.id})", flush=True)
    print(f"Fichier de données : {DATA_FILE}", flush=True)

    await recover_stop_after_restart()
    await cleanup_bonus_once_2026_09_13()
    await fix_new_week_green_roles_once()
    await catch_up_missed_weekly_close()

    # Démarrage immédiat des fonctions principales.
    if not scheduler.is_running():
        scheduler.start()
    if not weekly_scheduler.is_running():
        weekly_scheduler.start()
    if not sunday_reminder_scheduler.is_running():
        sunday_reminder_scheduler.start()
    if not monthly_sales_scheduler.is_running():
        monthly_sales_scheduler.start()
    if not quiz_scheduler.is_running():
        quiz_scheduler.start()
    if not birthday_scheduler.is_running():
        birthday_scheduler.start()

    # Parrainage initialisé ensuite pour ne jamais bloquer les sessions/PP.
    for guild in bot.guilds:
        try:
            await refresh_invites(guild)
        except Exception as exc:
            print("Initialisation invitations impossible:", exc)

# ============================================================
# LANCEMENT
# ============================================================

bot.run(TOKEN)
