import asyncio
import os
from threading import Thread
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, request
from waitress import serve
import motor.motor_asyncio
from dotenv import load_dotenv
import time
import datetime

# Load environment variables from the .env file
load_dotenv()

# Grab secrets securely from the environment
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

# === MONGODB DATABASE SETUP ===
cluster = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = cluster["roblox_tracker"]
collection = db["users"]

# === FLASK SERVER ===
app = Flask("")
SERVER_DOMAIN = "https://roblox-tracker-discord-bot.onrender.com"

@app.route("/")
def home():
    return "Public Profile Tracker is running 24/7!"

@app.route("/join")
def join():
    place_id = request.args.get("placeId")
    game_id = request.args.get("gameInstanceId")
    if not place_id or not game_id:
        return "Invalid parameters.", 400
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta http-equiv="refresh" content="0;url=roblox://experiences/start?placeId={place_id}&gameInstanceId={game_id}">
        <title>Joining Roblox Server...</title>
    </head>
    <body>
        <p>Launching Roblox... If nothing happens, <a href="roblox://experiences/start?placeId={place_id}&gameInstanceId={game_id}">click here</a>.</p>
    </body>
    </html>
    """

def run_web_server():
    serve(app, host="0.0.0.0", port=10000)

# === INITIALIZE DATA ===
TRACKED_USERS = {} 
last_played_place = {}
session_start_times = {}
active_alert_messages = {} # {user_id: {guild_id: message_object}}
active_session_data = {}   # {user_id: {"username": ..., "game_name": ...}}
username_cache = {}
game_name_cache = {}

# === DISCORD BOT SETUP ===
class RobloxTrackerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        global TRACKED_USERS
        
        # Load all users and their server lists from MongoDB
        cursor = collection.find({})
        async for document in cursor:
            user_id = int(document["_id"])
            TRACKED_USERS[user_id] = document.get("servers", [])
            last_played_place[user_id] = None
            
        await self.tree.sync()
        print(f"[INFO] Slash commands synced. Loaded {len(TRACKED_USERS)} users from database.")
        self.monitor_loop.start()

    async def get_username(self, session, user_id):
        if user_id in username_cache:
            return username_cache[user_id]
        try:
            url = f"https://users.roblox.com/v1/users/{user_id}"
            async with session.get(url, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    name = data.get("name", str(user_id))
                    username_cache[user_id] = name
                    return name
        except Exception as e:
            print(f"Error fetching username for {user_id}: {e}")
        return str(user_id)

    async def get_game_name(self, session, place_id):
        if not place_id:
            return "Unknown Game"
        if place_id in game_name_cache:
            return game_name_cache[place_id]
        try:
            url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
            async with session.get(url, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    if data and len(data) > 0:
                        name = data[0].get("name", f"Place {place_id}")
                        game_name_cache[place_id] = name
                        return name
        except Exception as e:
            print(f"Error fetching game name for Place ID {place_id}: {e}")
        return f"Place {place_id}"
        
    async def get_avatar_thumbnail(self, session, user_id):
        try:
            url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png&isCircular=false"
            async with session.get(url, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    thumbnails = data.get("data", [])
                    if thumbnails:
                        return thumbnails[0].get("imageUrl", None)
        except Exception as e:
            print(f"Error fetching avatar thumbnail for {user_id}: {e}")
        return None   

    @tasks.loop(seconds=60)
    async def monitor_loop(self):
        async with aiohttp.ClientSession() as session:
            try:
                for user_id, servers_list in list(TRACKED_USERS.items()):
                    if not servers_list:
                        continue
                        
                    username = await self.get_username(session, user_id)
                    avatar_url = await self.get_avatar_thumbnail(session, user_id)
                    
                    presence_url = "https://presence.roblox.com/v1/presence/users"
                    async with session.post(presence_url, json={"userIds": [user_id]}, timeout=10) as response:
                        if response.status == 200:
                            data = await response.json()
                            presences = data.get("userPresences", [])
                            
                            if presences:
                                user_status = presences[0]
                                presence_type = user_status.get("userPresenceType")
                                current_place_id = user_status.get("placeId")
                                root_place_id = user_status.get("rootPlaceId")
                                game_instance_id = user_status.get("gameId")
                                last_location = user_status.get("lastLocation", "")
                                
                                # presence_type 2 means "In Game"
                                if presence_type == 2:
                                    active_place = current_place_id
                                    old_place = last_played_place.get(user_id)

                                    # Only set start time if they were previously not playing
                                    if user_id not in session_start_times or old_place is None:
                                        session_start_times[user_id] = time.time()

                                    # Calculate duration securely from locked timestamp
                                    duration_seconds = int(time.time() - session_start_times[user_id])
                                    hours, remainder = divmod(duration_seconds, 3600)
                                    minutes = remainder // 60
                                    duration_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

                                    # If they just joined or switched games
                                    if active_place and old_place != active_place:
                                        if last_location and last_location.strip() and last_location != "Website":
                                            game_name = last_location
                                        else:
                                            game_name = await self.get_game_name(session, active_place)
                                            
                                        last_played_place[user_id] = active_place
                                        
                                        active_session_data[user_id] = {
                                            "username": username,
                                            "game_name": game_name,
                                            "active_place": active_place,
                                            "game_instance_id": game_instance_id
                                        }
                                        
                                        for server_cfg in servers_list:
                                            target_place_id = server_cfg.get("place_id")
                                            should_alert = (target_place_id is None) or (current_place_id == target_place_id or root_place_id == target_place_id)

                                            if should_alert:
                                                channel = self.get_channel(server_cfg.get("channel_id"))
                                                if channel:
                                                    faction = server_cfg.get("faction", "Unassigned")
                                                    role_id = server_cfg.get("role_id")
                                                    
                                                    if game_instance_id and active_place:
                                                        click_to_join = f"{SERVER_DOMAIN}/join?placeId={active_place}&gameInstanceId={game_instance_id}"
                                                    else:
                                                        click_to_join = f"https://www.roblox.com/games/{active_place}"
                                                    
                                                    embed = discord.Embed(
                                                        title="🟢 ONLINE - Playing Game",
                                                        description=f"**Player:** {username}\n**Faction:** {faction}\n**Game:** **{game_name}**\n**Status:** ONLINE (Duration: {duration_str})",
                                                        color=5814783
                                                    )
                                                    if avatar_url:
                                                        embed.set_thumbnail(url=avatar_url)
                                                    embed.add_field(name="Direct Join", value=f"[👉 Click Here to Join Game]({click_to_join})") 
                                                    
                                                    ping_text = f"<@&{role_id}>! " if role_id else ""
                                                    msg = await channel.send(content=f"{ping_text}Targeted player **{username}** is now active!", embed=embed)
                                                    
                                                    if user_id not in active_alert_messages:
                                                        active_alert_messages[user_id] = {}
                                                    active_alert_messages[user_id][server_cfg.get("guild_id")] = msg

                                    # If they are continuing to play, live-update the duration embed every 60 seconds
                                    elif user_id in active_alert_messages and user_id in active_session_data:
                                        s_data = active_session_data[user_id]
                                        for server_cfg in servers_list:
                                            guild_id = server_cfg.get("guild_id")
                                            if guild_id in active_alert_messages[user_id]:
                                                msg = active_alert_messages[user_id][guild_id]
                                                try:
                                                    faction = server_cfg.get("faction", "Unassigned")
                                                    embed = msg.embeds[0]
                                                    embed.description = (
                                                        f"**Player:** {s_data['username']}\n"
                                                        f"**Faction:** {faction}\n"
                                                        f"**Game:** **{s_data['game_name']}**\n"
                                                        f"**Status:** ONLINE (Duration: {duration_str})"
                                                    )
                                                    await msg.edit(embed=embed)
                                                except Exception as ex:
                                                    print(f"Error updating duration message for guild {guild_id}: {ex}")

                                else:
                                    # User left the game (Now OFFLINE)
                                    if last_played_place.get(user_id) is not None:
                                        last_played_place[user_id] = None
                                        session_start_times.pop(user_id, None)
                                        active_session_data.pop(user_id, None)
                                        
                                        for server_cfg in servers_list:
                                            channel = self.get_channel(server_cfg.get("channel_id"))
                                            if channel:
                                                embed = discord.Embed(
                                                    title="🔴 OFFLINE",
                                                    description=f"**Player:** {username}\n**Status:** OFFLINE",
                                                    color=15158332
                                                )
                                                if avatar_url:
                                                    embed.set_thumbnail(url=avatar_url)
                                                await channel.send(embed=embed)
                                                
                                        if user_id in active_alert_messages:
                                            active_alert_messages.pop(user_id, None)

                        elif response.status == 429:
                            await asyncio.sleep(120)
            except Exception as e:
                print(f"Error in monitor loop: {e}")

bot = RobloxTrackerBot()

# === CUSTOM CHECK FOR BOT MANAGER ROLE ===
def is_bot_manager():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
            
        guild_id = interaction.guild_id
        guild_doc = await db["guild_configs"].find_one({"_id": guild_id})
        if not guild_doc or "manager_role_id" not in guild_doc:
            await interaction.response.send_message(
                "⚠️ No bot manager role has been set up for this server yet. Ask an Administrator to use `/set_manager_role`.", 
                ephemeral=True
            )
            return False
            
        role_id = guild_doc["manager_role_id"]
        role = interaction.guild.get_role(role_id)
        
        if role and role in interaction.user.roles:
            return True
            
        await interaction.response.send_message(
            f"❌ You do not have the required bot manager role ({role.mention if role else 'Unknown Role'}) to use this command.", 
            ephemeral=True
        )
        return False
    return app_commands.check(predicate)

# === NEW COMMAND: SET MANAGER ROLE ===
@bot.tree.command(name="set_manager_role", description="Set the required role to manage tracked users in this server")
@app_commands.describe(role="The role allowed to add/remove tracked users")
@app_commands.checks.has_permissions(administrator=True)
async def set_manager_role(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    await db["guild_configs"].update_one(
        {"_id": interaction.guild_id},
        {"$set": {"manager_role_id": role.id}},
        upsert=True
    )
    await interaction.followup.send(
        f"✅ Successfully set the bot manager role to {role.mention}.",
        ephemeral=True
    )

@set_manager_role.error
async def set_manager_role_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("⚠️ You need **Administrator** permissions to set the bot manager role.", ephemeral=True)

# === SLASH COMMANDS ===
@bot.tree.command(name="track", description="Add or update a Roblox user to track for THIS server")
@app_commands.describe(
    user_id="The numeric Roblox User ID",
    channel="The Discord channel to send alerts to",
    faction="Faction name",
    place_id="Target Roblox Place ID (leave empty to track all games)",
    role="Role to ping (optional)"
)
@is_bot_manager()
async def track_user(
    interaction: discord.Interaction, 
    user_id: int, 
    channel: discord.TextChannel,
    faction: str = "Unassigned", 
    place_id: int = None,
    role: discord.Role = None
):
    await interaction.response.defer(ephemeral=True)

    game_name = "Any Game"
    if place_id:
        async with aiohttp.ClientSession() as session:
            game_name = await bot.get_game_name(session, place_id)

    if user_id not in TRACKED_USERS:
        TRACKED_USERS[user_id] = []
        last_played_place[user_id] = None

    TRACKED_USERS[user_id] = [cfg for cfg in TRACKED_USERS[user_id] if cfg.get("guild_id") != interaction.guild_id]
    
    TRACKED_USERS[user_id].append({
        "guild_id": interaction.guild_id,
        "channel_id": channel.id,
        "role_id": role.id if role else None,
        "faction": faction,
        "place_id": place_id,
        "game_name": game_name
    })
    
    await collection.update_one(
        {"_id": user_id}, 
        {"$set": {"servers": TRACKED_USERS[user_id]}}, 
        upsert=True 
    )
    
    role_mention = role.mention if role else "None"
    target_game_str = f"**{game_name}**" if place_id else "**Any Game**"

    await interaction.followup.send(
        f"✅ Now tracking user ID `{user_id}` (**{faction}**) for {target_game_str} in this server.\n"
        f"📢 **Channel:** {channel.mention}\n"
        f"🔔 **Role Mention:** {role_mention}",
        ephemeral=True
    )

@bot.tree.command(name="untrack", description="Stop tracking a Roblox user in THIS server")
@app_commands.describe(user_id="The numeric Roblox User ID to remove")
@is_bot_manager()
async def untrack_user(interaction: discord.Interaction, user_id: int):
    if user_id in TRACKED_USERS:
        original_length = len(TRACKED_USERS[user_id])
        TRACKED_USERS[user_id] = [cfg for cfg in TRACKED_USERS[user_id] if cfg.get("guild_id") != interaction.guild_id]
        
        if len(TRACKED_USERS[user_id]) < original_length:
            if len(TRACKED_USERS[user_id]) == 0:
                del TRACKED_USERS[user_id]
                last_played_place.pop(user_id, None)
                await collection.delete_one({"_id": user_id})
            else:
                await collection.update_one({"_id": user_id}, {"$set": {"servers": TRACKED_USERS[user_id]}})
                
            await interaction.response.send_message(f"❌ Stopped tracking user ID `{user_id}` in this server.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ User ID `{user_id}` wasn't tracked in this server.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ User ID `{user_id}` is not currently being tracked at all.", ephemeral=True)

@bot.tree.command(name="list_tracked", description="View tracked users for THIS server")
async def list_tracked(interaction: discord.Interaction):
    if not TRACKED_USERS:
        await interaction.response.send_message("No users are currently being tracked.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        lines = []
        for uid, servers_list in TRACKED_USERS.items():
            for cfg in servers_list:
                if cfg.get("guild_id") == interaction.guild_id:
                    username = await bot.get_username(session, uid)
                    game_name = cfg.get("game_name", "Any Game")
                    ch_id = cfg.get("channel_id")
                    r_id = cfg.get("role_id")
                    
                    role_display = f"<@&{r_id}>" if r_id else "None"
                    channel_display = f"<#{ch_id}>" if ch_id else "Unknown"
                    
                    lines.append(
                        f"• **{username}** (`{uid}`) | **Faction:** {cfg.get('faction', 'Unassigned')} | **Game:** **{game_name}** | **Channel:** {channel_display} | **Role:** {role_display}"
                    )

    if lines:
        summary = "\n".join(lines)
        embed = discord.Embed(title="📋 Tracked Roblox Users (This Server)", description=summary, color=3447003)
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send("No users are currently being tracked in this specific server.", ephemeral=True)

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)
