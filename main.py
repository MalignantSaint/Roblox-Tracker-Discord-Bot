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
import logging

# Load environment variables from the .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("roblox-tracker")

# Grab secrets securely from the environment and validate them early
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN not set in environment")
    raise SystemExit("DISCORD_TOKEN not set in environment")

if not MONGO_URL:
    logger.error("MONGO_URL not set in environment")
    raise SystemExit("MONGO_URL not set in environment")

# === MONGODB DATABASE SETUP ===
cluster = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = cluster["roblox_tracker"]
collection = db["users"]
games_collection = db["tracked_games"]

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
active_alert_messages = {}  # {user_id: {guild_id: message_object}}
active_session_data = {}    # {user_id: {"username": ..., "game_name": ...}}
username_cache = {}
game_name_cache = {}

# === DISCORD BOT SETUP ===
class RobloxTrackerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
    
    async def setup_hook(self):
        global TRACKED_USERS

        # Load all users and their server lists from MongoDB
        try:
            cursor = collection.find({})
            async for document in cursor:
                try:
                    user_id = int(document["_id"])
                except Exception:
                    # If the stored _id isn't castable to int, skip with warning
                    logger.warning("Found non-int _id in users collection: %s", document.get("_id"))
                    continue
                TRACKED_USERS[user_id] = document.get("servers", [])
        except Exception as e:
            logger.exception("Failed to load tracked users from DB: %s", e)

        # Baseline state check on startup (batched to prevent API spam)
        if TRACKED_USERS:
            user_ids = list(TRACKED_USERS.keys())
            async with aiohttp.ClientSession() as session:
                # Roblox allows up to 100 user IDs per batch request
                for i in range(0, len(user_ids), 100):
                    batch = user_ids[i:i+100]
                    try:
                        presence_url = "https://presence.roblox.com/v1/presence/users"
                        async with session.post(presence_url, json={"userIds": batch}, timeout=10) as response:
                            if response.status == 200:
                                data = await response.json()
                                for user_status in data.get("userPresences", []):
                                    u_id = user_status.get("userId")
                                    if user_status.get("userPresenceType") == 2:
                                        current_place = user_status.get("placeId") or user_status.get("rootPlaceId")
                                        last_played_place[u_id] = current_place
                                        session_start_times[u_id] = time.time()
                                    else:
                                        last_played_place[u_id] = None
                            elif response.status == 429:
                                retry_after = 30
                                try:
                                    data = await response.json()
                                    retry_after = data.get("retryAfter", 30)
                                except Exception:
                                    pass
                                logger.warning(f"Rate limited during startup initialization. Sleeping for {retry_after}s...")
                                await asyncio.sleep(float(retry_after))
                            else:
                                # Non-200 status: log for debugging but continue
                                logger.warning("Unexpected status %s from presence API during startup", response.status)
                    except Exception as e:
                        logger.exception("Error initializing state batch: %s", e)

                    await asyncio.sleep(2.0)  # Pause between batches

        # Sync commands and start monitoring loop only once
        try:
            await self.tree.sync()
            logger.info("Slash commands synced. Initialized %d users from database.", len(TRACKED_USERS))
        except Exception as e:
            logger.exception("Error syncing application commands: %s", e)

        if not self.monitor_loop.is_running():
            self.monitor_loop.start()
        if not self.monitor_game_updates.is_running():
            self.monitor_game_updates.start()

    async def get_username(self, session, user_id):
        key = int(user_id)
        if key in username_cache:
            return username_cache[key]
        try:
            url = f"https://users.roblox.com/v1/users/{key}"
            async with session.get(url, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    name = data.get("name", str(key))
                    username_cache[key] = name
                    return name
                else:
                    logger.debug("Non-200 from users API for %s: %s", key, res.status)
        except Exception as e:
            logger.exception("Error fetching username for %s: %s", user_id, e)
        return str(key)

    async def get_game_name(self, session, place_id):
        if not place_id:
            return "Unknown Game"
        key = int(place_id)
        if key in game_name_cache:
            return game_name_cache[key]
        try:
            url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={key}"
            async with session.get(url, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        name = data[0].get("name")
                        if name:
                            game_name_cache[key] = name
                            return name
                else:
                    logger.debug("Non-200 from games API for %s: %s", key, res.status)
        except Exception as e:
            logger.exception("Error fetching game name for Place ID %s: %s", place_id, e)
        return f"Place {place_id}"

    async def get_game_details(place_id, session):
        # Step 1: Get Universe ID from Place ID
        universe_url = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
        async with session.get(universe_url) as res:
            if res.status != 200:
                print(f"[DEBUG] Universe API Error. Status code: {res.status}")
                return None
            universe_data = await res.json()
            universe_id = universe_data.get("universeId")

        # Step 2: Get Game Details from Universe ID
        games_url = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
        async with session.get(games_url) as res:
            if res.status != 200:
                print(f"[DEBUG] Games API Error. Status code: {res.status}")
                return None
            games_data = await res.json()
            if games_data.get("data"):
                return games_data["data"][0] # Contains 'updated' timestamp, 'name', etc.

    async def get_avatar_thumbnail(self, session, user_id):
        try:
            key = int(user_id)
            url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={key}&size=150x150&format=Png&isCircular=false"
            async with session.get(url, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    thumbnails = data.get("data", [])
                    if thumbnails:
                        return thumbnails[0].get("imageUrl", None)
                else:
                    logger.debug("Non-200 from thumbnails API for %s: %s", key, res.status)
        except Exception as e:
            logger.exception("Error fetching avatar thumbnail for %s: %s", user_id, e)
        return None

    '''# === BACKGROUND TASK: GAME UPDATE MONITOR ===
    @tasks.loop(minutes=5)
    async def monitor_game_updates(self):
        async with aiohttp.ClientSession() as session:
            try:
                cursor = games_collection.find({})
                tracked_docs = await cursor.to_list(length=None)
            
                if not tracked_docs:
                    return

                for doc in tracked_docs:
                    place_id = doc["place_id"]
                    guild_id = doc["guild_id"]
                    channel_id = doc["channel_id"]
                    stored_last_updated = doc.get("last_updated", 0)

                    url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
                    async with session.get(url, timeout=10) as res:
                        if res.status == 200:
                            data = await res.json()
                            if data and isinstance(data, list) and len(data) > 0:
                                place_info = data[0]
                                game_name = place_info.get("name", "Unknown Game")
                                updated_iso = place_info.get("updated")
                            
                                if not updated_iso:
                                    continue

                                from datetime import datetime
                                dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
                                current_timestamp = int(dt.timestamp())

                                # If it's the first time checking or database is empty, initialize it
                                if stored_last_updated == 0:
                                    await games_collection.update_one(
                                        {"place_id": place_id, "guild_id": guild_id},
                                        {"$set": {"last_updated": current_timestamp, "previous_updated": current_timestamp, "game_name": game_name}}
                                    )
                                elif current_timestamp > stored_last_updated:
                                    # Update detected! Use stored_last_updated as the previous time
                                    prev_timestamp = stored_last_updated
                                    new_timestamp = current_timestamp

                                    await games_collection.update_one(
                                        {"place_id": place_id, "guild_id": guild_id},
                                        {"$set": {"last_updated": new_timestamp, "previous_updated": prev_timestamp, "game_name": game_name}}
                                    )

                                    message_content = (
                                        f"🚨 PLACE UPDATED - {game_name}\n"
                                        f"Game Info\n"
                                        f"`Game Name:` {game_name}\n"
                                        f"[Game Link](https://roblox.com/games/{place_id}/)\n"
                                        f"Update Info\n"
                                        f"`Last updated -` (<t:{new_timestamp}:R>) <t:{new_timestamp}:f>\n"
                                        f"`Recently updated -` (<t:{prev_timestamp}:R>) <t:{prev_timestamp}:f>"
                                    )

                                    channel = self.get_channel(channel_id)
                                    if not channel:
                                        try:
                                            channel = await self.fetch_channel(channel_id)
                                        except Exception:
                                            continue
                                    
                                    try:
                                        await channel.send(message_content)
                                    except Exception as e:
                                        logger.exception("Failed to send update alert: %s", e)

                    await asyncio.sleep(2.0)
            except Exception as e:
                logger.exception("Error in monitor_game_updates loop: %s", e) '''

    # === BACKGROUND TASK: GAME UPDATE MONITOR (DEBUG MODE) ===
    @tasks.loop(seconds=30) # Sped up for testing
    async def monitor_game_updates(self):
        logger.info("\n[DEBUG] --- Starting Game Update Check ---")
        async with aiohttp.ClientSession() as session:
            try:
                cursor = games_collection.find({})
                tracked_docs = await cursor.to_list(length=None)
            
                if not tracked_docs:
                    logger.info("[DEBUG] No games found in database to track.")
                    return

                for doc in tracked_docs:
                    place_id = doc.get("place_id")
                    guild_id = doc.get("guild_id")
                    channel_id = doc.get("channel_id")
                    stored_last_updated = doc.get("last_updated", 0)

                    logger.info(f"[DEBUG] Checking Place ID: {place_id} | Stored DB Time: {stored_last_updated} | Channel ID: {channel_id}")

                    url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
                    async with session.get(url, timeout=10) as res:
                        if res.status == 200:
                            data = await res.json()
                            if not data or len(data) == 0:
                                logger.info(f"[DEBUG] Roblox API returned EMPTY data for Place {place_id}. Is this a Universe ID?")
                                continue

                            place_info = data[0]
                            game_name = place_info.get("name", "Unknown Game")
                            updated_iso = place_info.get("updated")
                        
                            if not updated_iso:
                                logger.info(f"[DEBUG] No 'updated' timestamp found in API response for {game_name}")
                                continue

                            from datetime import datetime
                            dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
                            current_timestamp = int(dt.timestamp())

                            logger.info(f"[DEBUG] API Time for {game_name}: {current_timestamp}")

                            if stored_last_updated == 0:
                                logger.info("[DEBUG] Initializing game (0 detected).")
                                await games_collection.update_one(
                                    {"place_id": place_id, "guild_id": guild_id},
                                    {"$set": {"last_updated": current_timestamp, "previous_updated": current_timestamp, "game_name": game_name}}
                                )
                            elif current_timestamp > stored_last_updated:
                                logger.info(f"[DEBUG] UPDATE DETECTED! {current_timestamp} > {stored_last_updated}")
                                prev_timestamp = stored_last_updated
                                new_timestamp = current_timestamp

                                await games_collection.update_one(
                                    {"place_id": place_id, "guild_id": guild_id},
                                    {"$set": {"last_updated": new_timestamp, "previous_updated": prev_timestamp, "game_name": game_name}}
                                )

                                channel = self.get_channel(int(channel_id))
                                if not channel:
                                    try:
                                        channel = await self.fetch_channel(int(channel_id))
                                    except Exception as e:
                                        logger.info(f"[DEBUG] CRITICAL ERROR: Could not find or fetch Discord channel {channel_id}. Error: {e}")
                                        continue
                                
                                message_content = (
                                    f"🚨 PLACE UPDATED - {game_name}\n"
                                    f"Game Info\n"
                                    f"`Game Name:` {game_name}\n"
                                    f"[Game Link](https://roblox.com/games/{place_id}/)\n"
                                    f"Update Info\n"
                                    f"`Last updated -` (<t:{new_timestamp}:R>) <t:{new_timestamp}:f>\n"
                                    f"`Recently updated -` (<t:{prev_timestamp}:R>) <t:{prev_timestamp}:f>"
                                )

                                try:
                                    logger.info(f"[DEBUG] Attempting to send message to channel {channel.name}...")
                                    await channel.send(message_content)
                                    logger.info("[DEBUG] Message sent successfully!")
                                except Exception as e:
                                    logger.info(f"[DEBUG] Failed to send discord message: {e}")
                            else:
                                logger.info("[DEBUG] No new update. Timestamps match.")
                        else:
                            logger.info(f"[DEBUG] API Error. Status code: {res.status}")

                    await asyncio.sleep(2.0)
            except Exception as e:
                logger.info(f"[DEBUG] Error in loop: {e}")

    @monitor_game_updates.before_loop
    async def before_game_updates(self):
        await self.wait_until_ready()
    
    @tasks.loop(seconds=90)  # Increased from 60s to 90s to naturally lower request frequency
    async def monitor_loop(self):
        async with aiohttp.ClientSession() as session:
            try:
                for user_id, servers_list in list(TRACKED_USERS.items()):
                    if not servers_list:
                        continue

                    await asyncio.sleep(2.0)

                    username = await self.get_username(session, user_id)
                    avatar_url = await self.get_avatar_thumbnail(session, user_id)

                    presence_url = "https://presence.roblox.com/v1/presence/users"
                    try:
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

                                    active_place = current_place_id or root_place_id
                                    was_playing = last_played_place.get(user_id) is not None

                                    # presence_type 2 means "In Game"
                                    if presence_type == 2 and active_place:
                                        game_name = await self.get_game_name(session, active_place)

                                        if not was_playing or user_id not in session_start_times:
                                            session_start_times[user_id] = time.time()
                                            last_played_place[user_id] = active_place

                                        duration_seconds = int(time.time() - session_start_times[user_id])
                                        hours, remainder = divmod(duration_seconds, 3600)
                                        minutes = remainder // 60
                                        duration_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

                                        if (not was_playing) or (last_played_place.get(user_id) != active_place):
                                            last_played_place[user_id] = active_place

                                            active_session_data[user_id] = {
                                                "username": username,
                                                "game_name": game_name,
                                                "active_place": active_place,
                                                "game_instance_id": game_instance_id
                                            }

                                            for server_cfg in servers_list:
                                                target_place_id = server_cfg.get("place_id")
                                                should_alert = (target_place_id is None) or (active_place == target_place_id)

                                                if should_alert:
                                                    channel_id = server_cfg.get("channel_id")
                                                    channel = self.get_channel(channel_id)
                                                    if not channel:
                                                        try:
                                                            channel = await self.fetch_channel(channel_id)
                                                        except discord.NotFound:
                                                            logger.warning("Channel %s not found for guild %s; skipping", channel_id, server_cfg.get("guild_id"))
                                                            continue
                                                        except Exception as e:
                                                            logger.exception("Error fetching channel %s: %s", channel_id, e)
                                                            continue

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
                                                    try:
                                                        msg = await channel.send(content=f"{ping_text}Targeted player **{username}** is now active!", embed=embed)
                                                    except discord.Forbidden:
                                                        logger.warning("Missing permissions to send messages in channel %s (guild %s).", channel_id, server_cfg.get("guild_id"))
                                                        continue
                                                    except Exception as e:
                                                        logger.exception("Failed to send online alert in channel %s: %s", channel_id, e)
                                                        continue

                                                    if user_id not in active_alert_messages:
                                                        active_alert_messages[user_id] = {}
                                                    active_alert_messages[user_id][server_cfg.get("guild_id")] = msg

                                        elif user_id in active_alert_messages and user_id in active_session_data:
                                            s_data = active_session_data[user_id]
                                            for server_cfg in servers_list:
                                                guild_id = server_cfg.get("guild_id")
                                                if user_id in active_alert_messages and guild_id in active_alert_messages[user_id]:
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
                                                    except Exception:
                                                        logger.exception("Error updating duration message for guild %s", guild_id)

                                    else:
                                        # User is not playing now but was playing before: finalize offline state
                                        if was_playing:
                                            if user_id in active_session_data and user_id in session_start_times:
                                                duration_seconds = int(time.time() - session_start_times[user_id])
                                                hours, remainder = divmod(duration_seconds, 3600)
                                                minutes = remainder // 60
                                                duration_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

                                                s_data = active_session_data[user_id]
                                                for server_cfg in servers_list:
                                                    guild_id = server_cfg.get("guild_id")
                                                    if user_id in active_alert_messages and guild_id in active_alert_messages[user_id]:
                                                        msg = active_alert_messages[user_id][guild_id]
                                                        try:
                                                            faction = server_cfg.get("faction", "Unassigned")
                                                            embed = msg.embeds[0]

                                                            embed.title = "🔴 OFFLINE"
                                                            embed.color = 15158332
                                                            embed.description = (
                                                                f"**Player:** {s_data['username']}\n"
                                                                f"**Faction:** {faction}\n"
                                                                f"**Game:** **{s_data['game_name']}**\n"
                                                                f"**Status:** OFFLINE (Duration: {duration_str})"
                                                            )
                                                            embed.clear_fields()

                                                            try:
                                                                await msg.edit(content=f"Player **{s_data['username']}** is now offline.", embed=embed)
                                                            except discord.Forbidden:
                                                                logger.warning("Missing permissions to edit messages in guild %s", guild_id)
                                                            except Exception:
                                                                logger.exception("Error editing final offline message for guild %s", guild_id)
                                                        except Exception:
                                                            logger.exception("Error preparing final offline embed for guild %s", guild_id)

                                            last_played_place[user_id] = None
                                            session_start_times.pop(user_id, None)
                                            active_session_data.pop(user_id, None)
                                            if user_id in active_alert_messages:
                                                active_alert_messages.pop(user_id, None)

                            elif response.status == 429:
                                # REPLACE THIS PART to respect Roblox's dynamic backoff
                                retry_after = 180
                                try:
                                    data = await response.json()
                                    retry_after = data.get("retryAfter", 180)
                                except Exception:
                                    pass
                                logger.warning(f"Hit Roblox presence API rate limit (429). Pausing loop for {retry_after}s...")
                                await asyncio.sleep(float(retry_after))
                            else:
                                logger.debug("Unexpected presence API status %s for user %s", response.status, user_id)
                    except asyncio.TimeoutError:
                        logger.warning("Timeout when checking presence for user %s", user_id)
                    except Exception:
                        logger.exception("Unexpected error when checking presence for user %s", user_id)

            except discord.HTTPException as he:
                try:
                    status = getattr(he, "status", None)
                    if status == 429:
                        logger.error("Encountered Discord 429 Rate Limit. Backing off.")
                        await asyncio.sleep(300)
                    else:
                        logger.exception("Discord HTTP Exception in monitor loop: %s", he)
                except Exception:
                    logger.exception("Error handling discord HTTP exception: %s", he)
            except Exception as e:
                logger.exception("Error in monitor loop: %s", e)

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
@bot.tree.command(name="track_game_updates", description="Track a Roblox game for update notifications in this server")
@app_commands.describe(
    place_id="The numeric Roblox Place ID",
    channel="The Discord channel to send update alerts to"
)
@is_bot_manager()
async def track_game_updates(interaction: discord.Interaction, place_id: int, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)

    # Fetch game name initially
    async with aiohttp.ClientSession() as session:
        game_name = await bot.get_game_name(session, place_id)
        
        # Get current update timestamp from Roblox API
        url = f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"
        current_timestamp = int(time.time())
        async with session.get(url, timeout=10) as res:
            if res.status == 200:
                data = await res.json()
                if data and isinstance(data, list) and len(data) > 0:
                    updated_iso = data[0].get("updated")
                    if updated_iso:
                        from datetime import datetime
                        dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
                        current_timestamp = int(dt.timestamp())

    # Save to MongoDB
    await games_collection.update_one(
        {"place_id": place_id, "guild_id": interaction.guild_id},
        {
            "$set": {
                "channel_id": channel.id,
                "game_name": game_name,
                "last_updated": current_timestamp,
                "previous_updated": current_timestamp
            }
        },
        upsert=True
    )

    await interaction.followup.send(
        f"✅ Now tracking updates for **{game_name}** (`{place_id}`) in {channel.mention}.",
        ephemeral=True
    )

@bot.tree.command(name="list_tracked_games", description="View Roblox games being tracked for updates in THIS server")
async def list_tracked_games(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Fetch tracked games for this specific guild from MongoDB
    cursor = games_collection.find({"guild_id": interaction.guild_id})
    tracked_games = await cursor.to_list(length=None)

    if not tracked_games:
        await interaction.followup.send("No game updates are currently being tracked in this server.", ephemeral=True)
        return

    lines = []
    for doc in tracked_games:
        place_id = doc.get("place_id")
        game_name = doc.get("game_name", "Unknown Game")
        channel_id = doc.get("channel_id")
        channel_display = f"<#{channel_id}>" if channel_id else "Unknown"
        
        last_timestamp = doc.get("last_updated", 0)
        time_display = f"<t:{last_timestamp}:R>" if last_timestamp else "Never"

        lines.append(
            f"• **{game_name}** (`{place_id}`) | **Channel:** {channel_display} | **Last Updated:** {time_display}"
        )

    summary = "\n".join(lines)
    embed = discord.Embed(
        title="📋 Tracked Game Updates (This Server)",
        description=summary,
        color=3447003
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="untrack_game_updates", description="Stop tracking a Roblox game's updates in this server")
@app_commands.describe(place_id="The numeric Roblox Place ID to remove")
@is_bot_manager()
async def untrack_game_updates(interaction: discord.Interaction, place_id: int):
    result = await games_collection.delete_one({"place_id": place_id, "guild_id": interaction.guild_id})
    if result.deleted_count > 0:
        await interaction.response.send_message(f"❌ Stopped tracking updates for Place ID `{place_id}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Place ID `{place_id}` was not being tracked in this server.", ephemeral=True)

@bot.tree.command(name="test_alert", description="Force an update alert test")
@is_bot_manager()
async def test_alert(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.send_message("Sending test...", ephemeral=True)
    
    current_time = int(time.time())
    message_content = (
        f"🚨 PLACE UPDATED - Test Game\n"
        f"Game Info\n"
        f"`Game Name:` Debug Test\n"
        f"[Game Link](https://roblox.com/)\n"
        f"Update Info\n"
        f"`Last updated -` (<t:{current_time}:R>) <t:{current_time}:f>\n"
        f"`Recently updated -` (<t:{current_time - 3600}:R>) <t:{current_time - 3600}:f>"
    )
    
    try:
        await channel.send(message_content)
        await interaction.followup.send("Test message sent successfully!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to send: {e}", ephemeral=True)

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

    # Ensure only one config per guild per user
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
