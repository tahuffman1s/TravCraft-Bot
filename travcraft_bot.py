import discord
from discord.ext import commands, tasks
import aiohttp
import json
import os
import asyncio
from datetime import datetime
import re
from typing import Optional

# Configuration file path
CONFIG_FILE = 'bot_config.json'

# Default configuration
DEFAULT_CONFIG = {
    "github": {
        "owner": "YOUR_GITHUB_USERNAME",
        "repo": "YOUR_REPO_NAME",
        "check_interval": 300  # Check every 5 minutes
    },
    "channels": {
        "release_announcements": None,  # Channel ID for release announcements
        "minecraft_chat": None,         # Channel ID for Minecraft chat bridge
        "modlist": None,                # Channel ID for modlist updates
        "readme": None                   # Channel ID for README/instructions
    },
    "messages": {
        "release_announcement_id": None,  # Message ID to edit for releases
        "modlist_message_id": None,       # Message ID to edit for modlist
        "readme_message_id": None          # Message ID to edit for README
    },
    "last_release_tag": None,
    "bot_prefix": "!"
}

class TravCraftBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        
        super().__init__(
            command_prefix=self.get_prefix, 
            intents=intents,
            help_command=None  # Disable default help command
        )
        
        self.config = self.load_config()
        self.session = None 

    def get_prefix(self, bot, message):
        return self.config.get('bot_prefix', '!')
    
    def load_config(self):
        """Load configuration from file or create default"""
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        else:
            self.save_config(DEFAULT_CONFIG)
            return DEFAULT_CONFIG.copy()
    
    def save_config(self, config=None):
        """Save configuration to file"""
        if config is None:
            config = self.config
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
    
    async def setup_hook(self):
        """Initialize bot components"""
        self.session = aiohttp.ClientSession()
        # Start the background tasks
        self.check_github_releases.start()
        self.update_readme.start()
    
    async def on_ready(self):
        print(f'Bot logged in as {self.user}')
        print(f'Connected to {len(self.guilds)} guilds')
        print(f'Prefix: {self.config.get("bot_prefix", "!")}')
        
        # Set bot status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="TravCraft releases"
            )
        )
        
        # Initial updates
        await self.check_and_update_all()
    
    async def close(self):
        """Cleanup on bot shutdown"""
        if self.session:
            await self.session.close()
        await super().close()
    
    async def check_and_update_all(self):
        """Run all update checks"""
        try:
            await self.check_github_releases()
            await self.update_modlist()
            await self.update_readme()
        except Exception as e:
            print(f"Error during initial updates: {e}")
    
    @tasks.loop(seconds=300)  # Check every 5 minutes by default
    async def check_github_releases(self):
        """Check for new GitHub releases"""
        if not self.session:
            return
            
        try:
            owner = self.config['github']['owner']
            repo = self.config['github']['repo']
            
            # Get latest release from GitHub
            url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
            headers = {'Accept': 'application/vnd.github.v3+json'}
            
            # Add token if available (increases rate limit)
            github_token = os.getenv('GITHUB_TOKEN')
            if github_token:
                headers['Authorization'] = f'token {github_token}'
            
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    print(f"GitHub API error: {resp.status}")
                    return
                    
                release_data = await resp.json()
            
            current_tag = release_data.get('tag_name')
            
            # Check if this is a new release
            if current_tag and current_tag != self.config.get('last_release_tag'):
                await self.announce_new_release(release_data)
                await self.update_modlist()  # Update modlist when new release
                
                # Save the new tag
                self.config['last_release_tag'] = current_tag
                self.save_config()
                
        except Exception as e:
            print(f"Error checking GitHub releases: {e}")
    
    @check_github_releases.before_loop
    async def before_check_github(self):
        """Wait until bot is ready before starting the loop"""
        await self.wait_until_ready()
    
    async def announce_new_release(self, release_data):
        """Announce a new release to configured channels"""
        tag = release_data.get('tag_name', 'Unknown')
        name = release_data.get('name', 'New Release')
        body = release_data.get('body', 'No description available')
        url = release_data.get('html_url', '')
        created = release_data.get('created_at', '')
        
        # Parse version number
        version = tag.replace('v', '') if tag else 'Unknown'
        
        # Get download URL for the stable version
        download_url = f"https://github.com/{self.config['github']['owner']}/{self.config['github']['repo']}/releases/latest/download/travcraft-latest.zip"
        
        # Create release embed
        embed = discord.Embed(
            title=f"🎉 {name}",
            description=f"A new version of TravCraft Client has been released!",
            color=discord.Color.green(),
            url=url,
            timestamp=datetime.fromisoformat(created.replace('Z', '+00:00')) if created else None
        )
        
        embed.add_field(name="Version", value=f"`{version}`", inline=True)
        embed.add_field(name="Download", value=f"[**Click Here**]({download_url})", inline=True)
        
        # Parse and add changelog
        changelog = self.parse_changelog(body)
        if changelog:
            embed.add_field(name="📋 Changelog", value=changelog[:1024], inline=False)
        
        embed.set_footer(text="TravCraft Client")
        
        # Send to release announcements channel
        if self.config['channels']['release_announcements']:
            channel = self.get_channel(self.config['channels']['release_announcements'])
            if channel:
                try:
                    # Check if we should edit existing message or send new one
                    if self.config['messages'].get('release_announcement_id'):
                        try:
                            msg = await channel.fetch_message(self.config['messages']['release_announcement_id'])
                            await msg.edit(embed=embed)
                        except:
                            # Message not found, send new one
                            msg = await channel.send(embed=embed)
                            self.config['messages']['release_announcement_id'] = msg.id
                            self.save_config()
                    else:
                        msg = await channel.send(embed=embed)
                        self.config['messages']['release_announcement_id'] = msg.id
                        self.save_config()
                except Exception as e:
                    print(f"Error sending release announcement: {e}")
        
        # Send simple message to Minecraft chat channel
        if self.config['channels']['minecraft_chat']:
            channel = self.get_channel(self.config['channels']['minecraft_chat'])
            if channel:
                try:
                    # Simple text for Minecraft chat bridge
                    minecraft_msg = f"📦 New TravCraft Client version {version} is now available! Download at: {download_url}"
                    await channel.send(minecraft_msg)
                except Exception as e:
                    print(f"Error sending to Minecraft chat: {e}")
    
    def parse_changelog(self, body):
        """Parse the changelog from release body"""
        if not body:
            return None
        
        # Extract the changes section
        lines = body.split('\n')
        changelog_lines = []
        in_changes = False
        
        for line in lines:
            if '### Added Mods' in line or '### Removed Mods' in line:
                in_changes = True
            elif '### Statistics' in line:
                in_changes = False
            elif in_changes and line.strip():
                # Clean up the line for Discord
                line = line.replace('✅', '➕').replace('❌', '➖')
                changelog_lines.append(line)
        
        return '\n'.join(changelog_lines[:10]) if changelog_lines else None  # Limit to 10 lines
    
    async def update_modlist(self):
        """Update the modlist channel with latest modlist.md"""
        if not self.session or not self.config['channels']['modlist']:
            return
        
        try:
            owner = self.config['github']['owner']
            repo = self.config['github']['repo']
            
            # Get modlist.md from GitHub
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/modlist.md"
            
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    print(f"Failed to fetch modlist.md: {resp.status}")
                    return
                
                content = await resp.text()
            
            # Split content into chunks if needed (Discord has 2000 char limit)
            chunks = self.split_content(content, 1900)
            
            channel = self.get_channel(self.config['channels']['modlist'])
            if not channel:
                return
            
            # Send or update modlist
            if self.config['messages'].get('modlist_message_id'):
                try:
                    # Try to edit existing message
                    msg = await channel.fetch_message(self.config['messages']['modlist_message_id'])
                    await msg.edit(content=f"```markdown\n{chunks[0]}\n```" if chunks else "Modlist is empty")
                    
                    # Delete and resend additional chunks if content is longer
                    if len(chunks) > 1:
                        await msg.delete()
                        for chunk in chunks:
                            await channel.send(f"```markdown\n{chunk}\n```")
                except:
                    # Message not found, send new ones
                    for chunk in chunks:
                        msg = await channel.send(f"```markdown\n{chunk}\n```")
                        if not self.config['messages'].get('modlist_message_id'):
                            self.config['messages']['modlist_message_id'] = msg.id
                            self.save_config()
            else:
                # Send new messages
                for i, chunk in enumerate(chunks):
                    msg = await channel.send(f"```markdown\n{chunk}\n```")
                    if i == 0:
                        self.config['messages']['modlist_message_id'] = msg.id
                        self.save_config()
                        
        except Exception as e:
            print(f"Error updating modlist: {e}")
    
    @tasks.loop(hours=1)  # Update README every hour
    async def update_readme(self):
        """Update the README channel with latest README.md"""
        if not self.session or not self.config['channels']['readme']:
            return
        
        try:
            owner = self.config['github']['owner']
            repo = self.config['github']['repo']
            
            # Get README.md from GitHub
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md"
            
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    print(f"Failed to fetch README.md: {resp.status}")
                    return
                
                content = await resp.text()
            
            # Convert markdown to Discord-friendly format
            content = self.markdown_to_discord(content)
            
            # Split content into chunks
            chunks = self.split_content(content, 1900)
            
            channel = self.get_channel(self.config['channels']['readme'])
            if not channel:
                return
            
            # Clear channel and send new content
            try:
                # Delete old messages
                async for message in channel.history(limit=10):
                    if message.author == self.user:
                        await message.delete()
                        await asyncio.sleep(0.5)  # Rate limit protection
                
                # Send new content
                for chunk in chunks:
                    await channel.send(chunk)
                    await asyncio.sleep(0.5)
                    
            except Exception as e:
                print(f"Error updating README channel: {e}")
                
        except Exception as e:
            print(f"Error updating README: {e}")
    
    @update_readme.before_loop
    async def before_update_readme(self):
        """Wait until bot is ready before starting the loop"""
        await self.wait_until_ready()
    
    def markdown_to_discord(self, content):
        """Convert GitHub markdown to Discord format"""
        # Convert headers
        content = re.sub(r'^### (.*)', r'**\1**', content, flags=re.MULTILINE)
        content = re.sub(r'^## (.*)', r'__**\1**__', content, flags=re.MULTILINE)
        content = re.sub(r'^# (.*)', r'__**\1**__', content, flags=re.MULTILINE)
        
        # Convert code blocks
        content = re.sub(r'```(\w+)\n', r'```\1\n', content)
        
        # Limit consecutive newlines
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        return content
    
    def split_content(self, content, max_length):
        """Split content into chunks for Discord"""
        if len(content) <= max_length:
            return [content]
        
        chunks = []
        lines = content.split('\n')
        current_chunk = ""
        
        for line in lines:
            if len(current_chunk) + len(line) + 1 <= max_length:
                current_chunk += line + '\n'
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = line + '\n'
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks

# Bot commands
bot = TravCraftBot()

@bot.command(name='setchannel')
@commands.has_permissions(administrator=True)
async def set_channel(ctx, channel_type: str, channel: discord.TextChannel = None):
    """Set a channel for bot updates
    
    Usage: !setchannel <type> [#channel]
    Types: releases, minecraft, modlist, readme
    
    If no channel is provided, uses the current channel.
    """
    channel = channel or ctx.channel
    
    channel_map = {
        'releases': 'release_announcements',
        'release': 'release_announcements',
        'minecraft': 'minecraft_chat',
        'mc': 'minecraft_chat',
        'modlist': 'modlist',
        'mods': 'modlist',
        'readme': 'readme',
        'instructions': 'readme'
    }
    
    if channel_type.lower() not in channel_map:
        await ctx.send(f"❌ Invalid channel type. Use one of: releases, minecraft, modlist, readme")
        return
    
    config_key = channel_map[channel_type.lower()]
    bot.config['channels'][config_key] = channel.id
    bot.save_config()
    
    await ctx.send(f"✅ Set {config_key.replace('_', ' ')} channel to {channel.mention}")

@bot.command(name='setrepo')
@commands.has_permissions(administrator=True)
async def set_repo(ctx, owner: str, repo: str):
    """Set the GitHub repository to monitor
    
    Usage: !setrepo <owner> <repo>
    Example: !setrepo YourUsername TravCraft-Client
    """
    bot.config['github']['owner'] = owner
    bot.config['github']['repo'] = repo
    bot.save_config()
    
    await ctx.send(f"✅ Now monitoring repository: {owner}/{repo}")

@bot.command(name='setprefix')
@commands.has_permissions(administrator=True)
async def set_prefix(ctx, prefix: str):
    """Change the bot's command prefix
    
    Usage: !setprefix <new_prefix>
    Example: !setprefix $
    """
    bot.config['bot_prefix'] = prefix
    bot.save_config()
    
    await ctx.send(f"✅ Command prefix changed to: {prefix}")

@bot.command(name='config')
@commands.has_permissions(administrator=True)
async def show_config(ctx):
    """Show current bot configuration"""
    embed = discord.Embed(
        title="Bot Configuration",
        color=discord.Color.blue()
    )
    
    # GitHub info
    embed.add_field(
        name="GitHub Repository",
        value=f"{bot.config['github']['owner']}/{bot.config['github']['repo']}",
        inline=False
    )
    
    # Channels
    channels_text = []
    for key, channel_id in bot.config['channels'].items():
        if channel_id:
            channel = bot.get_channel(channel_id)
            channels_text.append(f"{key}: {channel.mention if channel else 'Not found'}")
        else:
            channels_text.append(f"{key}: Not set")
    
    embed.add_field(
        name="Channels",
        value='\n'.join(channels_text) or "No channels configured",
        inline=False
    )
    
    # Other info
    embed.add_field(
        name="Command Prefix",
        value=f"`{bot.config.get('bot_prefix', '!')}`",
        inline=True
    )
    
    embed.add_field(
        name="Last Release",
        value=bot.config.get('last_release_tag', 'None'),
        inline=True
    )
    
    await ctx.send(embed=embed)

@bot.command(name='forceupdate')
@commands.has_permissions(administrator=True)
async def force_update(ctx):
    """Force an immediate update check"""
    await ctx.send("🔄 Checking for updates...")
    
    try:
        await bot.check_and_update_all()
        await ctx.send("✅ Update check complete!")
    except Exception as e:
        await ctx.send(f"❌ Error during update: {e}")

@bot.command(name='help')
async def help_command(ctx):
    """Show help for bot commands"""
    embed = discord.Embed(
        title="TravCraft Bot Commands",
        description="Bot for monitoring TravCraft Client releases",
        color=discord.Color.blue()
    )
    
    admin_commands = [
        ("setchannel", "Set a channel for updates", "!setchannel releases #announcements"),
        ("setrepo", "Set GitHub repository", "!setrepo owner repo"),
        ("setprefix", "Change command prefix", "!setprefix $"),
        ("config", "Show current configuration", "!config"),
        ("forceupdate", "Force immediate update check", "!forceupdate")
    ]
    
    for name, desc, example in admin_commands:
        embed.add_field(
            name=f"{bot.config.get('bot_prefix', '!')}{name}",
            value=f"{desc}\n`{example}`",
            inline=False
        )
    
    embed.set_footer(text="Administrator permissions required for all commands")
    
    await ctx.send(embed=embed)

# Run the bot
if __name__ == "__main__":
    # Get bot token from environment variable
    token = os.getenv('DISCORD_BOT_TOKEN')
    
    if not token:
        print("Error: DISCORD_BOT_TOKEN environment variable not set!")
        print("Set it using: export DISCORD_BOT_TOKEN='your-bot-token-here'")
        exit(1)
    
    # Run the bot
    bot.run(token)
