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

# === MONGODB DATABASE SETUP ===
# Make sure to replace this with your actual MongoDB Connection String!
MONGO_URL = "PASTE_YOUR_CONNECTION_STRING_HERE"
cluster = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = cluster["roblox_tracker"]
collection = db["users"]

# === INITIALIZE DATA ===
# TRACKED_USERS will now hold a list of server configurations for each user
TRACKED_USERS = {} 
last_played_place = {}
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
            # 'servers' is a list of dictionaries containing channel, role, guild, etc.
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

    @tasks.loop(seconds=60)
    async def monitor_loop(self):
        async with aiohttp.ClientSession() as session:
            try:
                for user_id, servers_list in list(TRACKED_USERS.items()):
                    if not servers_list:
                        continue # Nobody is tracking this user anymore
                        
                    username = await self.get_username(session, user_id)
                    
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

                                    # Only process if they changed games/joined a new server
                                    if active_place and old_place != active_place:
                                        
                                        # Figure out game name once for all servers
                                        if last_location and last_location.strip() and last_location != "Website":
                                            game_name = last_location
                                        else:
                                            game_name = await self.get_game_name(session, active_place)
                                            
                                        # Now check EVERY server that is tracking this user
                                        for server_cfg in servers_list:
                                            target_place_id = server_cfg.get("place_id")
                                            should_alert = False

                                            if target_place_id is None:
                                                # This server tracks "Any Game"
                                                should_alert = True
                                            else:
                                                # This server tracks a Specific Game
                                                if current_place_id == target_place_id or root_place_id == target_place_id:
                                                    should_alert = True

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
                                                        title="🎮 Join Server",
                                                        description=f"**Player:** {username}\n**Faction:** {faction}\n**Game:** **{game_name}**",
                                                        color=5814783
                                                    )
                                                    embed.add_field(name="Direct Join", value=f"[👉 Click Here to Join Game]({click_to_join})")
                                                    
                                                    ping_text = f"<@&{role_id}>! " if role_id else ""
                                                    message_content = f"{ping_text}Targeted player **{username}** of **{faction}** is now active in **{game_name}**!"

                                                    message = await channel.send(content=message_content, embed=embed)
                                                    if channel.is_news():
                                                        await message.publish()
                                                        
                                        # Update global location after checking all servers
                                        last_played_place[user_id] = active_place
                                else:
                                    last_played_place[user_id] = None

                        elif response.status == 429:
                            print("[WARNING] Roblox API rate limit hit. Pausing for 2 minutes...")
                            await asyncio.sleep(120)

            except Exception as e:
                print(f"An error occurred in monitor_loop: {e}")

bot = RobloxTrackerBot()

# === SLASH COMMANDS ===
@bot.tree.command(name="track", description="Add or update a Roblox user to track for THIS server")
@app_commands.describe(
    user_id="The numeric Roblox User ID",
    channel="The Discord channel to send alerts to",
    faction="Faction name",
    place_id="Target Roblox Place ID (leave empty to track all games)",
    role="Role to ping (optional)"
)
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

    # Make sure this user exists in our tracking dictionary
    if user_id not in TRACKED_USERS:
        TRACKED_USERS[user_id] = []
        last_played_place[user_id] = None

    # Clear out any old configuration FOR THIS SPECIFIC SERVER to prevent duplicate alerts
    TRACKED_USERS[user_id] = [cfg for cfg in TRACKED_USERS[user_id] if cfg.get("guild_id") != interaction.guild_id]
    
    # Append the new configuration for this server
    TRACKED_USERS[user_id].append({
        "guild_id": interaction.guild_id,
        "channel_id": channel.id,
        "role_id": role.id if role else None,
        "faction": faction,
        "place_id": place_id,
        "game_name": game_name
    })
    
    # Save the updated list to MongoDB
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
async def untrack_user(interaction: discord.Interaction, user_id: int):
    if user_id in TRACKED_USERS:
        original_length = len(TRACKED_USERS[user_id])
        
        # Filter out the configuration that matches the server the command was typed in
        TRACKED_USERS[user_id] = [cfg for cfg in TRACKED_USERS[user_id] if cfg.get("guild_id") != interaction.guild_id]
        
        if len(TRACKED_USERS[user_id]) < original_length:
            if len(TRACKED_USERS[user_id]) == 0:
                # If no servers are tracking this user anymore, delete them entirely
                del TRACKED_USERS[user_id]
                last_played_place.pop(user_id, None)
                await collection.delete_one({"_id": user_id})
            else:
                # Update MongoDB with the modified list
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
                # Only show the user if they are tracked in THIS specific Discord server
                if cfg.get("guild_id") == interaction.guild_id:
                    username = await bot.get_username(session, uid)
                    p_id = cfg.get("place_id")
                    
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
    bot.run("MTUzOTg0MjEyNjAzOTM1OTQ4OA.GMQa_G.kBexGt4556mlJLQh2Y9P6GrLEOA4Kp2k4oVebs")
