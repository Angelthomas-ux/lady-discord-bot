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
DATA.setdefault("birthdays", {})
DATA.setdefault("group_session_chain", [])


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

    session = {
        "name": name,
        "start": start,
        "end": end,
        "kind": kind,
        "free": is_free,
        "normal": set(),
        "participants": set(),
        "dressing_count": dressing_count,
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

            if member.guild_permissions.administrator:
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

    # Contrôle des rendus uniquement sur les sessions normales.
    if old["kind"] != "mega":
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
    if not choices:
        choices = FREE_SESSIONS[:]

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
# GROUPE SESSION — 3 LIENS AU-DESSUS
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
            None
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

def is_absent_this_week(uid):
    info = DATA.setdefault("absences", {}).get(str(uid))
    if not info:
        return False

    # Les anciennes commandes acceptaient des dates libres : si on ne peut pas
    # les parser proprement, on considère l'absence enregistrée comme valable.
    def parse_day(value):
        if not value:
            return None
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m"):
            try:
                d = datetime.strptime(value, fmt)
                if fmt == "%d/%m":
                    d = d.replace(year=now().year)
                return d.date()
            except Exception:
                pass
        return None

    start = parse_day(info.get("debut"))
    end = parse_day(info.get("fin"))
    if not start or not end:
        return True

    today = now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return start <= sunday and end >= monday


async def weekly_close(guild, closure_date):
    key = closure_date.isoformat()

    async with weekly_lock:
        async with data_lock:
            closures = DATA.setdefault("weekly_closures", [])
            if key in closures:
                return

            # On marque avant les actions Discord pour empêcher un double passage
            # après un redémarrage Railway.
            closures.append(key)
            DATA["weekly_closures"] = closures[-60:]
            save()

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

        async with data_lock:
            for member in pp_winners:
                md(member.id)["bows"] += 1
            for member in sales_winners:
                md(member.id)["bows"] += 1
            save()

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

            if is_absent_this_week(member.id) or new_this_week:
                await set_week_roles(member, rose=True)
                continue

            orange_role = discord.utils.get(guild.roles, name=ROLE_ORANGE)
            already_orange = bool(orange_role and orange_role in member.roles)

            if already_orange:
                await set_week_roles(member, red=True)
            else:
                await set_week_roles(member, orange=True)

        # Reset hebdomadaire seulement après calcul des gagnantes et des rôles.
        async with data_lock:
            for member, _, _ in members_with_data:
                m = md(member.id)
                m["pp_week"] = 0
                m["sales_week"] = 0
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

        print(f"Clôture hebdomadaire terminée pour {key}")


@tasks.loop(seconds=20)
async def weekly_scheduler():
    n = now()
    if n.weekday() != 6 or not (time(23, 50) <= n.time() < time(23, 59, 59)):
        return

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
            f"Tu gagnes **+1 nœud** — tu en as maintenant **{bows}**."
        )
    except Exception:
        pass

# ============================================================
# JEUX / QUIZ — 6 PAR JOUR
# ============================================================

QUIZ_TIMES = [time(9, 45), time(11, 0), time(15, 0), time(18, 0), time(20, 0), time(22, 0)]

QUIZ_QUESTIONS = [
    ("Quelle est la capitale de la France ?", ["paris"]),
    ("Combien y a-t-il de jours dans une semaine ?", ["7", "sept"]),
    ("Quelle planète est surnommée la planète rouge ?", ["mars"]),
    ("Quel animal miaule ?", ["chat", "le chat"]),
    ("Combien font 5 + 7 ?", ["12", "douze"]),
    ("Quelle couleur obtient-on en mélangeant bleu et jaune ?", ["vert", "verte"]),
    ("Quel est le plus grand océan du monde ?", ["pacifique", "océan pacifique", "ocean pacifique"]),
    ("Combien de mois compte une année ?", ["12", "douze"]),
    ("Quel fruit jaune est souvent associé aux singes ?", ["banane", "la banane"]),
    ("Quel est le contraire de chaud ?", ["froid"]),
    ("Combien de côtés a un triangle ?", ["3", "trois"]),
    ("Quel gaz respirons-nous principalement pour vivre ?", ["oxygène", "oxygene"]),
    ("Quelle saison vient après l'été ?", ["automne", "l'automne"]),
    ("Quel animal est surnommé le roi de la jungle ?", ["lion", "le lion"]),
    ("Combien font 9 x 3 ?", ["27", "vingt-sept", "vingt sept"]),
    ("Dans quel pays se trouve Rome ?", ["italie", "l'italie"]),
    ("Quel est le satellite naturel de la Terre ?", ["lune", "la lune"]),
    ("Combien de minutes y a-t-il dans une heure ?", ["60", "soixante"]),
    ("Quelle couleur a une émeraude ?", ["vert", "verte"]),
    ("Quel animal produit de la laine ?", ["mouton", "le mouton"]),
    ("Quelle est la capitale de l'Espagne ?", ["madrid"]),
    ("Combien font 100 divisé par 4 ?", ["25", "vingt-cinq", "vingt cinq"]),
    ("Quel est le premier mois de l'année ?", ["janvier"]),
    ("Quel instrument possède généralement 88 touches ?", ["piano", "le piano"]),
    ("Quelle est la capitale de l'Italie ?", ["rome"]),
    ("Quel animal aboie ?", ["chien", "le chien"]),
    ("Combien de pattes a une araignée ?", ["8", "huit"]),
    ("Quel métal précieux est symbolisé par Au ?", ["or", "l'or"]),
    ("Quelle fête a lieu le 25 décembre ?", ["noël", "noel"]),
    ("Combien font 15 - 6 ?", ["9", "neuf"]),
    ("Quel est le plus grand mammifère du monde ?", ["baleine bleue", "la baleine bleue"]),
    ("Quelle est la capitale du Royaume-Uni ?", ["londres"]),
    ("Combien de couleurs compte traditionnellement un arc-en-ciel ?", ["7", "sept"]),
    ("Quel organe pompe le sang dans le corps ?", ["coeur", "cœur", "le coeur", "le cœur"]),
    ("Quel jour vient après vendredi ?", ["samedi"]),
    ("Quel est le contraire de rapide ?", ["lent", "lente"]),
    ("Combien font 8 x 8 ?", ["64", "soixante-quatre", "soixante quatre"]),
    ("Dans quel pays se trouve la tour de Pise ?", ["italie", "l'italie"]),
    ("Quel animal pond des œufs et donne de la laine ?", ["aucun", "aucun animal"]),
    ("Quelle est la capitale de la Belgique ?", ["bruxelles"]),
]


def normalize_answer(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def quiz_overlaps_mega(start_dt):
    # 20 questions x 30 s = jusqu'à 10 minutes.
    end_dt = start_dt + timedelta(minutes=10)
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
        questions = random.sample(QUIZ_QUESTIONS, min(20, len(QUIZ_QUESTIONS)))
        await channel.send("🎮 **Le jeu Lady commence !** 20 questions — 30 secondes par question. Première bonne réponse = 🎁")

        for index, (question, answers) in enumerate(questions, start=1):
            await channel.send(f"❓ **Question {index}/20**\n{question}")

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

            async with data_lock:
                m = md(winner_msg.author.id)
                m["gifts"] += 1
                gifts = m["gifts"]
                save()

            await channel.send(
                f"🎁 Bravo {winner_msg.author.mention} ! **+1 cadeau** — tu en as maintenant **{gifts}**."
            )

        await channel.send("🏁 **Le jeu Lady est terminé !**")


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
    return bool(ctx.guild and ctx.author.guild_permissions.administrator)


@bot.command()
async def troc(ctx, member: discord.Member):
    if not admin(ctx):
        return

    async with data_lock:
        m = md(member.id)
        if m["gifts"] < 5:
            await ctx.send(f"🎁 {member.mention} n'a pas assez de cadeaux : **{m['gifts']}/5**.")
            return
        m["gifts"] -= 5
        m["bows"] += 1
        gifts_left = m["gifts"]
        bows = m["bows"]
        save()

    await ctx.send(
        f"🎀 {member.mention} : **5 🎁 → 1 🎀**. "
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


@bot.event
async def on_ready():
    print(f"Lady connectée : {bot.user} ({bot.user.id})")
    print(f"Fichier de données : {DATA_FILE}")

    await recover_stop_after_restart()
    await cleanup_bonus_once_2026_09_13()
    await fix_new_week_green_roles_once()

    # Démarrage immédiat des fonctions principales.
    if not scheduler.is_running():
        scheduler.start()
    if not weekly_scheduler.is_running():
        weekly_scheduler.start()
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
