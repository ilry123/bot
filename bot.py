import discord
from discord import app_commands
from discord.ext import commands
import datetime
import time
import json
import os

# ==================== ⚙️ 基本設定 ====================

OWNER_ID = 1391357873866408017

WHITELIST_FILE = "whitelist.json"
BLACKLIST_FILE = "blacklist.json"

def load_list(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return list(default)

def save_list(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[持久化錯誤] 無法儲存 {filename}: {e}")

WHITELIST = load_list(WHITELIST_FILE, [OWNER_ID])
BLACKLIST = load_list(BLACKLIST_FILE, [])

if OWNER_ID not in WHITELIST:
    WHITELIST.append(OWNER_ID)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

_synced = False

# ==================== 🛡️ 工具函式 ====================

def is_verified_bot(user):
    try:
        return user.bot and user.public_flags.verified_bot
    except (AttributeError, Exception):
        return False

def truncate(text, max_len=1024):
    if text and len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text or "（無內容）"

# ==================== 🛡️ [備份功能模組] ====================

class BackupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="backup")
    @commands.has_permissions(administrator=True)
    async def backup_config(self, ctx):
        guild = ctx.guild

        roles_data = []
        for role in guild.roles:
            if role.is_default():
                continue
            roles_data.append({
                "name": role.name,
                "permissions": role.permissions.value,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "position": role.position
            })

        channels_data = []
        for channel in guild.channels:
            overwrites_data = []
            for target, perms in channel.overwrites.items():
                overwrites_data.append({
                    "target_id": target.id,
                    "target_type": "role" if isinstance(target, discord.Role) else "member",
                    "permissions": perms.value
                })

            if isinstance(channel, discord.TextChannel):
                channels_data.append({
                    "name": channel.name,
                    "type": "text",
                    "position": channel.position,
                    "topic": channel.topic,
                    "category_id": channel.category_id,
                    "nsfw": channel.nsfw,
                    "slowmode_delay": channel.slowmode_delay,
                    "overwrites": overwrites_data
                })
            elif isinstance(channel, discord.VoiceChannel):
                channels_data.append({
                    "name": channel.name,
                    "type": "voice",
                    "position": channel.position,
                    "category_id": channel.category_id,
                    "bitrate": channel.bitrate,
                    "user_limit": channel.user_limit,
                    "overwrites": overwrites_data
                })
            elif isinstance(channel, discord.CategoryChannel):
                channels_data.append({
                    "name": channel.name,
                    "type": "category",
                    "position": channel.position,
                    "overwrites": overwrites_data
                })

        backup_data = {
            "guild_name": guild.name,
            "roles": roles_data,
            "channels": channels_data,
            "backup_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        filename = f"backup_{guild.id}.json"
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=4)
            await ctx.send(f"✅ **配置備份完成！** 檔案已儲存為 `{filename}`")
        except Exception as e:
            await ctx.send(f"❌ 備份失敗：{e}")

# ==================== 🚫 黑名單全域攔截機制 ====================

@bot.check
async def check_blacklist(ctx):
    if is_verified_bot(ctx.author):
        return True
    if ctx.author.id in BLACKLIST:
        await ctx.send(f"❌ {ctx.author.mention} 你已被列入機器人黑名單，無法使用任何指令！")
        return False
    return True

@bot.tree.interaction_check
async def check_slash_blacklist(interaction: discord.Interaction) -> bool:
    if is_verified_bot(interaction.user):
        return True
    if interaction.user.id in BLACKLIST:
        await interaction.response.send_message("❌ 你已被列入機器人黑名單，無法使用任何指令！", ephemeral=True)
        return False
    return True

# ==================== 🛡️ 高級防 Nuke 系統邏輯 ====================

antispam_enabled = True
spam_tracker = {}
deleted_messages = {}

nuke_tracker = {
    "channel_delete": {},
    "channel_create": {},
    "role_delete": {},
    "role_create": {},
    "ban": {},
    "member_kick": {},
    "guild_update": {}
}

TIME_WINDOW = 5
MAX_ALLOWED_ACTIONS = 3

_cleanup_counter = 0
_punished_users = {}

def is_nuke_attempt(action_type, guild_id, user_id):
    global _cleanup_counter

    if user_id in WHITELIST:
        return False
    if bot.user and user_id == bot.user.id:
        return False

    now = time.time()
    key = (guild_id, user_id)

    existing = nuke_tracker[action_type].get(key, [])
    fresh = [t for t in existing if now - t < TIME_WINDOW]
    fresh.append(now)
    nuke_tracker[action_type][key] = fresh

    _cleanup_counter += 1
    if _cleanup_counter >= 100:
        _cleanup_counter = 0
        for at in nuke_tracker:
            expired = [k for k, v in nuke_tracker[at].items() if all(now - t >= TIME_WINDOW for t in v)]
            for k in expired:
                del nuke_tracker[at][k]

    return len(fresh) >= MAX_ALLOWED_ACTIONS

async def execute_anti_nuke(guild, executor, reason):
    global _punished_users

    if executor.id == bot.user.id or executor.id in WHITELIST:
        return

    punish_key = (guild.id, executor.id)
    now = time.time()

    _punished_users = {k: v for k, v in _punished_users.items() if now - v < 30}

    if punish_key in _punished_users:
        return
    _punished_users[punish_key] = now

    try:
        member = guild.get_member(executor.id)
        if member:
            try:
                roles_to_remove = [r for r in member.roles if r != guild.default_role and not r.managed]
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason=f"[防 Nuke] 移除權限")
            except discord.Forbidden:
                print(f"[Anti-Nuke] 無法移除 {executor.name} 的身分組（權限不足）")
            except Exception as e:
                print(f"[Anti-Nuke] 移除身分組時發生錯誤: {e}")

        try:
            await guild.ban(executor, reason=f"[高級防 Nuke 觸發] {reason}")
        except discord.Forbidden:
            try:
                await guild.kick(executor, reason=f"[高級防 Nuke 觸發] {reason}")
            except discord.Forbidden:
                print(f"[Anti-Nuke] 無法封鎖或踢出 {executor.name}（權限不足）")
        except Exception as e:
            print(f"[Anti-Nuke] 封鎖時發生錯誤: {e}")

        embed = discord.Embed(
            title="🚨🚨 偵測到 Nuke 攻擊並已成功攔截！ 🚨🚨",
            description=f"**違規者**：{executor.mention} (`{executor.id}`)\n**觸發原因**：{reason}\n**處理結果**：⚡ **已將該目標封鎖並移除權限！**",
            color=discord.Color.red(),
            timestamp=datetime.datetime.now()
        )

        target_channel = guild.system_channel or next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
        )
        if target_channel:
            try:
                await target_channel.send(content="🚨 伺服器受到攻擊警報！", embed=embed)
            except discord.Forbidden:
                print(f"[Anti-Nuke] 無法在 {target_channel.name} 發送警報（無發言權限）")
            except Exception as e:
                print(f"[Anti-Nuke] 發送警報時發生錯誤: {e}")

        print(f"[Anti-Nuke 成功打擊] 已封鎖破壞者 {executor.name} ({executor.id}) - 原因：{reason}")

    except Exception as e:
        print(f"[Anti-Nuke 錯誤] {e}")

# 生成 Help Embed 訊息卡片
def build_help_embed():
    status_text = "🟢 已開啟" if antispam_enabled else "🔴 已關閉"

    embed = discord.Embed(
        title="🤖 機器人指令與安全防護清單",
        description="本伺服器已配備【高級防 Nuke】與全自動防刷屏安全機制。",
        color=discord.Color.blue()
    )

    embed.add_field(name="🛡️ 高級防爆破與安全機制", value=(
        f"• **防刷屏保護**：`{status_text}` (連續 5 次重複訊息禁言 1 分鐘)\n"
        "• **防 Nuke 爆破**：`🔴 最高戒備中`\n"
        "  └ 5秒內刪/建頻道、刪/建身分組、Ban/Kick人、修改伺服器設定超過 3 次，**秒封鎖破壞者**！\n"
        "• **白名單保護**：白名單成員不受防炸機制限制。\n"
        "• **已驗證機器人 (勾勾)**：可使用所有指令，但炸群行為同樣會被攔截。"
    ), inline=False)

    embed.add_field(name="🌐 一般功能 (所有人/支援私訊)", value=(
        "`!ping` - 測試機器人連線延遲\n"
        "`!avatar [@使用者 或 ID]` - 查看指定使用者的頭像大圖\n"
        "`!67` - 回應 67\n"
        "`!這波我挺瑋瑋` - 瑋瑋應援訊息\n"
        "`!皮言` - 閱讀皮言的故事\n"
        "`!tungtung` - 自動在同一則訊息發送指定群組連結並重複 3 次\n"
        "`/爆炸` - 自訂訊息與按鈕刷屏工具\n"
        "`!backup` - 備份伺服器配置 (管理員)"
    ), inline=False)

    embed.add_field(name="🛠️ 管理員功能 (需管理員權限)", value=(
        "`!status` - 查看伺服器防炸狀態與系統資訊\n"
        "`!關閉外部` - 禁止本頻道使用外部應用程式 (防無權限爆破)\n"
        "`!開啟外部` - 允許本頻道使用外部應用程式\n"
        "`!whitelist [add/remove/list] [ID]` - 管理白名單成員 (擁有者限定)\n"
        "`!blacklist [add/remove/list] [ID]` - 管理黑名單成員 (擁有者限定)\n"
        "`!addrole @使用者 @身分組` - 給予使用者身分組\n"
        "`!removerole @使用者 @身分組` - 移除使用者的身分組\n"
        "`!deleted` - 查看本頻道最近一條被刪除的訊息\n"
        "`!antispam` - 切換防刷屏開關 (開啟 / 關閉)\n"
        "`!kick @使用者 [原因]` - 踢出使用者\n"
        "`!ban @使用者 [原因]` - 封鎖使用者\n"
        "`!unban [使用者ID 或 名稱]` - 解除封鎖\n"
        "`!timeout @使用者 [時間] [原因]` - 手動禁言使用者\n"
        "`!untimeout @使用者` - 解除禁言\n"
        "`!clear [數量]` - 批次清理訊息"
    ), inline=False)

    return embed

# ==================== 💬 新增的應援與故事指令 ====================

@bot.command(name="這波我挺瑋瑋")
async def support_weiwei(ctx):
    message = (
        "這波我挺瑋瑋，雖然瑋瑋開會睡覺也有不對的地方,可是哲哲摔獎牌真的讓人很傷心,"
        "但隨著頻道的成長,不完美的地方也一步一步改善,相信大家都很喜歡黃氏兄弟,"
        "這件事已經過滿久了,大家要心平氣和的討論,不能只用一件事去判斷他是怎樣的人喔!\n\n"
        "那些說很娘的人,他們比其他人還要努力, 而且他們影片,不比其他百萬YouTuber來的差,"
        "他們的每個企劃,做的都很棒.....…而且為什麼會哭?這代表說,他珍惜他們兄弟倆一起得到的獎勵,"
        "再說了,那塊百萬獎牌是他們事前先去訂做的,你們可以去迎新檢查！"
    )
    await ctx.send(message)

@bot.command(name="皮言")
async def pi_yan_story(ctx):
    story = (
        "皮言是一個極其普通的人。他住在一條沒什麼特別的巷子裡，養了一隻只吃塑膠袋的貓，喜歡在凌晨兩點打開冰箱，看著燈光思考人生。\n\n"
        "但有一天，皮言醒來後發現了一件不得了的事——他的影子，變長了三十公分。\n"
        "不是錯覺，也不是光線的問題。那影子就像自己長高了一截，獨立於牆上，甚至在他沒動的時候，影子還會微微晃動，像是在偷笑。皮言嚇壞了，立刻去量尺。「真的三十公分……」他喃喃說道，「難道是我昨晚的泡麵多放了根香腸？」\n\n"
        "從那天起，一切變得不一樣了。他的影子似乎有了意識。早上他去上班，影子卻故意走慢；晚上他要睡覺，影子卻在床邊比出奇怪的姿勢。有時他半夜醒來，會看到影子在天花板上「倒著坐著」，像在思考宇宙的起源。\n\n"
        "同事們開始注意到皮言的異樣。「你最近是不是長高了？」「沒有。」「那為什麼你的影子比你還長？」皮言只好苦笑。沒人會相信他影子自己在發育。\n\n"
        "他試著用各種方法讓影子恢復原狀：拿手電筒照、拿電風扇吹，甚至請鄰居的貓去咬。結果那貓一靠近，影子竟伸出一條黑色的線，輕輕把貓推開。皮言愣住了，那不是普通的影子——那是一個活著的東西。\n\n"
        "有一天，他鼓起勇氣對影子說話。「你到底想要什麼？」影子沒有回答，只是慢慢伸長，又長了三十公分。那畫面就像黑暗在呼吸。\n\n"
        "後來，皮言的影子變得越來越長，直到有一天，整個房間都被影子佔滿。他再也分不清哪裡是自己，哪裡是影子。鄰居只記得，那晚屋裡的燈忽然全滅，隔天早上，門口多了一條長長的黑線，一直延伸到遠方。\n\n"
        "警方來調查，只找到一張紙條，上面寫著：「我只是想多一點空間呼吸。」——皮言\n\n"
        "沒有人明白這句話的意思。有人說皮言被影子吞噬了，也有人說他成為了影子的形狀。但從那天起，只要月亮特別亮、夜裡特別靜的時候，巷口的牆上總會出現一條比任何人都長三十公分的影子。它不動，也不說話，只靜靜地貼在那裡，彷彿在等著誰回來。"
    )
    await ctx.send(story)

# ==================== 📜 進階 Log 記錄系統 ====================

class LogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_log_channel(self, guild):
        channel = discord.utils.get(guild.text_channels, name="logs")
        if not channel:
            try:
                channel = await guild.create_text_channel(name="logs", reason="Log 記錄系統自動建立")
            except discord.Forbidden:
                print(f"[Log] 無法在 {guild.name} 建立 logs 頻道（權限不足）")
                return None
            except discord.HTTPException as e:
                print(f"[Log] 建立 logs 頻道失敗: {e}")
                return None
        return channel

    @commands.Cog.listener()
    async def on_member_join(self, member):
        channel = await self.get_log_channel(member.guild)
        if channel:
            embed = discord.Embed(title="📥 成員加入", color=discord.Color.green())
            embed.add_field(name="成員", value=f"{member.mention} ({member.name})", inline=False)
            embed.set_footer(text=f"ID: {member.id}")
            await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        channel = await self.get_log_channel(member.guild)
        if channel:
            embed = discord.Embed(title="📤 成員離開", color=discord.Color.red())
            embed.add_field(name="成員", value=f"{member.name}", inline=False)
            embed.set_footer(text=f"ID: {member.id}")
            await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.nick != after.nick:
            channel = await self.get_log_channel(after.guild)
            if channel:
                embed = discord.Embed(title="✏️ 暱稱變更", color=discord.Color.blue())
                embed.add_field(name="使用者", value=f"{after.mention}", inline=False)
                embed.add_field(name="原暱稱", value=f"{before.nick or '無'}", inline=True)
                embed.add_field(name="新暱稱", value=f"{after.nick or '無'}", inline=True)
                await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author.bot or before.content == after.content:
            return
        channel = await self.get_log_channel(after.guild)
        if channel:
            embed = discord.Embed(title="📝 訊息已編輯", color=discord.Color.orange())
            embed.add_field(name="發送者", value=f"{after.author.mention}", inline=False)
            embed.add_field(name="修改前", value=truncate(before.content, 1024), inline=False)
            embed.add_field(name="修改後", value=truncate(after.content, 1024), inline=False)
            await channel.send(embed=embed)

# ==================== 🚨 防 Nuke 與事件監控 ====================

@bot.event
async def on_ready():
    global _synced
    try:
        if not bot.get_cog("NukeCog"):
            await bot.add_cog(NukeCog(bot))
        if not bot.get_cog("LogCog"):
            await bot.add_cog(LogCog(bot))
        if not bot.get_cog("BackupCog"):
            await bot.add_cog(BackupCog(bot))
        if not _synced:
            synced = await bot.tree.sync()
            _synced = True
            print(f'✅ 已成功同步 {len(synced)} 個斜線指令！')
    except Exception as e:
        print(f'❌ 載入/同步斜線指令失敗: {e}')

    print(f'🤖 機器人 {bot.user} 已成功上線！高級防 Nuke 系統【最高戒備】運作中。')

@bot.event
async def on_guild_join(guild):
    try:
        created_channel = await guild.create_text_channel(
            name="🤖-機器人指令說明",
            reason="[自動設定] 機器人加入伺服器自動建立指令介紹頻道"
        )
        embed = build_help_embed()
        await created_channel.send(content="👋 **感謝將機器人邀請至本伺服器！以下為完整的指令與功能說明：**", embed=embed)
    except discord.Forbidden:
        print(f"[on_guild_join] 無法在 {guild.name} 建立頻道（權限不足）")
    except Exception as e:
        print(f"[on_guild_join] 建立頻道或發送說明時發生錯誤: {e}")

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
            executor = entry.user
            if is_nuke_attempt("channel_delete", guild.id, executor.id):
                await execute_anti_nuke(guild, executor, "短時間內大量刪除頻道 (Nuke)")
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Anti-Nuke] on_guild_channel_delete 錯誤: {e}")

@bot.event
async def on_guild_channel_create(channel):
    guild = channel.guild
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
            executor = entry.user
            if is_nuke_attempt("channel_create", guild.id, executor.id):
                await execute_anti_nuke(guild, executor, "短時間內大量新建頻道 (Nuke)")
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Anti-Nuke] on_guild_channel_create 錯誤: {e}")

@bot.event
async def on_guild_role_delete(role):
    guild = role.guild
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
            executor = entry.user
            if is_nuke_attempt("role_delete", guild.id, executor.id):
                await execute_anti_nuke(guild, executor, "短時間內大量刪除身分組 (Nuke)")
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Anti-Nuke] on_guild_role_delete 錯誤: {e}")

@bot.event
async def on_guild_role_create(role):
    guild = role.guild
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_create):
            executor = entry.user
            if is_nuke_attempt("role_create", guild.id, executor.id):
                await execute_anti_nuke(guild, executor, "短時間內大量建立身分組 (Nuke)")
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Anti-Nuke] on_guild_role_create 錯誤: {e}")

@bot.event
async def on_member_ban(guild, user):
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
            executor = entry.user
            if is_nuke_attempt("ban", guild.id, executor.id):
                await execute_anti_nuke(guild, executor, "短時間內大量封鎖成員 (Nuke)")
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Anti-Nuke] on_member_ban 錯誤: {e}")

@bot.event
async def on_member_remove(member):
    guild = member.guild
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.kick):
            if entry.target and entry.target.id == member.id:
                executor = entry.user
                if is_nuke_attempt("member_kick", guild.id, executor.id):
                    await execute_anti_nuke(guild, executor, "短時間內大量踢出成員 (Nuke)")
                break
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Anti-Nuke] on_member_remove 錯誤: {e}")

@bot.event
async def on_guild_update(before, after):
    guild = after
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.guild_update):
            executor = entry.user
            if is_nuke_attempt("guild_update", guild.id, executor.id):
                await execute_anti_nuke(guild, executor, "短時間內大量修改伺服器設定 (Nuke)")
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"[Anti-Nuke] on_guild_update 錯誤: {e}")

# ==================== 💥 斜線指令 (NukeCog - 安全加強版) ====================

class CustomMessageModal(discord.ui.Modal, title="自訂爆炸訊息"):
    message_input = discord.ui.TextInput(
        label="請輸入要洗版的訊息內容",
        style=discord.TextStyle.paragraph,
        placeholder="在此輸入你想發送的文字...",
        required=True,
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        view = BoomView(custom_text=self.message_input.value)
        await interaction.response.send_message(f"已收到訊息！點擊下方按鈕即可刷出 5 則獨立訊息：", view=view, ephemeral=True)

class BoomView(discord.ui.View):
    def __init__(self, custom_text: str):
        super().__init__(timeout=180)
        self.custom_text = custom_text

    @discord.ui.button(label="!!!", style=discord.ButtonStyle.danger)
    async def spam_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in WHITELIST and not is_verified_bot(interaction.user):
            await interaction.response.send_message("你沒有權限使用這個按鈕！", ephemeral=True)
            return

        await interaction.response.send_message("開始刷屏...", ephemeral=True)
        channel = interaction.channel
        if channel:
            try:
                for _ in range(5):
                    await channel.send(self.custom_text)
            except Exception as e:
                await interaction.followup.send(f"❌ 發送失敗：{e}", ephemeral=True)

class NukeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="爆炸", description="自訂訊息與按鈕刷屏工具")
    async def boom(self, interaction: discord.Interaction):
        if interaction.user.id not in WHITELIST and not is_verified_bot(interaction.user):
            await interaction.response.send_message("你沒有權限使用這個指令！", ephemeral=True)
            return
        await interaction.response.send_modal(CustomMessageModal())

# ==================== 💬 一般指令與管理員狀態查看 ====================

@bot.command(name="status", aliases=["狀態", "伺服器狀態"])
@commands.has_permissions(administrator=True)
async def server_status_cmd(ctx):
    guild = ctx.guild
    channel = ctx.channel

    overwrite = channel.overwrites_for(guild.default_role)
    try:
        ext_apps_status = "🔒 已封鎖 (安全)" if overwrite.use_external_apps is False else "🔓 已開啟 (可能遭外部無權限爆破)"
    except AttributeError:
        ext_apps_status = "ℹ️ 此 discord.py 版本不支援外部 App 權限查詢"

    antispam_status = "🟢 運作中 (重複5次禁言)" if antispam_enabled else "🔴 已關閉"

    embed = discord.Embed(
        title=f"🛡️ 【{guild.name}】防護與伺服器狀態儀表板",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="🛡️ 防炸安全狀態", value=(
        f"• **防 Nuke 爆破**：🟢 `最高戒備中` (5秒超過3次刪/建頻道、刪/建身分組、Ban/Kick、改伺服器設定即封鎖)\n"
        f"• **防刷屏機制**：{antispam_status}\n"
        f"• **本頻道外部 App 權限**：{ext_apps_status}"
    ), inline=False)

    embed.add_field(name="📊 系統與名單數據", value=(
        f"• **伺服器總人數**：`{guild.member_count}` 人\n"
        f"• **防爆白名單人數**：`{len(WHITELIST)}` 人\n"
        f"• **機器人黑名單人數**：`{len(BLACKLIST)}` 人\n"
        f"• **機器人連線延遲**：`{round(bot.latency * 1000)} ms`"
    ), inline=False)

    embed.set_footer(text=f"查詢者：{ctx.author.name}", icon_url=ctx.author.display_avatar.url)

    await ctx.send(embed=embed)

@server_status_cmd.error
async def server_status_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 只有**管理者**才能查看伺服器狀態狀態！")

@bot.command(name="tungtung")
async def tungtung_cmd(ctx):
    links = "discord.gg/brainrots discord.gg/daptoper"

    for _ in range(3):
        await ctx.send(links)

@bot.command(name="關閉外部")
@commands.has_permissions(administrator=True)
async def disable_external_cmd(ctx):
    channel = ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ 此指令只能在伺服器的文字頻道使用！")
        return

    try:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.use_external_apps = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"🔒 已成功**關閉**本頻道 ({channel.mention}) 的「使用外部應用程式」權限！其他成員將無法使用外部 Bot/App 進行無權限爆破。")
    except AttributeError:
        await ctx.send("❌ 此 discord.py 版本不支援外部 App 權限設定。")
    except discord.Forbidden:
        await ctx.send("❌ 機器人沒有管理頻道權限！")
    except Exception as e:
        await ctx.send(f"❌ 操作失敗：{e}")

@bot.command(name="開啟外部")
@commands.has_permissions(administrator=True)
async def enable_external_cmd(ctx):
    channel = ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ 此指令只能在伺服器的文字頻道使用！")
        return

    try:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.use_external_apps = True
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"🔓 已成功**開啟**本頻道 ({channel.mention}) 的「使用外部應用程式」權限！")
    except AttributeError:
        await ctx.send("❌ 此 discord.py 版本不支援外部 App 權限設定。")
    except discord.Forbidden:
        await ctx.send("❌ 機器人沒有管理頻道權限！")
    except Exception as e:
        await ctx.send(f"❌ 操作失敗：{e}")

@bot.event
async def on_message_delete(message):
    if message.author.bot or not message.guild:
        return
    deleted_messages[message.channel.id] = {
        "author": message.author,
        "content": message.content if message.content else "(非文字訊息/含有圖片或貼圖)",
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild and antispam_enabled and message.author.id not in WHITELIST:
        user_id = message.author.id
        content = message.content.strip()

        if content:
            now = time.time()
            if len(spam_tracker) > 100:
                for uid in list(spam_tracker.keys()):
                    if now - spam_tracker[uid].get("last_time", 0) > 300:
                        del spam_tracker[uid]

            if user_id not in spam_tracker:
                spam_tracker[user_id] = {"last_msg": content, "count": 1, "last_time": now}
            else:
                if spam_tracker[user_id]["last_msg"] == content:
                    spam_tracker[user_id]["count"] += 1
                else:
                    spam_tracker[user_id] = {"last_msg": content, "count": 1, "last_time": now}
                spam_tracker[user_id]["last_time"] = now

            if spam_tracker[user_id]["count"] >= 5:
                try:
                    spam_tracker[user_id] = {"last_msg": "", "count": 0, "last_time": now}
                    duration = datetime.timedelta(minutes=1)
                    await message.author.timeout(duration, reason="[防刷屏] 發送重複訊息超過 5 次")
                    await message.channel.send(f'⚠️ {message.author.mention} 因短時間內重複發送相同訊息，已被自動禁言 **1 分鐘**！')
                except discord.Forbidden:
                    print(f"[防刷屏] 無法禁言 {message.author.name}（權限不足）")
                except Exception as e:
                    print(f"[防刷屏] 禁言錯誤: {e}")

    await bot.process_commands(message)

@bot.command(name="whitelist", aliases=["白名單"])
async def whitelist_cmd(ctx, action: str = None, user_id: int = None):
    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ 只有機器人擁有者才能管理白名單！")
        return

    if not action or action not in ["add", "remove", "list", "新增", "移除", "列表"]:
        await ctx.send("❌ 用法：\n`!whitelist add [使用者ID]` - 新增\n`!whitelist remove [使用者ID]` - 移除\n`!whitelist list` - 查看清單")
        return

    if action in ["add", "新增"]:
        if not user_id:
            await ctx.send("❌ 請提供要加入白名單的使用者 ID！")
            return
        if user_id not in WHITELIST:
            WHITELIST.append(user_id)
            save_list(WHITELIST_FILE, WHITELIST)
            await ctx.send(f"✅ 已成功將 ID `{user_id}` 加入防炸白名單！")
        else:
            await ctx.send("⚠️ 該 ID 已經在白名單中了！")

    elif action in ["remove", "移除"]:
        if not user_id:
            await ctx.send("❌ 請提供要從白名單移除的使用者 ID！")
            return
        if user_id in WHITELIST:
            if user_id == OWNER_ID:
                await ctx.send("❌ 不能將機器人擁有者從白名單移除！")
                return
            WHITELIST.remove(user_id)
            save_list(WHITELIST_FILE, WHITELIST)
            await ctx.send(f"🗑️ 已成功將 ID `{user_id}` 從白名單移除！")
        else:
            await ctx.send("❌ 該 ID 不在白名單中！")

    elif action in ["list", "列表"]:
        members = "\n".join([f"• `{uid}`" for uid in WHITELIST])
        await ctx.send(f"🛡️ **目前白名單成員 ID 清單：**\n{members}")

@bot.command(name="blacklist", aliases=["黑名單"])
async def blacklist_cmd(ctx, action: str = None, user_id: int = None):
    if ctx.author.id != OWNER_ID:
        return

    if not action or action not in ["add", "remove", "list", "新增", "移除", "列表"]:
        await ctx.send("❌ 用法：\n`!blacklist add [使用者ID]` - 列入黑名單\n`!blacklist remove [使用者ID]` - 解除黑名單\n`!blacklist list` - 查看黑名單清單")
        return

    if action in ["add", "新增"]:
        if not user_id:
            await ctx.send("❌ 請提供要列入黑名單的使用者 ID！")
            return
        if user_id == OWNER_ID:
            await ctx.send("❌ 不能將擁有者列入黑名單！")
            return
        if user_id not in BLACKLIST:
            BLACKLIST.append(user_id)
            save_list(BLACKLIST_FILE, BLACKLIST)
            await ctx.send(f"🚫 已成功將 ID `{user_id}` 列入機器人黑名單！對方將無法使用任何指令。")
        else:
            await ctx.send("⚠️ 該 ID 已經在黑名單中了！")

    elif action in ["remove", "移除"]:
        if not user_id:
            await ctx.send("❌ 請提供要解除黑名單的使用者 ID！")
            return
        if user_id in BLACKLIST:
            BLACKLIST.remove(user_id)
            save_list(BLACKLIST_FILE, BLACKLIST)
            await ctx.send(f"🔓 已成功將 ID `{user_id}` 從黑名單中移除！")
        else:
            await ctx.send("❌ 該 ID 不在黑名單中！")

    elif action in ["list", "列表"]:
        if not BLACKLIST:
            await ctx.send("ℹ️ 目前黑名單為空。")
            return
        members = "\n".join([f"• `{uid}`" for uid in BLACKLIST])
        await ctx.send(f"🚫 **目前黑名單成員 ID 清單：**\n{members}")

@bot.command(name="help", aliases=["說明", "幫助"])
async def help(ctx):
    embed = build_help_embed()
    await ctx.send(embed=embed)

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f'Pong! 延遲：{round(bot.latency * 1000)}ms')

@bot.command(name="67")
async def cmd_67(ctx):
    await ctx.send("67")

@bot.command(name="avatar", aliases=["頭貼", "av"])
async def avatar(ctx, *, user_input: str = None):
    user = None
    if user_input is None:
        user = ctx.author
    else:
        if ctx.message.mentions:
            user = ctx.message.mentions[0]
        elif user_input.isdigit():
            try:
                user = await bot.fetch_user(int(user_input))
            except Exception:
                await ctx.send("❌ 找不到該 ID 對應的使用者！")
                return
        elif ctx.guild:
            user = discord.utils.find(lambda m: m.name == user_input or m.display_name == user_input, ctx.guild.members)
            if user is None:
                await ctx.send("❌ 找不到該名稱的使用者！")
                return
        else:
            await ctx.send("❌ 私訊中請使用使用者 ID 或標記！")
            return

    embed = discord.Embed(title="🖼️ 使用者頭像", description=f"**{user.name}** 的頭貼：", color=discord.Color.blue())
    avatar_url = user.display_avatar.url
    embed.set_image(url=avatar_url)
    embed.add_field(name="原圖連結", value=f"[點我下載高清頭貼]({avatar_url})")
    await ctx.send(embed=embed)

@bot.command(name="addrole", aliases=["給身分組", "giverole"])
@commands.has_permissions(administrator=True)
async def add_role(ctx, member: discord.Member, role: discord.Role):
    if role.managed:
        await ctx.send("❌ 這是整合身分組（如機器人身分組），無法手動給予！")
        return
    if role >= ctx.author.top_role and ctx.author.id != OWNER_ID:
        await ctx.send("❌ 你不能給予與你同階或更高階的身分組！")
        return
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ 機器人的權限低於該身分組，無法給予！")
        return
    try:
        await member.add_roles(role)
        await ctx.send(f'✅ 已成功為 **{member.display_name}** 新增身分組：**{role.name}**！')
    except discord.Forbidden:
        await ctx.send("❌ 給予身分組失敗！請確保機器人的身分組階層高於該身分組。")
    except Exception as e:
        await ctx.send(f"❌ 給予身分組失敗：{e}")

@add_role.error
async def add_role_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="removerole", aliases=["移除身分組", "delrole"])
@commands.has_permissions(administrator=True)
async def remove_role(ctx, member: discord.Member, role: discord.Role):
    if role.managed:
        await ctx.send("❌ 這是整合身分組（如機器人身分組），無法手動移除！")
        return
    if role >= ctx.author.top_role and ctx.author.id != OWNER_ID:
        await ctx.send("❌ 你不能移除與你同階或更高階的身分組！")
        return
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ 機器人的權限低於該身分組，無法移除！")
        return
    try:
        await member.remove_roles(role)
        await ctx.send(f'🗑️ 已成功移除 **{member.display_name}** 的身分組：**{role.name}**！')
    except discord.Forbidden:
        await ctx.send("❌ 移除身分組失敗！請確保機器人的身分組階層高於該身分組。")
    except Exception as e:
        await ctx.send(f"❌ 移除身分組失敗：{e}")

@remove_role.error
async def remove_role_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="kick")
@commands.has_permissions(administrator=True)
async def kick(ctx, member: discord.Member, *, reason: str = "未提供原因"):
    if member.id == ctx.author.id:
        await ctx.send("❌ 你不能踢出自己！")
        return
    if member.id == bot.user.id:
        await ctx.send("❌ 你不能踢出機器人！")
        return
    if member.top_role >= ctx.author.top_role and ctx.author.id != OWNER_ID:
        await ctx.send("❌ 你不能踢出與你同階或更高階的成員！")
        return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("❌ 機器人的權限低於該成員，無法踢出！")
        return
    try:
        await member.kick(reason=reason)
        await ctx.send(f'**{member.display_name}** 已被踢出💔')
    except discord.Forbidden:
        await ctx.send("❌ 機器人權限不足，無法踢出該成員！")
    except Exception as e:
        await ctx.send(f"❌ 踢出失敗：{e}")

@kick.error
async def kick_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="ban")
@commands.has_permissions(administrator=True)
async def ban(ctx, member: discord.Member, *, reason: str = "未提供原因"):
    if member.id == ctx.author.id:
        await ctx.send("❌ 你不能封鎖自己！")
        return
    if member.id == bot.user.id:
        await ctx.send("❌ 你不能封鎖機器人！")
        return
    if member.top_role >= ctx.author.top_role and ctx.author.id != OWNER_ID:
        await ctx.send("❌ 你不能封鎖與你同階或更高階的成員！")
        return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("❌ 機器人的權限低於該成員，無法封鎖！")
        return
    try:
        await member.ban(reason=reason)
        await ctx.send(f'**{member.display_name}** 已被停權💥💥💥')
    except discord.Forbidden:
        await ctx.send("❌ 機器人權限不足，無法封鎖該成員！")
    except Exception as e:
        await ctx.send(f"❌ 封鎖失敗：{e}")

@ban.error
async def ban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="unban")
@commands.has_permissions(administrator=True)
async def unban(ctx, *, user_input: str):
    try:
        banned_users = [entry async for entry in ctx.guild.bans()]
    except Exception:
        await ctx.send("❌ 無法取得封鎖清單！")
        return

    user_to_unban = None
    for ban_entry in banned_users:
        user = ban_entry.user
        if str(user.id) == user_input or user.name == user_input:
            user_to_unban = user
            break

    if user_to_unban:
        try:
            await ctx.guild.unban(user_to_unban)
            await ctx.send(f'🔓已將 **{user_to_unban.name}** 解除停權')
        except discord.Forbidden:
            await ctx.send("❌ 機器人沒有封鎖成員權限！")
        except Exception as e:
            await ctx.send(f"❌ 解除封鎖失敗：{e}")
    else:
        await ctx.send(f'❌ 在封鎖清單中找不到 `{user_input}`！')

@unban.error
async def unban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="timeout")
@commands.has_permissions(administrator=True)
async def timeout(ctx, member: discord.Member, time_str: str = "10m", *, reason: str = "未提供原因"):
    unit = time_str[-1].lower()
    if unit not in ['s', 'm', 'h', 'd'] or not time_str[:-1].isdigit():
        await ctx.send("❌ 時間格式錯誤！請使用如 `30s`, `10m`, `2h`, `1d`")
        return

    num = int(time_str[:-1])
    seconds = num * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]

    if seconds <= 0:
        await ctx.send("❌ 禁言時間必須為正數！")
        return

    max_timeout = 28 * 86400
    if seconds > max_timeout:
        await ctx.send("❌ 禁言時間不能超過 28 天！")
        return

    duration = datetime.timedelta(seconds=seconds)

    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send("❌ 機器人的權限低於該成員，無法禁言！")
        return

    try:
        await member.timeout(duration, reason=reason)
        await ctx.send(f'已將 **{member.display_name}** 禁言 **{time_str}**')
    except discord.Forbidden:
        await ctx.send("❌ 機器人權限不足，無法禁言該成員！")
    except Exception as e:
        await ctx.send(f"❌ 禁言失敗：{e}")

@timeout.error
async def timeout_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="untimeout")
@commands.has_permissions(administrator=True)
async def untimeout(ctx, member: discord.Member):
    try:
        await member.timeout(None)
        await ctx.send(f'已解除 **{member.display_name}** 的禁言')
    except discord.Forbidden:
        await ctx.send("❌ 機器人權限不足，無法解除禁言！")
    except Exception as e:
        await ctx.send(f"❌ 解除禁言失敗：{e}")

@untimeout.error
async def untimeout_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="deleted", aliases=["已刪除", "snipe"])
@commands.has_permissions(administrator=True)
async def view_deleted(ctx):
    channel_id = ctx.channel.id
    if channel_id not in deleted_messages:
        await ctx.send("🔍 本頻道最近沒有任何訊息被刪除的紀錄！")
        return

    data = deleted_messages[channel_id]
    embed = discord.Embed(
        title="🗑️ 最近刪除的訊息紀錄",
        color=discord.Color.red()
    )
    embed.add_field(name="發送者", value=data["author"].mention, inline=True)
    embed.add_field(name="刪除時間", value=data["time"], inline=True)
    embed.add_field(name="訊息內容", value=f"```\n{truncate(data['content'], 1000)}\n```", inline=False)
    embed.set_thumbnail(url=data["author"].display_avatar.url)

    await ctx.send(embed=embed)

@view_deleted.error
async def view_deleted_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

@bot.command(name="antispam", aliases=["防刷屏"])
@commands.has_permissions(administrator=True)
async def toggle_antispam(ctx):
    global antispam_enabled
    antispam_enabled = not antispam_enabled
    if antispam_enabled:
        await ctx.send("🛡️ 防刷屏功能已 **開啟**！")
    else:
        await ctx.send("⚠️ 防刷屏功能已 **關閉**！")

@bot.command(name="clear")
@commands.has_permissions(administrator=True)
async def clear(ctx, amount: int = 5):
    if amount < 1:
        await ctx.send("❌ 數量必須至少為 1！")
        return
    if amount > 1000:
        await ctx.send("❌ 單次最多清理 1000 條訊息！")
        return
    try:
        await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f'🧹 已清理 {amount} 條訊息！', delete_after=3)
    except discord.Forbidden:
        await ctx.send("❌ 機器人沒有管理訊息權限！")
    except Exception as e:
        await ctx.send(f"❌ 清理失敗：{e}")

@clear.error
async def clear_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你需要管理員權限才能使用此指令！")

# ==================== 🔑 啟動 ====================

TOKEN = os.environ['TOKEN']

if __name__ == "__main__":
    bot.run(TOKEN)