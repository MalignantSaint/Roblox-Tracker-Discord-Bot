import discord
from discord.ext import tasks
import aiohttp
import asyncio
from threading import Thread
from flask import Flask, request

# === FLASK SERVER ===
app = Flask("")
SERVER_DOMAIN = "https://roblox-tracker-discord-bot.onrender.com/"

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
    app.run(host="0.0.0.0", port=10000)

# === CONFIGURATION ===
DISCORD_ROLE_ID = 1539998360046407801
# IMPORTANT: Put the ID of the channel where you want the bot to send messages here!
TARGET_CHANNEL_ID = 1301548308610940970

TRACKED_USERS = {
    6054221747: {"place_id": 110823256031006, "faction": "The Lapis Fleet"},
    3655587119: {"place_id": 110823256031006, "faction": "The Crimson Alliance"},
    1304868946: {"place_id": 110823256031006, "faction": "The Crimson Alliance"},
    8309322015: {"place_id": 110823256031006, "faction": "The Lapis Fleet"},
    4977310930: {"place_id": 110823256031006, "faction": "The Lapis Fleet"},
}

already_playing_state = {user_id: False for user_id in TRACKED_USERS}
username_cache = {}

# === DISCORD BOT ===
class RobloxTrackerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)

    async def setup_hook(self):
        # Start the tracking loop once the bot is ready
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

    @tasks.loop(seconds=60)
    async def monitor_loop(self):
        channel = self.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            print("Could not find the target channel. Check TARGET_CHANNEL_ID.")
            return

        async with aiohttp.ClientSession() as session:
            try:
                for user_id, user_data in TRACKED_USERS.items():
                    target_place_id = user_data["place_id"]
                    faction = user_data["faction"]
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
                                    if not already_playing_state[user_id]:
                                        if game_instance_id:
                                            click_to_join = f"{SERVER_DOMAIN}/join?placeId={target_place_id}&gameInstanceId={game_instance_id}"
                                        else:
                                            click_to_join = f"https://www.roblox.com/games/{target_place_id}"
                                        
                                        # Create the Discord Embed
                                        embed = discord.Embed(
                                            title="🎮 Join Server",
                                            description=f"**Player:** {username}\n**Faction:** {faction}\n**Place ID:** `{target_place_id}`",
                                            color=5814783
                                        )
                                        embed.add_field(name="Direct Join", value=f"[👉 Click Here to Join Game]({click_to_join})")
                                        
                                        # Send the message
                                        message = await channel.send(
                                            content=f"<@&{DISCORD_ROLE_ID}>! Targeted player **{username}** of **{faction}** is now active in Pirate Mayhem!",
                                            embed=embed
                                        )
                                        
                                        # Auto-publish (Crosspost) natively!
                                        if channel.is_news():
                                            await message.publish()
                                            print(f"[SUCCESS] Auto-published alert for {username}!")
                                            
                                        already_playing_state[user_id] = True
                                else:
                                    if already_playing_state[user_id]:
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

if __name__ == "__main__":
    # Start Flask Server
    Thread(target=run_web_server, daemon=True).start()
    
    # Start Discord Bot
    client = RobloxTrackerBot()
    # ⚠️ PASTE YOUR BRAND NEW BOT TOKEN BELOW ⚠️
    client.run("MTUzOTg0MjEyNjAzOTM1OTQ4OA.GMQa_G.kBexGt4556mlJLQh2Y9P6GrLEOA4Kp2k4oVebs")
