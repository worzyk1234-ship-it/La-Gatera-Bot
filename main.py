import asyncio
import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
command_tree = app_commands.CommandTree(bot)
giveaway_group = app_commands.Group(
    name="giveaway",
    description="Crea y administra sorteos",
)
halloween_group = app_commands.Group(
    name="halloween",
    description="Juega al evento Truco o Trato de Halloween",
)
PREFIX = ","
GIVEAWAYS_FILE = Path("data/giveaways.json")
WARNINGS_FILE = Path("data/warnings.json")
HALLOWEEN_FILE = Path("data/halloween.json")
HALLOWEEN_ROLE_NAME = "Halloween Gatera"
HALLOWEEN_REWARD_THRESHOLD = 100
HALLOWEEN_COOLDOWN = timedelta(hours=20)
HALLOWEEN_PERMISSION_MASK = 268435504

giveaways: dict[str, dict[str, Any]] = {}
giveaway_tasks: dict[str, asyncio.Task[None]] = {}
giveaway_lock = asyncio.Lock()
giveaways_loaded = False
warning_records: dict[str, dict[str, list[dict[str, Any]]]] = {}
warnings_lock = asyncio.Lock()
warnings_loaded = False
halloween_data: dict[str, dict[str, Any]] = {}
halloween_lock = asyncio.Lock()
halloween_loaded = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_duration(value: str) -> int:
    """Parse durations such as 30s, 10m, 2h, 1d2h."""
    normalized = value.casefold().replace(" ", "")
    if not re.fullmatch(r"(?:\d+[smhd])+", normalized):
        raise ValueError("Usa formatos como `30s`, `10m`, `2h` o `1d2h`.")

    units = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}
    seconds = sum(
        int(match.group(1)) * units[match.group(2)]
        for match in re.finditer(r"(\d+)([smhd])", normalized)
    )

    if seconds < 10:
        raise ValueError("La duración mínima es de 10 segundos.")
    if seconds > 30 * 86_400:
        raise ValueError("La duración máxima es de 30 días.")
    return seconds


def parse_claim_hours(value: str) -> int:
    normalized = value.casefold().strip()
    match = re.fullmatch(r"(\d+)\s*(?:h|hora|horas)?", normalized)
    if match is None:
        raise ValueError("El límite de reclamación debe ser, por ejemplo, `24h`.")

    hours = int(match.group(1))
    if not 1 <= hours <= 168:
        raise ValueError("El límite de reclamación debe estar entre 1 y 168 horas.")
    return hours


def parse_create_options(raw_value: str) -> tuple[str, str, int]:
    segments = [segment.strip() for segment in raw_value.split("|")]
    prize = segments[0]
    requirements = ""
    claim_hours = 24

    for segment in segments[1:]:
        if ":" not in segment:
            raise ValueError(
                "Las opciones deben escribirse como `requisitos: ...` o `reclamo: 24h`."
            )
        key, value = (part.strip() for part in segment.split(":", maxsplit=1))
        normalized_key = key.casefold()
        if normalized_key in {"requisitos", "requisito", "requirements"}:
            requirements = value
        elif normalized_key in {"reclamo", "reclamar", "claim"}:
            claim_hours = parse_claim_hours(value)
        else:
            raise ValueError(f"No conozco la opción `{key}`.")

    return prize, requirements, claim_hours


def normalize_role_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name).casefold()
    return re.sub(r"[\s_-]+", "", decomposed)


def can_manage_warnings(member: discord.Member) -> bool:
    allowed_roles = {"owner", "coowner", "mod"}
    user_roles = {normalize_role_name(role.name) for role in member.roles}
    return bool(user_roles & allowed_roles)


def save_giveaways() -> None:
    GIVEAWAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = GIVEAWAYS_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(giveaways, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(GIVEAWAYS_FILE)


def load_giveaways() -> None:
    if not GIVEAWAYS_FILE.exists():
        return

    try:
        saved_giveaways = json.loads(GIVEAWAYS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved_giveaways, dict):
            giveaways.update(saved_giveaways)
    except (OSError, json.JSONDecodeError):
        logging.exception("No se pudo cargar data/giveaways.json")


def save_warnings() -> None:
    WARNINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = WARNINGS_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(warning_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(WARNINGS_FILE)


def load_warnings() -> None:
    if not WARNINGS_FILE.exists():
        return

    try:
        saved_warnings = json.loads(WARNINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved_warnings, dict):
            warning_records.update(saved_warnings)
    except (OSError, json.JSONDecodeError):
        logging.exception("No se pudo cargar data/warnings.json")


def save_halloween() -> None:
    HALLOWEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = HALLOWEEN_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(halloween_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(HALLOWEEN_FILE)


def load_halloween() -> None:
    if not HALLOWEEN_FILE.exists():
        return

    try:
        saved_data = json.loads(HALLOWEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(saved_data, dict):
            halloween_data.update(saved_data)
    except (OSError, json.JSONDecodeError):
        logging.exception("No se pudo cargar data/halloween.json")


def halloween_guild_state(guild_id: int) -> dict[str, Any]:
    return halloween_data.setdefault(
        str(guild_id),
        {"active": False, "role_id": None, "players": {}},
    )


def halloween_player_stats(
    guild_id: int,
    user_id: int,
) -> dict[str, Any]:
    state = halloween_guild_state(guild_id)
    return state.setdefault("players", {}).setdefault(
        str(user_id),
        {"candies": 0, "games": 0, "jackpots": 0, "last_played": None},
    )


def roll_halloween_choice(choice: str, current_candies: int) -> tuple[int, str, bool]:
    rng = secrets.SystemRandom()
    if choice == "trato":
        reward = rng.randint(8, 14)
        return reward, f"Te llevas **{reward} caramelos**. ¡Buen trato!", False

    if rng.random() < 0.45:
        reward = rng.randint(20, 35)
        return (
            reward,
            f"¡El truco sale genial! Encuentras **{reward} caramelos extra**.",
            True,
        )

    loss = min(current_candies, rng.randint(1, 5))
    if loss == 0:
        return 0, "¡Te han asustado! Por suerte, no llevabas caramelos que perder.", False
    return -loss, f"¡Era una trampa! Un fantasma se lleva **{loss} caramelos**.", False


def halloween_cooldown_remaining(stats: dict[str, Any]) -> timedelta | None:
    last_played = stats.get("last_played")
    if not last_played:
        return None
    next_play = datetime.fromisoformat(last_played) + HALLOWEEN_COOLDOWN
    remaining = next_play - utc_now()
    return remaining if remaining.total_seconds() > 0 else None


def format_remaining(duration: timedelta) -> str:
    seconds = max(0, int(duration.total_seconds()))
    hours, remainder = divmod(seconds, 3_600)
    minutes = remainder // 60
    return f"{hours} h {minutes} min"


def new_giveaway_id() -> str:
    while True:
        giveaway_id = secrets.token_hex(3).upper()
        if giveaway_id not in giveaways:
            return giveaway_id


def giveaway_embed(giveaway: dict[str, Any]) -> discord.Embed:
    is_active = giveaway["status"] == "active"
    end_timestamp = int(datetime.fromisoformat(giveaway["ends_at"]).timestamp())
    participant_count = len(giveaway.get("participants", []))
    requirements = str(giveaway.get("requirements", "")).strip()

    if is_active:
        description = (
            "Pulsa **Participar** para entrar en el sorteo.\n\n"
            f"👥 Participantes: **{participant_count}**\n"
            f"🏆 Ganadores: **{giveaway['winner_count']}**\n"
            f"⏰ Termina: <t:{end_timestamp}:R>"
        )
        color = discord.Color.gold()
    elif giveaway["status"] == "cancelled":
        description = "Este sorteo ha sido cancelado por un administrador."
        color = discord.Color.dark_grey()
    else:
        winner_ids = giveaway.get("winners", [])
        winner_text = (
            " ".join(f"<@{winner_id}>" for winner_id in winner_ids)
            if winner_ids
            else "No hubo participantes."
        )
        description = (
            f"👥 Participantes: **{participant_count}**\n"
            f"🎉 Ganador(es): {winner_text}"
        )
        claim_deadline = giveaway.get("claim_deadline")
        if claim_deadline:
            claim_timestamp = int(
                datetime.fromisoformat(claim_deadline).timestamp()
            )
            description += (
                "\n📩 Abre un ticket para reclamar antes de "
                f"<t:{claim_timestamp}:R>."
            )
        description += f"\n⏰ Terminó: <t:{end_timestamp}:R>"
        color = discord.Color.green()

    embed = discord.Embed(
        title=f"🎉 Sorteo: {giveaway['prize']}",
        description=description,
        color=color,
    )
    embed.set_footer(
        text=f"ID: {giveaway['id']} · Organizado por <@{giveaway['host_id']}>"
    )
    if requirements:
        embed.add_field(name="Requisitos", value=requirements, inline=False)
    return embed


def can_manage_giveaways(member: discord.Member) -> bool:
    allowed_roles = {"administrador"}
    user_roles = {normalize_role_name(role.name) for role in member.roles}
    return bool(user_roles & allowed_roles)


def can_manage_halloween(member: discord.Member) -> bool:
    allowed_roles = {"administrador", "owner", "coowner"}
    user_roles = {normalize_role_name(role.name) for role in member.roles}
    has_required_permissions = (
        member.guild_permissions.value & HALLOWEEN_PERMISSION_MASK
    ) == HALLOWEEN_PERMISSION_MASK
    return bool(user_roles & allowed_roles) or has_required_permissions


def can_view_giveaway_participants(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    return can_manage_giveaways(interaction.user)


async def prepare_halloween_guild(
    guild: discord.Guild,
) -> tuple[discord.Role | None, str | None]:
    state = halloween_guild_state(guild.id)
    role = guild.get_role(int(state["role_id"])) if state.get("role_id") else None

    if role is None:
        role = discord.utils.find(
            lambda candidate: normalize_role_name(candidate.name)
            == normalize_role_name(HALLOWEEN_ROLE_NAME),
            guild.roles,
        )

    if role is None:
        try:
            role = await guild.create_role(
                name=HALLOWEEN_ROLE_NAME,
                colour=discord.Colour.from_rgb(116, 59, 191),
                hoist=True,
                mentionable=False,
                reason="Preparar el evento Truco o Trato de Halloween",
            )
        except discord.Forbidden:
            return None, (
                "No pude crear el rol. Dale al bot el permiso **Gestionar roles** "
                "y comprueba que su rol esté por encima de los roles que va a asignar."
            )
        except discord.HTTPException:
            return None, "Discord no pudo crear el rol. Inténtalo de nuevo."

    state["role_id"] = role.id
    state["active"] = True
    state.setdefault("players", {})
    save_halloween()
    return role, None


async def award_halloween_role(
    guild: discord.Guild,
    member: discord.Member,
) -> str | None:
    state = halloween_guild_state(guild.id)
    role_id = state.get("role_id")
    role = guild.get_role(int(role_id)) if role_id else None
    if role is None:
        return "No encuentro el rol del evento; avisa al staff para volver a prepararlo."
    if role in member.roles:
        return None

    try:
        await member.add_roles(
            role,
            reason="Recompensa del evento Truco o Trato de Halloween",
        )
    except discord.Forbidden:
        return (
            "Has alcanzado la meta, pero el bot no puede asignarte el rol. "
            "El staff debe subir el rol del bot por encima de "
            f"**{role.name}** y darle **Gestionar roles**."
        )
    except discord.HTTPException:
        return "Has alcanzado la meta, pero Discord no pudo asignar el rol. Avisa al staff."
    return None


async def play_halloween(
    guild: discord.Guild,
    member: discord.Member,
    choice: str,
) -> tuple[dict[str, Any] | None, str | None]:
    async with halloween_lock:
        state = halloween_guild_state(guild.id)
        if not state.get("active"):
            return None, "El evento está cerrado. Pide al staff que use `/halloween preparar`."

        stats = halloween_player_stats(guild.id, member.id)
        cooldown = halloween_cooldown_remaining(stats)
        if cooldown is not None:
            return None, (
                "Ya has jugado hoy. Podrás volver a llamar a la puerta en "
                f"**{format_remaining(cooldown)}**."
            )

        old_candies = int(stats.get("candies", 0))
        delta, message, jackpot = roll_halloween_choice(choice, old_candies)
        candies = max(0, old_candies + delta)
        stats["candies"] = candies
        stats["games"] = int(stats.get("games", 0)) + 1
        stats["jackpots"] = int(stats.get("jackpots", 0)) + int(jackpot)
        stats["last_played"] = utc_now().isoformat()
        save_halloween()

    role_message = None
    unlocked_now = (
        candies >= HALLOWEEN_REWARD_THRESHOLD
        and not any(
            normalize_role_name(role.name)
            == normalize_role_name(HALLOWEEN_ROLE_NAME)
            for role in member.roles
        )
    )
    if candies >= HALLOWEEN_REWARD_THRESHOLD:
        role_message = await award_halloween_role(guild, member)

    return {
        "message": message,
        "candies": candies,
        "old_candies": old_candies,
        "jackpot": jackpot,
        "unlocked_now": unlocked_now and role_message is None,
        "role_message": role_message,
        "choice": choice,
    }, None


def halloween_result_embed(
    member: discord.Member,
    result: dict[str, Any],
) -> discord.Embed:
    embed = discord.Embed(
        title="🎃 Truco o Trato",
        description=result["message"],
        color=discord.Color.purple() if result["jackpot"] else discord.Color.orange(),
    )
    embed.add_field(name="Tu bolsa", value=f"🍬 **{result['candies']} caramelos**")
    embed.add_field(
        name="Rol especial",
        value=(
            f"¡Desbloqueaste **{HALLOWEEN_ROLE_NAME}** 🐈‍⬛"
            if result["unlocked_now"]
            else (
                result["role_message"]
                or f"Te faltan **{max(0, HALLOWEEN_REWARD_THRESHOLD - result['candies'])}** "
                "caramelos para el rol."
            )
        ),
        inline=False,
    )
    embed.set_footer(text=f"Jugado por {member.display_name} · Vuelve en 20 horas")
    return embed


class HalloweenGameView(discord.ui.View):
    def __init__(self, guild: discord.Guild, member: discord.Member):
        super().__init__(timeout=90)
        self.guild = guild
        self.member = member
        self.used = False

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def choose(
        self,
        interaction: discord.Interaction,
        choice: str,
    ) -> None:
        if self.used:
            await interaction.response.send_message(
                "Ya elegiste una puerta. Puedes volver a jugar cuando termine el tiempo de espera.",
                ephemeral=True,
            )
            return
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                "Esta puerta es solo para quien inició el juego.",
                ephemeral=True,
            )
            return

        self.used = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.defer()
        result, error = await play_halloween(self.guild, self.member, choice)
        if error:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            if interaction.message is not None:
                await interaction.message.edit(
                    content=error,
                    embed=None,
                    view=self,
                )
            return

        if interaction.message is not None:
            await interaction.message.edit(
                content=None,
                embed=halloween_result_embed(self.member, result),
                view=None,
            )

    @discord.ui.button(
        label="Truco",
        emoji="👻",
        style=discord.ButtonStyle.danger,
    )
    async def trick(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self.choose(interaction, "truco")

    @discord.ui.button(
        label="Trato",
        emoji="🍬",
        style=discord.ButtonStyle.success,
    )
    async def treat(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self.choose(interaction, "trato")


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: str):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        giveaway = giveaways.get(giveaway_id)
        status = giveaway.get("status") if giveaway else "cancelled"
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.custom_id == "giveaway:join":
                child.disabled = status != "active"
            elif child.custom_id == "giveaway:claim":
                child.disabled = status != "ended"
            elif child.custom_id == "giveaway:participants":
                child.disabled = status == "cancelled"

    @discord.ui.button(
        label="Participar",
        style=discord.ButtonStyle.success,
        custom_id="giveaway:join",
    )
    async def join(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        giveaway = giveaways.get(self.giveaway_id)
        if giveaway is None or giveaway["status"] != "active":
            await interaction.response.send_message(
                "Este sorteo ya no está activo.",
                ephemeral=True,
            )
            return

        async with giveaway_lock:
            giveaway = giveaways.get(self.giveaway_id)
            if giveaway is None or giveaway["status"] != "active":
                await interaction.response.send_message(
                    "Este sorteo ya no está activo.",
                    ephemeral=True,
                )
                return

            participants = giveaway.setdefault("participants", [])
            if interaction.user.id in participants:
                await interaction.response.send_message(
                    "Ya estás participando en este sorteo.",
                    ephemeral=True,
                )
                return

            participants.append(interaction.user.id)
            save_giveaways()

        await interaction.response.send_message(
            "Te has apuntado al sorteo correctamente.",
            ephemeral=True,
        )
        if interaction.message is not None:
            await interaction.message.edit(
                embed=giveaway_embed(giveaway),
                view=self,
            )

    @discord.ui.button(
        label="Participantes",
        style=discord.ButtonStyle.secondary,
        custom_id="giveaway:participants",
    )
    async def participants(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        if not can_view_giveaway_participants(interaction):
            await interaction.response.send_message(
                "Solo los usuarios con el rol Administrador pueden ver los participantes.",
                ephemeral=True,
            )
            return

        giveaway = giveaways.get(self.giveaway_id)
        if giveaway is None:
            await interaction.response.send_message(
                "Este sorteo ya no existe.",
                ephemeral=True,
            )
            return

        participant_ids = giveaway.get("participants", [])
        if participant_ids:
            visible_participants = " ".join(
                f"<@{participant_id}>" for participant_id in participant_ids[:25]
            )
            if len(participant_ids) > 25:
                visible_participants += f"\n… y {len(participant_ids) - 25} más."
        else:
            visible_participants = "Todavía no hay participantes."

        await interaction.response.send_message(
            f"Participantes ({len(participant_ids)}):\n{visible_participants}",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Reclamar premio",
        style=discord.ButtonStyle.primary,
        custom_id="giveaway:claim",
    )
    async def claim(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        giveaway = giveaways.get(self.giveaway_id)
        if giveaway is None or giveaway["status"] != "ended":
            await interaction.response.send_message(
                "Este sorteo todavía no ha terminado o ya no está disponible.",
                ephemeral=True,
            )
            return

        if interaction.user.id not in giveaway.get("winners", []):
            await interaction.response.send_message(
                "Este botón solo está disponible para los ganadores.",
                ephemeral=True,
            )
            return

        claim_deadline = giveaway.get("claim_deadline")
        if claim_deadline and utc_now() > datetime.fromisoformat(claim_deadline):
            await interaction.response.send_message(
                "El tiempo para reclamar este premio ya terminó.",
                ephemeral=True,
            )
            return

        claims = giveaway.setdefault("claims", [])
        if interaction.user.id in claims:
            await interaction.response.send_message(
                "Ya has marcado este premio como reclamado. Abre un ticket para recibirlo.",
                ephemeral=True,
            )
            return

        claims.append(interaction.user.id)
        save_giveaways()
        await interaction.response.send_message(
            "Reclamación registrada. Abre un ticket en el servidor para recibir tu premio.",
            ephemeral=True,
        )


async def fetch_giveaway_message(
    giveaway: dict[str, Any],
) -> discord.Message | None:
    try:
        channel = bot.get_channel(int(giveaway["channel_id"]))
        if channel is None:
            channel = await bot.fetch_channel(int(giveaway["channel_id"]))
        return await channel.fetch_message(int(giveaway["message_id"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logging.warning("No se pudo acceder al mensaje del sorteo %s", giveaway["id"])
        return None


async def update_giveaway_message(
    giveaway: dict[str, Any],
) -> discord.Message | None:
    message = await fetch_giveaway_message(giveaway)
    if message is None:
        return None

    try:
        await message.edit(
            embed=giveaway_embed(giveaway),
            view=GiveawayView(giveaway["id"]),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logging.warning("No se pudo actualizar el sorteo %s", giveaway["id"])
    return message


async def finish_giveaway(
    giveaway_id: str,
    cancelled: bool = False,
) -> dict[str, Any] | None:
    async with giveaway_lock:
        giveaway = giveaways.get(giveaway_id)
        if giveaway is None or giveaway["status"] != "active":
            return None

        if cancelled:
            giveaway["status"] = "cancelled"
            giveaway["winners"] = []
            giveaway["claims"] = []
            giveaway["claim_deadline"] = None
        else:
            participants = list(giveaway.get("participants", []))
            winner_count = min(giveaway["winner_count"], len(participants))
            giveaway["status"] = "ended"
            giveaway["winners"] = secrets.SystemRandom().sample(
                participants,
                winner_count,
            )
            giveaway["claims"] = []
            giveaway["claim_deadline"] = (
                utc_now()
                + timedelta(hours=int(giveaway.get("claim_hours", 24)))
            ).isoformat() if winner_count else None

        save_giveaways()
        snapshot = dict(giveaway)

    current_task = asyncio.current_task()
    scheduled_task = giveaway_tasks.get(giveaway_id)
    if scheduled_task is not None and scheduled_task is not current_task:
        scheduled_task.cancel()

    message = await update_giveaway_message(snapshot)
    if message is not None and snapshot["status"] == "ended":
        winners = snapshot.get("winners", [])
        if winners:
            mentions = " ".join(f"<@{winner_id}>" for winner_id in winners)
            claim_timestamp = int(
                datetime.fromisoformat(snapshot["claim_deadline"]).timestamp()
            )
            await message.channel.send(
                f"🎉 Enhorabuena {mentions}, habéis ganado **{snapshot['prize']}**.\n"
                f"📩 Abrid un ticket antes de <t:{claim_timestamp}:F> para reclamarlo.\n"
                "Después de ese plazo, el staff podrá hacer un reroll."
            )
        else:
            await message.channel.send(
                f"El sorteo de **{snapshot['prize']}** terminó sin participantes."
            )

    return snapshot


async def reroll_giveaway(
    giveaway_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    async with giveaway_lock:
        giveaway = giveaways.get(giveaway_id)
        if giveaway is None:
            return None, "No encontré un sorteo con ese ID."
        if giveaway["status"] != "ended":
            return None, "Solo se puede hacer reroll de un sorteo terminado."

        winners = list(giveaway.get("winners", []))
        participants = list(giveaway.get("participants", []))
        candidates = [participant for participant in participants if participant not in winners]
        if not winners:
            return None, "Ese sorteo no tuvo ganadores."
        if not candidates:
            return None, "No hay participantes nuevos para elegir otro ganador."

        claims = set(giveaway.get("claims", []))
        target_index = next(
            (
                index
                for index, winner_id in enumerate(winners)
                if winner_id not in claims
            ),
            0,
        )
        old_winner = winners[target_index]
        new_winner = secrets.SystemRandom().choice(candidates)
        winners[target_index] = new_winner
        giveaway["winners"] = winners
        giveaway["claims"] = [
            winner_id
            for winner_id in giveaway.get("claims", [])
            if winner_id != old_winner
        ]
        giveaway["claim_deadline"] = (
            utc_now() + timedelta(hours=int(giveaway.get("claim_hours", 24)))
        ).isoformat()
        save_giveaways()
        snapshot = dict(giveaway)

    message = await update_giveaway_message(snapshot)
    if message is not None:
        claim_timestamp = int(
            datetime.fromisoformat(snapshot["claim_deadline"]).timestamp()
        )
        reroll_embed = discord.Embed(
            title="🔄 Nuevo ganador",
            description=(
                f"El premio **{snapshot['prize']}** tiene un nuevo ganador.\n\n"
                f"🎉 Ganador: <@{new_winner}>\n\n"
                f"📩 Abre un ticket antes de <t:{claim_timestamp}:F> "
                "para reclamar el premio."
            ),
            color=discord.Color.blurple(),
        )
        reroll_embed.set_footer(text=f"Sorteo {snapshot['id']}")
        await message.channel.send(embed=reroll_embed)

    return snapshot, None


def resolve_reroll_giveaway_id(
    guild_id: int,
    identifier: str | None,
) -> tuple[str | None, str | None]:
    if identifier:
        giveaway_id = identifier.upper()
        giveaway = giveaways.get(giveaway_id)
        if giveaway is None or giveaway["guild_id"] != guild_id:
            return None, "No encontré un sorteo con ese ID."
        return giveaway_id, None

    ended_giveaways = [
        giveaway
        for giveaway in giveaways.values()
        if giveaway["guild_id"] == guild_id and giveaway["status"] == "ended"
    ]
    if not ended_giveaways:
        return None, "No hay ningún sorteo terminado para hacer reroll."
    if len(ended_giveaways) > 1:
        return None, "Hay varios sorteos terminados. Usa el ID del sorteo."
    return ended_giveaways[0]["id"], None


async def schedule_giveaway(giveaway_id: str) -> None:
    giveaway = giveaways.get(giveaway_id)
    if giveaway is None:
        return

    delay = max(
        0,
        (datetime.fromisoformat(giveaway["ends_at"]) - utc_now()).total_seconds(),
    )
    try:
        await asyncio.sleep(delay)
        await finish_giveaway(giveaway_id)
    except asyncio.CancelledError:
        return
    finally:
        giveaway_tasks.pop(giveaway_id, None)


async def restore_active_giveaways() -> None:
    load_giveaways()
    for giveaway_id, giveaway in giveaways.items():
        if giveaway.get("status") not in {"active", "ended"}:
            continue

        try:
            bot.add_view(
                GiveawayView(giveaway_id),
                message_id=int(giveaway["message_id"]),
            )
        except (KeyError, ValueError, discord.ClientException):
            logging.exception("No se pudo restaurar el sorteo %s", giveaway_id)

        if giveaway.get("status") == "active":
            giveaway_tasks[giveaway_id] = asyncio.create_task(
                schedule_giveaway(giveaway_id)
            )


async def delete_command_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except discord.Forbidden:
        logging.warning(
            "No puedo borrar el comando: falta el permiso Gestionar mensajes."
        )
    except discord.HTTPException:
        logging.warning("Discord rechazó el borrado del mensaje de comando.")


def parse_user_id(value: str) -> int:
    normalized = value.strip()
    if normalized.startswith("<@") and normalized.endswith(">"):
        normalized = normalized[2:-1].lstrip("!")
    if not normalized.isdigit():
        raise ValueError("Indica un ID de usuario válido.")

    user_id = int(normalized)
    if user_id <= 0:
        raise ValueError("Indica un ID de usuario válido.")
    return user_id


async def fetch_guild_member(
    guild: discord.Guild,
    user_id: int,
) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member

    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def resolve_warning_target(
    guild: discord.Guild,
    reference: str,
) -> tuple[discord.Member | None, str | None]:
    reference = reference.strip()
    try:
        user_id = parse_user_id(reference)
    except ValueError:
        user_id = None

    if user_id is not None:
        target = await fetch_guild_member(guild, user_id)
        if target is None:
            return None, "No encontré a ese usuario en este servidor."
        return target, None

    normalized_reference = reference.casefold().lstrip("@")
    username_matches = [
        member
        for member in guild.members
        if member.name.casefold() == normalized_reference
    ]
    if len(username_matches) == 1:
        return username_matches[0], None
    if len(username_matches) > 1:
        return None, "Hay varios usuarios con ese nombre. Usa una mención o su ID."

    display_name_matches = [
        member
        for member in guild.members
        if member.display_name.casefold() == normalized_reference
    ]
    if len(display_name_matches) == 1:
        return display_name_matches[0], None
    if len(display_name_matches) > 1:
        return None, "Hay varios usuarios con ese nombre visible. Usa una mención o su ID."

    return None, "No encontré a ese usuario. Usa su nombre exacto, una mención o su ID."


def warnings_for_user(guild_id: int, user_id: int) -> list[dict[str, Any]]:
    guild_warnings = warning_records.get(str(guild_id), {})
    return guild_warnings.get(str(user_id), [])


def warning_history_embed(
    guild: discord.Guild,
    user_id: int,
    records: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title="📋 Registro de avisos",
        description=f"Usuario: <@{user_id}>\nID: `{user_id}`",
        color=discord.Color.orange(),
    )

    for record in records[-10:]:
        timestamp = int(datetime.fromisoformat(record["created_at"]).timestamp())
        embed.add_field(
            name=f"Aviso #{record['id']} · <t:{timestamp}:f>",
            value=(
                f"**Razón:** {record['reason']}\n"
                f"**Moderador:** <@{record['moderator_id']}>"
            ),
            inline=False,
        )

    if len(records) > 10:
        embed.set_footer(
            text=f"Mostrando los 10 avisos más recientes de un total de {len(records)}."
        )
    else:
        embed.set_footer(text=f"Total de avisos: {len(records)}")
    return embed


async def issue_warning(
    guild: discord.Guild,
    target: discord.Member,
    moderator_id: int,
    reason: str,
) -> tuple[discord.Member | None, dict[str, Any] | None, bool, str | None]:
    reason = reason.strip()
    if not reason:
        return None, None, False, "Debes indicar una razón para el aviso."
    if len(reason) > 500:
        return None, None, False, "La razón no puede superar los 500 caracteres."

    async with warnings_lock:
        guild_warnings = warning_records.setdefault(str(guild.id), {})
        user_warnings = guild_warnings.setdefault(str(target.id), [])
        warning = {
            "id": len(user_warnings) + 1,
            "user_id": target.id,
            "moderator_id": moderator_id,
            "reason": reason,
            "created_at": utc_now().isoformat(),
        }
        user_warnings.append(warning)
        save_warnings()

    dm_sent = True
    try:
        dm_embed = discord.Embed(
            title="⚠️ Has recibido un aviso",
            description=(
                f"Has recibido un aviso en **{guild.name}**.\n\n"
                f"**Razón:** {reason}\n"
                f"**Aviso:** #{warning['id']}"
            ),
            color=discord.Color.orange(),
        )
        await target.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False
        logging.info("No se pudo enviar por DM el aviso a %s.", target.id)

    return target, warning, dm_sent, None


async def handle_warning_command(
    message: discord.Message,
    content: str,
) -> None:
    parts = content.split(maxsplit=2)
    command = parts[0].casefold()

    if command == f"{PREFIX}warn":
        if len(parts) < 3:
            await message.channel.send(
                f"Uso: `{PREFIX}warn <usuario> <razón>`"
            )
            return

        target, target_error = await resolve_warning_target(
            message.guild,
            parts[1],
        )
        if target_error:
            await message.channel.send(target_error)
            return

        target, warning, dm_sent, error = await issue_warning(
            message.guild,
            target,
            message.author.id,
            parts[2],
        )
        if error:
            await message.channel.send(error)
            return

        await delete_command_message(message)
        dm_note = (
            " También se le envió un DM."
            if dm_sent
            else " No se pudo enviarle DM; comprueba que tenga los mensajes privados abiertos."
        )
        await message.channel.send(
            f"⚠️ Aviso **#{warning['id']}** registrado para "
            f"{target.mention}.{dm_note}"
        )
        return

    if len(parts) < 2:
        await message.channel.send(f"Uso: `{PREFIX}warns <usuario>`")
        return

    target, target_error = await resolve_warning_target(
        message.guild,
        parts[1],
    )
    if target_error:
        await message.channel.send(target_error)
        return

    records = warnings_for_user(message.guild.id, target.id)
    await delete_command_message(message)
    if not records:
        await message.channel.send(
            f"El usuario {target.mention} no tiene avisos registrados."
        )
        return

    await message.channel.send(
        embed=warning_history_embed(message.guild, target.id, records)
    )


async def create_giveaway(
    channel: discord.abc.Messageable,
    guild_id: int,
    host_id: int,
    duration_value: str,
    winner_count: int,
    prize: str,
    requirements: str = "",
    claim_hours: int = 24,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        duration_seconds = parse_duration(duration_value)
    except ValueError as error:
        return None, f"Error: {error}"

    if not 1 <= winner_count <= 20:
        return None, "El número de ganadores debe estar entre 1 y 20."

    prize = prize.strip()
    if not 1 <= len(prize) <= 200:
        return None, "El premio debe tener entre 1 y 200 caracteres."
    requirements = requirements.strip()
    if len(requirements) > 500:
        return None, "Los requisitos no pueden superar los 500 caracteres."
    if not 1 <= claim_hours <= 168:
        return None, "El límite de reclamación debe estar entre 1 y 168 horas."

    giveaway_id = new_giveaway_id()
    ends_at = utc_now() + timedelta(seconds=duration_seconds)
    giveaway = {
        "id": giveaway_id,
        "guild_id": guild_id,
        "channel_id": channel.id,
        "message_id": 0,
        "host_id": host_id,
        "prize": prize,
        "winner_count": winner_count,
        "ends_at": ends_at.isoformat(),
        "status": "active",
        "participants": [],
        "winners": [],
        "requirements": requirements,
        "claim_hours": claim_hours,
        "claim_deadline": None,
        "claims": [],
    }
    giveaways[giveaway_id] = giveaway
    save_giveaways()

    try:
        giveaway_message = await channel.send(
            embed=giveaway_embed(giveaway),
            view=GiveawayView(giveaway_id),
        )
    except discord.HTTPException:
        giveaways.pop(giveaway_id, None)
        save_giveaways()
        return (
            None,
            "No pude crear el sorteo. Comprueba que tengo permiso para enviar mensajes.",
        )

    giveaway["message_id"] = giveaway_message.id
    save_giveaways()
    giveaway_tasks[giveaway_id] = asyncio.create_task(
        schedule_giveaway(giveaway_id)
    )
    return giveaway, None


async def handle_giveaway_command(
    message: discord.Message,
    content: str,
) -> None:
    parts = content.split(maxsplit=4)
    subcommand = parts[1].casefold() if len(parts) > 1 else "ayuda"
    await delete_command_message(message)

    if subcommand in {"ayuda", "help"}:
        await message.channel.send(
            "**Comandos de sorteos:**\n"
            f"`{PREFIX}sorteo crear <duración> <ganadores> <premio>`\n"
            f"`{PREFIX}sorteo terminar <ID>`\n"
            f"`{PREFIX}sorteo cancelar <ID>`\n"
            f"`{PREFIX}sorteo reroll [ID]`\n"
            f"`{PREFIX}sorteo lista`\n\n"
            "Duraciones: `30s`, `10m`, `2h`, `1d` o combinadas como `1d2h`.\n"
            "Opcionales: `| requisitos: rol VIP | reclamo: 24h`."
        )
        return

    if subcommand == "crear":
        if len(parts) < 5:
            await message.channel.send(
                f"Uso: `{PREFIX}sorteo crear 1h 1 Premio del sorteo`"
            )
            return

        try:
            winner_count = int(parts[3])
        except ValueError as error:
            await message.channel.send(
                "El número de ganadores debe ser un número entero."
            )
            return

        try:
            prize, requirements, claim_hours = parse_create_options(parts[4])
        except ValueError as error:
            await message.channel.send(f"Error: {error}")
            return

        _, error = await create_giveaway(
            channel=message.channel,
            guild_id=message.guild.id,
            host_id=message.author.id,
            duration_value=parts[2],
            winner_count=winner_count,
            prize=prize,
            requirements=requirements,
            claim_hours=claim_hours,
        )
        if error:
            await message.channel.send(error)
        return

    if subcommand in {"terminar", "finalizar"}:
        if len(parts) < 3:
            await message.channel.send(f"Uso: `{PREFIX}sorteo terminar <ID>`")
            return

        result = await finish_giveaway(parts[2].upper())
        if result is None:
            await message.channel.send("No encontré un sorteo activo con ese ID.")
        return

    if subcommand == "cancelar":
        if len(parts) < 3:
            await message.channel.send(f"Uso: `{PREFIX}sorteo cancelar <ID>`")
            return

        result = await finish_giveaway(parts[2].upper(), cancelled=True)
        if result is None:
            await message.channel.send("No encontré un sorteo activo con ese ID.")
        return

    if subcommand == "reroll":
        giveaway_id, resolve_error = resolve_reroll_giveaway_id(
            message.guild.id,
            parts[2] if len(parts) >= 3 else None,
        )
        if resolve_error:
            await message.channel.send(resolve_error)
            return

        _, error = await reroll_giveaway(giveaway_id)
        if error:
            await message.channel.send(error)
        return

    if subcommand == "lista":
        active_giveaways = [
            giveaway
            for giveaway in giveaways.values()
            if giveaway["guild_id"] == message.guild.id
            and giveaway["status"] == "active"
        ]
        if not active_giveaways:
            await message.channel.send("No hay sorteos activos en este servidor.")
            return

        lines = [
            f"**{giveaway['id']}** · {giveaway['prize']} · "
            f"<t:{int(datetime.fromisoformat(giveaway['ends_at']).timestamp())}:R> · "
            f"{len(giveaway.get('participants', []))} participante(s)"
            for giveaway in active_giveaways[:20]
        ]
        await message.channel.send("**Sorteos activos:**\n" + "\n".join(lines))
        return

    await message.channel.send(
        f"No conozco ese subcomando. Usa `{PREFIX}sorteo ayuda`."
    )


async def send_in_chunks(channel: discord.abc.Messageable, text: str) -> None:
    """Send text without exceeding Discord's 2,000-character limit."""
    for start in range(0, len(text), 2_000):
        await channel.send(text[start : start + 2_000])


@bot.event
async def on_ready() -> None:
    global giveaways_loaded, warnings_loaded, halloween_loaded

    if bot.user is not None:
        logging.info("Bot conectado como %s (ID: %s)", bot.user, bot.user.id)

    # Sincronizar el comando en cada servidor para que aparezca rápidamente
    # en el menú de comandos de Discord.
    try:
        for guild in bot.guilds:
            command_tree.copy_global_to(guild=guild)
            await command_tree.sync(guild=guild)
        logging.info("Comandos sincronizados en %d servidor(es)", len(bot.guilds))
    except Exception:
        logging.exception("No se pudieron sincronizar los comandos")

    if not giveaways_loaded:
        await restore_active_giveaways()
        giveaways_loaded = True
        logging.info("Sorteos activos restaurados: %d", len(giveaway_tasks))

    if not warnings_loaded:
        load_warnings()
        warnings_loaded = True
        logging.info("Registros de avisos cargados")

    if not halloween_loaded:
        load_halloween()
        halloween_loaded = True
        logging.info("Datos del evento Halloween cargados")


@command_tree.command(name="warn", description="Registra un aviso para un usuario")
@app_commands.describe(
    usuario="Usuario que recibirá el aviso",
    razon="Motivo del aviso",
)
async def warn_command(
    interaction: discord.Interaction,
    usuario: discord.Member,
    razon: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not can_manage_warnings(
        interaction.user
    ):
        await interaction.response.send_message(
            "Solo los roles Owner, Co-Owner y Mod pueden gestionar avisos.",
            ephemeral=True,
        )
        return

    target, warning, dm_sent, error = await issue_warning(
        interaction.guild,
        usuario,
        interaction.user.id,
        razon,
    )
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    dm_note = (
        " También se le envió un DM."
        if dm_sent
        else " No se pudo enviarle DM; puede tener los mensajes privados cerrados."
    )
    await interaction.response.send_message(
        f"⚠️ Aviso **#{warning['id']}** registrado para "
        f"{target.mention}.{dm_note}",
        ephemeral=True,
    )


@command_tree.command(name="warns", description="Consulta los avisos de un usuario")
@app_commands.describe(usuario="Usuario cuyo registro quieres consultar")
async def warns_command(
    interaction: discord.Interaction,
    usuario: discord.Member,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not can_manage_warnings(
        interaction.user
    ):
        await interaction.response.send_message(
            "Solo los roles Owner, Co-Owner y Mod pueden consultar avisos.",
            ephemeral=True,
        )
        return

    records = warnings_for_user(interaction.guild.id, usuario.id)
    if not records:
        await interaction.response.send_message(
            f"El usuario {usuario.mention} no tiene avisos registrados.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=warning_history_embed(interaction.guild, usuario.id, records),
        ephemeral=True,
    )


@command_tree.command(name="send", description="Envía un mensaje como el bot")
@app_commands.describe(mensaje="El mensaje que quieres enviar")
async def send_command(interaction: discord.Interaction, mensaje: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor."
        )
        return

    if not isinstance(interaction.user, discord.Member) or not (
        interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message("No puedes hacer eso tontin")
        return

    chunks = [mensaje[start : start + 2_000] for start in range(0, len(mensaje), 2_000)]
    await interaction.response.send_message(chunks[0])

    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


def interaction_is_admin(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and isinstance(interaction.user, discord.Member)
        and can_manage_giveaways(interaction.user)
    )


def interaction_can_manage_halloween(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and isinstance(interaction.user, discord.Member)
        and can_manage_halloween(interaction.user)
    )


def halloween_board_embed(guild: discord.Guild) -> discord.Embed:
    state = halloween_guild_state(guild.id)
    players = state.get("players", {})
    ranked_players = sorted(
        (
            (int(user_id), player)
            for user_id, player in players.items()
            if int(player.get("candies", 0)) > 0
        ),
        key=lambda entry: (
            int(entry[1].get("candies", 0)),
            int(entry[1].get("jackpots", 0)),
        ),
        reverse=True,
    )[:10]

    embed = discord.Embed(
        title="🏆 Tabla de caramelos",
        description=(
            "Clasificación del evento Truco o Trato."
            if ranked_players
            else "Todavía no hay caramelos en la tabla. ¡Sé el primero en jugar!"
        ),
        color=discord.Color.purple(),
    )
    if ranked_players:
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[index] if index < 3 else f'**{index + 1}.**'} "
            f"<@{user_id}> — **{int(player.get('candies', 0))} 🍬**"
            for index, (user_id, player) in enumerate(ranked_players)
        ]
        embed.add_field(name="Top 10", value="\n".join(lines), inline=False)
    embed.add_field(
        name=f"Recompensa · {HALLOWEEN_ROLE_NAME} 🐈‍⬛",
        value=f"Consigue **{HALLOWEEN_REWARD_THRESHOLD} caramelos** para desbloquear el rol.",
        inline=False,
    )
    embed.set_footer(text="Se actualiza al jugar · Una visita cada 20 horas")
    return embed


def halloween_profile_embed(
    guild: discord.Guild,
    member: discord.Member,
) -> discord.Embed:
    stats = halloween_player_stats(guild.id, member.id)
    candies = int(stats.get("candies", 0))
    cooldown = halloween_cooldown_remaining(stats)
    role = guild.get_role(int(halloween_guild_state(guild.id).get("role_id") or 0))
    has_special_role = role is not None and role in member.roles
    progress = min(100, round(candies / HALLOWEEN_REWARD_THRESHOLD * 100))

    embed = discord.Embed(
        title=f"🎃 Perfil de {member.display_name}",
        description=f"🍬 **{candies} caramelos**\n"
        f"🎲 Partidas: **{int(stats.get('games', 0))}** · "
        f"👻 Trucos exitosos: **{int(stats.get('jackpots', 0))}**",
        color=discord.Color.purple(),
    )
    embed.add_field(
        name=f"Progreso de {HALLOWEEN_ROLE_NAME}",
        value=(
            f"`{'▰' * (progress // 10)}{'▱' * (10 - progress // 10)}` "
            f"**{candies}/{HALLOWEEN_REWARD_THRESHOLD}** 🍬"
        ),
        inline=False,
    )
    embed.add_field(
        name="Rol especial",
        value=(
            "Desbloqueado 🐈‍⬛"
            if has_special_role
            else (
                "Meta alcanzada: el staff debe revisar los permisos del bot."
                if candies >= HALLOWEEN_REWARD_THRESHOLD
                else f"Te faltan {HALLOWEEN_REWARD_THRESHOLD - candies} caramelos."
            )
        ),
        inline=True,
    )
    embed.add_field(
        name="Próxima visita",
        value=(
            f"En **{format_remaining(cooldown)}**"
            if cooldown
            else "¡Puedes jugar ahora!"
        ),
        inline=True,
    )
    return embed


async def handle_halloween_command(
    message: discord.Message,
    content: str,
) -> None:
    parts = content.split(maxsplit=2)
    subcommand = parts[1].casefold() if len(parts) > 1 else "ayuda"

    if subcommand in {"ayuda", "help", "reglas"}:
        await message.channel.send(
            "**🎃 Evento Truco o Trato**\n"
            f"`{PREFIX}halloween jugar` — elige Truco o Trato.\n"
            f"`{PREFIX}halloween tabla` — mira el top 10 de caramelos.\n"
            f"`{PREFIX}halloween perfil [usuario]` — consulta tu progreso.\n"
            "Trato: ganas 8–14 caramelos. Truco: 45% de ganar 20–35; "
            "si sale mal, puedes perder hasta 5.\n"
            "Puedes jugar una vez cada 20 horas. Al llegar a "
            f"**{HALLOWEEN_REWARD_THRESHOLD} caramelos** recibes "
            f"**{HALLOWEEN_ROLE_NAME}** 🐈‍⬛."
        )
        return

    if subcommand == "preparar":
        if not can_manage_halloween(message.author):
            await message.channel.send(
                "Solo los roles Administrador, Owner o Co-Owner pueden preparar el evento."
            )
            return
        await delete_command_message(message)
        role, error = await prepare_halloween_guild(message.guild)
        if error:
            await message.channel.send(error)
            return
        await message.channel.send(
            embed=discord.Embed(
                title="🎃 ¡Ha comenzado Truco o Trato!",
                description=(
                    "El evento ya está abierto. Usa **,halloween jugar** para "
                    "llamar a una puerta, consigue caramelos y escala la tabla.\n\n"
                    f"Al reunir **{HALLOWEEN_REWARD_THRESHOLD} caramelos** "
                    f"desbloquearás **{role.name}** 🐈‍⬛."
                ),
                color=discord.Color.purple(),
            )
        )
        return

    if subcommand == "cerrar":
        if not can_manage_halloween(message.author):
            await message.channel.send(
                "Solo los roles Administrador, Owner o Co-Owner pueden cerrar el evento."
            )
            return
        state = halloween_guild_state(message.guild.id)
        state["active"] = False
        save_halloween()
        await delete_command_message(message)
        await message.channel.send("🎃 El evento Truco o Trato está cerrado.")
        return

    if subcommand == "jugar":
        state = halloween_guild_state(message.guild.id)
        if not state.get("active"):
            await message.channel.send(
                "El evento está cerrado. El staff puede iniciarlo con "
                f"`{PREFIX}halloween preparar`."
            )
            return
        stats = halloween_player_stats(message.guild.id, message.author.id)
        cooldown = halloween_cooldown_remaining(stats)
        if cooldown:
            await message.channel.send(
                f"Ya llamaste a una puerta. Vuelve en **{format_remaining(cooldown)}**."
            )
            return

        await delete_command_message(message)
        embed = discord.Embed(
            title="🏚️ Una puerta misteriosa",
            description=(
                f"{message.author.mention}, elige tu destino:\n\n"
                "👻 **Truco:** riesgo alto. 45% de ganar 20–35 caramelos; "
                "si fallas puedes perder hasta 5.\n"
                "🍬 **Trato:** ganas 8–14 caramelos con seguridad.\n\n"
                "Solo tú puedes elegir. Tienes una visita cada 20 horas."
            ),
            color=discord.Color.dark_purple(),
        )
        await message.channel.send(
            embed=embed,
            view=HalloweenGameView(message.guild, message.author),
        )
        return

    if subcommand == "tabla":
        await message.channel.send(embed=halloween_board_embed(message.guild))
        return

    if subcommand == "perfil":
        target = message.author
        if len(parts) > 2:
            target, error = await resolve_warning_target(message.guild, parts[2])
            if error:
                await message.channel.send(error)
                return
        await message.channel.send(embed=halloween_profile_embed(message.guild, target))
        return

    await message.channel.send(
        f"No conozco ese subcomando. Usa `{PREFIX}halloween ayuda`."
    )


@halloween_group.command(name="preparar", description="Crea el rol e inicia Truco o Trato")
async def halloween_prepare_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return
    if not interaction_can_manage_halloween(interaction):
        await interaction.response.send_message(
            "Solo los roles Administrador, Owner o Co-Owner pueden preparar el evento.",
            ephemeral=True,
        )
        return

    role, error = await prepare_halloween_guild(interaction.guild)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    announcement = discord.Embed(
        title="🎃 ¡Ha comenzado Truco o Trato!",
        description=(
            "El evento ya está abierto. Usa **/halloween jugar** para llamar "
            "a una puerta, consigue caramelos y sube en la tabla.\n\n"
            f"Al reunir **{HALLOWEEN_REWARD_THRESHOLD} caramelos**, "
            f"desbloquearás **{role.name}** 🐈‍⬛."
        ),
        color=discord.Color.purple(),
    )
    await interaction.response.send_message(embed=announcement)


@halloween_group.command(name="cerrar", description="Cierra temporalmente el evento")
async def halloween_close_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return
    if not interaction_can_manage_halloween(interaction):
        await interaction.response.send_message(
            "Solo los roles Administrador, Owner o Co-Owner pueden cerrar el evento.",
            ephemeral=True,
        )
        return

    state = halloween_guild_state(interaction.guild.id)
    state["active"] = False
    save_halloween()
    await interaction.response.send_message("🎃 El evento Truco o Trato está cerrado.")


@halloween_group.command(name="jugar", description="Llama a una puerta en busca de caramelos")
async def halloween_play_command(interaction: discord.Interaction) -> None:
    if (
        interaction.guild is None
        or not isinstance(interaction.user, discord.Member)
    ):
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return

    state = halloween_guild_state(interaction.guild.id)
    if not state.get("active"):
        await interaction.response.send_message(
            "El evento está cerrado. Pide al staff que use `/halloween preparar`.",
            ephemeral=True,
        )
        return

    stats = halloween_player_stats(interaction.guild.id, interaction.user.id)
    cooldown = halloween_cooldown_remaining(stats)
    if cooldown:
        await interaction.response.send_message(
            f"Ya llamaste a una puerta. Vuelve en **{format_remaining(cooldown)}**.",
            ephemeral=True,
        )
        return

    game_embed = discord.Embed(
        title="🏚️ Una puerta misteriosa",
        description=(
            "Elige tu destino:\n\n"
            "👻 **Truco:** riesgo alto. 45% de ganar 20–35 caramelos; "
            "si fallas puedes perder hasta 5.\n"
            "🍬 **Trato:** ganas 8–14 caramelos con seguridad.\n\n"
            "Solo tú puedes elegir. Tienes una visita cada 20 horas."
        ),
        color=discord.Color.dark_purple(),
    )
    await interaction.response.send_message(
        embed=game_embed,
        view=HalloweenGameView(interaction.guild, interaction.user),
        ephemeral=True,
    )


@halloween_group.command(name="tabla", description="Muestra el top 10 de caramelos")
async def halloween_leaderboard_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=halloween_board_embed(interaction.guild)
    )


@halloween_group.command(name="perfil", description="Muestra el progreso del evento")
@app_commands.describe(miembro="Perfil que quieres consultar; por defecto, el tuyo")
async def halloween_profile_command(
    interaction: discord.Interaction,
    miembro: discord.Member | None = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return
    target = miembro or interaction.user
    await interaction.response.send_message(
        embed=halloween_profile_embed(interaction.guild, target),
        ephemeral=True,
    )


@giveaway_group.command(name="crear", description="Crea un sorteo")
@app_commands.describe(
    duracion="Ejemplos: 30s, 10m, 2h, 1d2h",
    ganadores="Número de ganadores, entre 1 y 20",
    premio="Premio del sorteo",
    requisitos="Opcional. Déjalo vacío si no hay requisitos.",
    horas_reclamar="Horas que tienen los ganadores para abrir ticket",
)
async def giveaway_create_command(
    interaction: discord.Interaction,
    duracion: str,
    ganadores: app_commands.Range[int, 1, 20],
    premio: str,
    requisitos: str = "",
    horas_reclamar: app_commands.Range[int, 1, 168] = 24,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando solo funciona dentro de un servidor.",
            ephemeral=True,
        )
        return

    if not interaction_is_admin(interaction):
        await interaction.response.send_message(
            "Necesitas tener el rol Administrador para gestionar sorteos.",
            ephemeral=True,
        )
        return

    if interaction.channel is None:
        await interaction.response.send_message(
            "No pude identificar el canal del sorteo.",
            ephemeral=True,
        )
        return

    giveaway, error = await create_giveaway(
        channel=interaction.channel,
        guild_id=interaction.guild.id,
        host_id=interaction.user.id,
        duration_value=duracion,
        winner_count=int(ganadores),
        prize=premio,
        requirements=requisitos,
        claim_hours=int(horas_reclamar),
    )
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    await interaction.response.send_message(
        f"Sorteo creado correctamente. ID: `{giveaway['id']}`",
        ephemeral=True,
    )


@giveaway_group.command(name="terminar", description="Termina un sorteo y elige ganadores")
@app_commands.describe(identificador="ID que aparece en el sorteo")
async def giveaway_end_command(
    interaction: discord.Interaction,
    identificador: str,
) -> None:
    if not interaction_is_admin(interaction):
        await interaction.response.send_message(
            "Necesitas tener el rol Administrador para gestionar sorteos.",
            ephemeral=True,
        )
        return

    giveaway = giveaways.get(identificador.upper())
    if giveaway is None or giveaway["guild_id"] != interaction.guild.id:
        await interaction.response.send_message(
            "No encontré un sorteo activo con ese ID.",
            ephemeral=True,
        )
        return

    result = await finish_giveaway(identificador.upper())
    if result is None:
        await interaction.response.send_message(
            "No encontré un sorteo activo con ese ID.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"El sorteo `{identificador.upper()}` ha terminado.",
        ephemeral=True,
    )


@giveaway_group.command(name="cancelar", description="Cancela un sorteo activo")
@app_commands.describe(identificador="ID que aparece en el sorteo")
async def giveaway_cancel_command(
    interaction: discord.Interaction,
    identificador: str,
) -> None:
    if not interaction_is_admin(interaction):
        await interaction.response.send_message(
            "Necesitas tener el rol Administrador para gestionar sorteos.",
            ephemeral=True,
        )
        return

    giveaway = giveaways.get(identificador.upper())
    if giveaway is None or giveaway["guild_id"] != interaction.guild.id:
        await interaction.response.send_message(
            "No encontré un sorteo activo con ese ID.",
            ephemeral=True,
        )
        return

    result = await finish_giveaway(identificador.upper(), cancelled=True)
    if result is None:
        await interaction.response.send_message(
            "No encontré un sorteo activo con ese ID.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"El sorteo `{identificador.upper()}` ha sido cancelado.",
        ephemeral=True,
    )


@giveaway_group.command(name="reroll", description="Elige un nuevo ganador")
@app_commands.describe(identificador="Opcional si solo hay un sorteo terminado")
async def giveaway_reroll_command(
    interaction: discord.Interaction,
    identificador: str | None = None,
) -> None:
    if not interaction_is_admin(interaction):
        await interaction.response.send_message(
            "Necesitas tener el rol Administrador para gestionar sorteos.",
            ephemeral=True,
        )
        return

    giveaway_id, resolve_error = resolve_reroll_giveaway_id(
        interaction.guild.id,
        identificador,
    )
    if resolve_error:
        await interaction.response.send_message(resolve_error, ephemeral=True)
        return

    _, error = await reroll_giveaway(giveaway_id)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    await interaction.response.send_message(
        f"Se ha elegido un nuevo ganador para `{giveaway_id}`.",
        ephemeral=True,
    )


@giveaway_group.command(name="lista", description="Muestra tus sorteos activos")
async def giveaway_list_command(interaction: discord.Interaction) -> None:
    if not interaction_is_admin(interaction):
        await interaction.response.send_message(
            "Necesitas tener el rol Administrador para gestionar sorteos.",
            ephemeral=True,
        )
        return

    active_giveaways = [
        giveaway
        for giveaway in giveaways.values()
        if giveaway["guild_id"] == interaction.guild.id
        and giveaway["status"] == "active"
    ]
    if not active_giveaways:
        await interaction.response.send_message(
            "No hay sorteos activos en este servidor.",
            ephemeral=True,
        )
        return

    lines = [
        f"**{giveaway['id']}** · {giveaway['prize']} · "
        f"<t:{int(datetime.fromisoformat(giveaway['ends_at']).timestamp())}:R> · "
        f"{len(giveaway.get('participants', []))} participante(s)"
        for giveaway in active_giveaways[:20]
    ]
    await interaction.response.send_message(
        "**Sorteos activos:**\n" + "\n".join(lines),
        ephemeral=True,
    )


command_tree.add_command(giveaway_group)
command_tree.add_command(halloween_group)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # El comando con prefijo se procesa solo dentro de servidores.
    if message.guild is None:
        return

    content = message.content.strip()
    lowered_content = content.casefold()
    is_giveaway_command = lowered_content == f"{PREFIX}sorteo" or lowered_content.startswith(
        f"{PREFIX}sorteo "
    )
    is_warn_command = lowered_content == f"{PREFIX}warn" or lowered_content.startswith(
        f"{PREFIX}warn "
    )
    is_warns_command = lowered_content == f"{PREFIX}warns" or lowered_content.startswith(
        f"{PREFIX}warns "
    )
    is_halloween_command = lowered_content == f"{PREFIX}halloween" or lowered_content.startswith(
        f"{PREFIX}halloween "
    )
    is_prefix_command = lowered_content == f"{PREFIX}send" or lowered_content.startswith(
        f"{PREFIX}send "
    )
    is_legacy_command = lowered_content.startswith("send:")

    if is_warn_command or is_warns_command:
        if not can_manage_warnings(message.author):
            await message.channel.send(
                "Solo los roles Owner, Co-Owner y Mod pueden gestionar avisos."
            )
            return
        await handle_warning_command(message, content)
        return

    if is_halloween_command:
        await handle_halloween_command(message, content)
        return

    if is_giveaway_command:
        if not can_manage_giveaways(message.author):
            await message.channel.send(
                "Necesitas tener el rol Administrador para gestionar sorteos."
            )
            return
        await handle_giveaway_command(message, content)
        return

    if not is_prefix_command and not is_legacy_command:
        return

    if not message.author.guild_permissions.administrator:
        await message.channel.send("No puedes hacer eso tontin")
        return

    if is_prefix_command:
        text = content[len(f"{PREFIX}send") :].strip()
    else:
        text = content[len("send:") :].strip()

    if not text:
        await message.channel.send(f"Uso: `{PREFIX}send tu mensaje`")
        return

    if is_prefix_command:
        await delete_command_message(message)

    await send_in_chunks(message.channel, text)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "Falta el secreto DISCORD_TOKEN. Guárdalo en Secrets antes de iniciar el bot."
        )

    bot.run(token)


if __name__ == "__main__":
    main()
