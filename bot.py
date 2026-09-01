import os, json, random, asyncio, re
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo
import discord
from discord.ext import commands, tasks

TIMEZONE=ZoneInfo("Europe/Paris")
SALON_SESSIONS_ID=1521853338918977558
SALON_VENTES_ID=1528658847806390382
SALON_JEU_ID=1541713193762820106
SALON_DISCUSSION_ID=1528130797029167134
SALON_ANNIVERSAIRES_ID=1541680943583068200
ROLE_ROSE="🌸 SEMAINE À FAIRE"
ROLE_VERT="✅ À JOUR"
ROLE_ORANGE="Semaine non validée"
ROLE_ROUGE="Supprimer"
SEUIL_SEMAINE=6
DATA_DIR=Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH","/app/data"))
DATA_DIR.mkdir(parents=True,exist_ok=True)
DATA_FILE=DATA_DIR/"lady_data.json"
TOKEN=os.environ.get("DISCORD_TOKEN")
if not TOKEN: raise RuntimeError("DISCORD_TOKEN absent")

intents=discord.Intents.default()
intents.message_content=True
intents.members=True
intents.reactions=True
bot=commands.Bot(command_prefix="!",intents=intents,help_command=None)
lock=asyncio.Lock()
session=None
started=set()

def fresh():
    return {"members":{},"sales_messages":[],"absences":{}}

def load():
    try:
        with DATA_FILE.open(encoding="utf-8") as f: d=json.load(f)
        x=fresh(); x.update(d); return x
    except Exception: return fresh()

DATA=load()

def save():
    p=DATA_FILE.with_suffix(".tmp")
    p.write_text(json.dumps(DATA,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(p,DATA_FILE)

def md(uid):
    m=DATA["members"].setdefault(str(uid),{})
    defaults={"pp_week":0,"pp_total":0,"record":0,"warnings":0,"gifts":0,"bows":0,
              "sales_week":0,"crown_until":None,"diamond_until":None,"birthday_until":None,
              "gift_step":0,"diamond_step":0}
    for k,v in defaults.items(): m.setdefault(k,v)
    return m

def now(): return datetime.now(TIMEZONE)
def rem(v):
    if not v:return None
    try:d=datetime.fromisoformat(v)
    except:return None
    if d<=now():return None
    s=int((d-now()).total_seconds()); return f"{s//3600}h {(s%3600)//60:02d}min"

async def role_update(member):
    m=md(member.id)
    rose=discord.utils.get(member.guild.roles,name=ROLE_ROSE)
    green=discord.utils.get(member.guild.roles,name=ROLE_VERT)
    try:
        if m["pp_week"]>=SEUIL_SEMAINE:
            if green and green not in member.roles: await member.add_roles(green)
            if rose and rose in member.roles: await member.remove_roles(rose)
        elif rose and rose not in member.roles: await member.add_roles(rose)
    except discord.Forbidden: pass

async def pp(member):
    async with lock:
        m=md(member.id); m["pp_week"]+=1; m["pp_total"]+=1
        m["record"]=max(m["record"],m["pp_week"])
        gs=m["pp_total"]//6
        if gs>m["gift_step"]: m["gifts"]+=gs-m["gift_step"]; m["gift_step"]=gs
        ds=m["pp_total"]//20
        if ds>m["diamond_step"]:
            m["diamond_step"]=ds
            m["diamond_until"]=(now()+timedelta(hours=24)).isoformat()
        save()
    await role_update(member)

FIXED=[
("🌙 Nocturne",time(0,30),time(8,0),"normal"),
("☕ Petit déjeuner",time(9),time(9,30),"normal"),
("🍹 Apéro",time(11,30),time(12),"normal"),
("🍽️ Repas",time(12),time(12,30),"normal"),
("🚀 Méga Boost",time(14),time(14,30),"mega"),
("🍹 Apéro",time(18,30),time(19),"normal"),
("🍽️ Repas",time(19),time(19,30),"normal"),
("🚀 Méga Boost",time(21),time(21,30),"mega")]

async def begin(name,a,b,kind):
    global session
    if session:return False
    ch=bot.get_channel(SALON_SESSIONS_ID)
    if not ch:return False
    session={"name":name,"end":b,"kind":kind,"participants":set(),"normal":set()}
    if kind=="mega":
        d=bot.get_channel(SALON_DISCUSSION_ID)
        if d: await d.send(f"@everyone 🚀 *{name} commence !*")
    txt=f"✨ **{name}**\n⏰ Fin : **{b:%H:%M}**\n"
    txt+=("🚫 Aucun bonus 🎁 🎀 👑 💎 pendant le Méga Boost." if kind=="mega" else "🔗 1 lien normal par membre.")
    await ch.send(txt); return True

async def finish():
    global session
    s=session
    if not s:return
    ch=bot.get_channel(SALON_SESSIONS_ID)
    if ch: await ch.send(f"🛑 **{s['name']} terminée**\n👥 {len(s['participants'])} participante(s).")
    if s["kind"]=="mega" and s["participants"]:
        ids=list(s["participants"]); random.shuffle(ids)
        rewards=["🎁","🎀","👑"]; lines=[]
        async with lock:
            for uid,r in zip(ids[:3],rewards):
                m=md(uid)
                if r=="🎁":m["gifts"]+=1
                elif r=="🎀":m["bows"]+=1
                else:m["crown_until"]=(now()+timedelta(hours=24)).isoformat()
                lines.append(f"<@{uid}> gagne {r}")
            save()
        if ch:await ch.send("🎉 **Résultats Méga Boost**\n"+"\n".join(lines))
    session=None

@tasks.loop(seconds=20)
async def scheduler():
    global session
    t=now()
    if session and t>=session["end"]:await finish()
    if session:return
    for name,st,et,kind in FIXED:
        a=datetime.combine(t.date(),st,tzinfo=TIMEZONE); b=datetime.combine(t.date(),et,tzinfo=TIMEZONE)
        if a<=t<b:
            k=f"{t.date()}-{st}-{name}"
            if k not in started and await begin(name,a,b,kind):started.add(k)
            return

@scheduler.before_loop
async def wait():await bot.wait_until_ready()

VINTED=re.compile(r"https?://\S*vinted\.\S+",re.I)

async def stats(user):
    m=md(user.id); c=rem(m["crown_until"]); d=rem(m["diamond_until"]); a=rem(m["birthday_until"])
    status="✅ Validée" if m["pp_week"]>=6 else f"🌸 En cours ({m['pp_week']}/6)"
    text=(f"📊 **Tes statistiques Lady**\n\n📅 Semaine : **{status}**\n💗 Participations : **{m['pp_week']}**\n"
          f"🏆 Record : **{m['record']}**\n⚠️ Avertissements : **{m['warnings']}/3**\n🎁 Cadeaux : **{m['gifts']}**\n"
          f"🎀 Nœuds : **{m['bows']}**\n👑 Couronne : **{'active — '+c if c else 'inactive'}**\n"
          f"💎 Diamant : **{'actif — '+d if d else 'inactif'}**\n🎂 Anniversaire : **{'actif — '+a if a else 'inactif'}**\n"
          f"🛍️ Ventes semaine : *{m['sales_week']}*")
    try:await user.send(text)
    except discord.Forbidden:pass

async def sale(msg):
    mid=str(msg.id)
    async with lock:
        if mid in DATA["sales_messages"]:return
        DATA["sales_messages"].append(mid); m=md(msg.author.id);m["sales_week"]+=1;m["bows"]+=1;save()

async def participation(msg):
    global session
    s=session
    if not s:return
    uid=msg.author.id;text=msg.content or "";m=md(uid)
    if s["kind"]=="mega":
        if any(x in text for x in "🎁🎀👑💎"):return
        if uid in s["normal"]:return
        s["normal"].add(uid);s["participants"].add(uid);await pp(msg.author);return
    if "🎁" in text:
        async with lock:
            m=md(uid)
            if m["gifts"]>0:m["gifts"]-=1;save()
        return
    if "🎀" in text:
        async with lock:
            m=md(uid)
            if m["bows"]>0:m["bows"]-=1;save()
        return
    if "👑" in text or "💎" in text:return
    if uid in s["normal"]:
        if m["gifts"]>0:
            try:await msg.author.send("🎁 Ton lien supplémentaire n'a pas été validé : tu as oublié 🎁. Ton cadeau reste disponible.")
            except:pass
        return
    s["normal"].add(uid);s["participants"].add(uid);await pp(msg.author)

@bot.event
async def on_message(msg):
    if msg.author.bot:return
    if (msg.content or "").strip().lower()=="lady_stat":await stats(msg.author);return
    if msg.channel.id==SALON_VENTES_ID:
        image=any((x.content_type or "").startswith("image/") for x in msg.attachments)
        if "vendu" in (msg.content or "").lower() and image:await sale(msg)
    if msg.channel.id==SALON_SESSIONS_ID and session and VINTED.search(msg.content or ""):await participation(msg)
    await bot.process_commands(msg)

def admin(ctx):return ctx.author.guild_permissions.administrator

@bot.command()
async def lady_troc(ctx,member:discord.Member,gifts:int):
    if not admin(ctx):return
    if gifts!=6:return await ctx.send("Troc prévu *6 🎁 = 1 🎀***.")
    async with lock:
        m=md(member.id)
        if m["gifts"]<6:return await ctx.send("Pas assez de 🎁.")
        m["gifts"]-=6;m["bows"]+=1;save()
    await ctx.send(f"🎀 {member.mention} : 6 🎁 → 1 🎀.")

@bot.command()
async def lady_absence(ctx,member:discord.Member,debut:str,fin:str):
    if not admin(ctx):return
    DATA["absences"][str(member.id)]={"debut":debut,"fin":fin};save()
    await ctx.send(f"🏖️ Absence enregistrée pour {member.mention}.")

@bot.event
async def on_ready():
    print(f"Lady connectée : {bot.user} ({bot.user.id})")
    if not scheduler.is_running():scheduler.start()

bot.run(TOKEN)
