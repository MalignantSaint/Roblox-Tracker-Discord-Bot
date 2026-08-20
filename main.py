import asyncio
from threading import Thread
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, request
from waitress import serve

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

# === CONFIGURATION & DEFAULTS ===
DEFAULT_ROLE_ID = 1539998360046407801
DEFAULT_CHANNEL_ID = 1301548308610940970

TRACKED_USERS = {
    6054221747: {"place_id": 110823256031006, "faction": "The Lapis Fleet", "channel_id": DEFAULT_CHANNEL_ID, "role_id": DEFAULT_ROLE_ID},
    3655587119: {"place_id": 110823256031006, "faction": "The Crimson Alliance", "channel_id": DEFAULT_CHANNEL_ID, "role_id": DEFAULT_ROLE_ID},
    1304868946: {"place_id": 110823256031006, "faction": "The Crimson Alliance", "channel_id": DEFAULT_CHANNEL_ID, "role_id": DEFAULT_ROLE_ID},
    8309322015: {"place_id": 110823256031006, "faction": "The Lapis Fleet", "channel_id": DEFAULT_CHANNEL_ID, "role_id": DEFAULT_ROLE_ID},
    4977310930: {"place_id": 110823256031006, "faction": "The Lapis Fleet", "channel_id": DEFAULT_CHANNEL_ID, "role_id": DEFAULT_ROLE_ID},
}

already_playing_state = {user_id: False for user_id in TRACKED_USERS}
username_cache = {}
game_name_cache = {}

# === DISCORD BOT SETUP ===
class RobloxTrackerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("[INFO] Slash commands synchronized globally.")
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
        if place_id in game_name_cache:
            return game_name_cache[place_id]
        try:
            url = f"https://economy.roblox.com/v1/assets/{place_id}/details"
            async with session.get(url, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    name = data.get("Name", f"Place {place_id}")
                    game_name_cache[place_id] = name
                    return name
        except Exception as e:
            print(f"Error fetching game name for Place ID {place_id}: {e}")
        return f"Place {place_id}"

    @tasks.loop(seconds=60)
    async def monitor_loop(self):
        async with aiohttp.ClientSession() as session:
            try:
                for user_id, user_data in list(TRACKED_USERS.items()):
                    target_channel_id = user_data.get("channel_id", DEFAULT_CHANNEL_ID)
                    role_id = user_data.get("role_id", DEFAULT_ROLE_ID)
                    
                    channel = self.get_channel(target_channel_id)
                    if not channel:
                        continue

                    target_place_id = user_data["place_id"]
                    faction = user_data["faction"]
                    game_name = user_data.get("game_name") or await self.get_game_name(session, target_place_id)
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
                                game_instance_id = user_status.get("gameId")
                                last_location = user_status.get("lastLocation", "")
                                
                                is_playing_target = False
                                if presence_type == 2:
                                    if current_place_id == target_place_id or str(target_place_id) in last_location:
                                        is_playing_target = True

                                if is_playing_target:
                                    if not already_playing_state.get(user_id, False):
                                        if game_instance_id:
                                            click_to_join = f"{SERVER_DOMAIN}/join?placeId={target_place_id}&gameInstanceId={game_instance_id}"
                                        else:
                                            click_to_join = f"https://www.roblox.com/games/{target_place_id}"
                                        
                                        embed = discord.Embed(
                                            title="🎮 Join Server",
                                            description=f"**Player:** {username}\n**Faction:** {faction}\n**Game:** **{game_name}**",
                                            color=5814783
                                        )
                                        embed.add_field(name="Direct Join", value=f"[👉 Click Here to Join Game]({click_to_join})")
                                        
                                        message = await channel.send(
                                            content=f"<@&{role_id}>! Targeted player **{username}** of **{faction}** is now active in **{game_name}**!",
                                            embed=embed
                                        )
                                        
                                        if channel.is_news():
                                            await message.publish()
                                            print(f"[SUCCESS] Auto-published alert for {username}!")
                                            
                                        already_playing_state[user_id] = True
                                else:
                                    if already_playing_state.get(user_id, False):
                                        print(f"User {username} left. Resetting state.")
                                        already_playing_state[user_id] = False

                        elif response.status == 429:
                            print("[WARNING] Roblox API rate limit hit. Pausing for 2 minutes...")
                            await asyncio.sleep(120)

            except Exception as e:
                print(f"An error occurred in monitor_loop: {e}")

    @monitor_loop.before_loop
    async def before_monitor_loop(self):
        await self.wait_until_ready()
        print(f"Bot logged in as {self.user} and monitoring {len(TRACKED_USERS)} users...")

bot = RobloxTrackerBot()

# === SLASH COMMANDS ===
@bot.tree.command(name="track", description="Add or update a Roblox user to track")
@app_commands.describe(
    user_id="The numeric Roblox User ID",
    faction="Faction name",
    place_id="Target Roblox Place ID",
    channel="Target channel for alerts (optional)",
    role="Role to ping (optional)"
)
async def track_user(
    interaction: discord.Interaction, 
    user_id: int, 
    faction: str = "Unassigned", 
    place_id: int = 110823256031006,
    channel: discord.TextChannel = None,
    role: discord.Role = None
):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        game_name = await bot.get_game_name(session, place_id)

    target_channel_id = channel.id if channel else DEFAULT_CHANNEL_ID
    target_role_id = role.id if role else DEFAULT_ROLE_ID

    TRACKED_USERS[user_id] = {
        "place_id": place_id, 
        "faction": faction, 
        "game_name": game_name,
        "channel_id": target_channel_id,
        "role_id": target_role_id
    }
    already_playing_state[user_id] = False
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, 
            name=f"{len(TRACKED_USERS)} Roblox players"
        )
    )

    channel_mention = channel.mention if channel else f"<#{DEFAULT_CHANNEL_ID}>"
    role_mention = role.mention if role else f"<@&{DEFAULT_ROLE_ID}>"

    await interaction.followup.send(
        f"✅ Now tracking user ID `{user_id}` (**{faction}**) for **{game_name}**.\n"
        f"📢 **Channel:** {channel_mention}\n"
        f"🔔 **Role Mention:** {role_mention}",
        ephemeral=True
    )

@bot.tree.command(name="untrack", description="Stop tracking a Roblox user")
@app_commands.describe(user_id="The numeric Roblox User ID to remove")
async def untrack_user(interaction: discord.Interaction, user_id: int):
    if user_id in TRACKED_USERS:
        del TRACKED_USERS[user_id]
        already_playing_state.pop(user_id, None)
        
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, 
                name=f"{len(TRACKED_USERS)} Roblox players"
            )
        )
        await interaction.response.send_message(f"❌ Stopped tracking user ID `{user_id}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ User ID `{user_id}` is not currently being tracked.", ephemeral=True)

@bot.tree.command(name="list_tracked", description="View all currently tracked users")
async def list_tracked(interaction: discord.Interaction):
    if not TRACKED_USERS:
        await interaction.response.send_message("No users are currently being tracked.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        lines = []
        for uid, data in TRACKED_USERS.items():
            username = await bot.get_username(session, uid)
            game_name = data.get("game_name") or await bot.get_game_name(session, data["place_id"])
            ch_id = data.get("channel_id", DEFAULT_CHANNEL_ID)
            r_id = data.get("role_id", DEFAULT_ROLE_ID)
            lines.append(
                f"• **{username}** (`{uid}`) | **Faction:** {data['faction']} | **Game:** **{game_name}** | **Channel:** <#{ch_id}> | **Role:** <@&{r_id}>"
            )

    summary = "\n".join(lines)
    embed = discord.Embed(title="📋 Tracked Roblox Users", description=summary, color=3447003)
    await interaction.followup.send(embed=embed, ephemeral=True)

if __name__ == "__main__":
    Thread(target=run_web_server, daemon=True).start()
    bot.run("MTUzOTg0MjEyNjAzOTM1OTQ4OA.GMQa_G.kBexGt4556mlJLQh2Y9P6GrLEOA4Kp2k4oVebs")
