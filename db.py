"""自携数据库封装（SQLAlchemy + aiosqlite）

AstrBot 只提供简单 KV 存储，无法满足关系型查询需求。
这里自携 SQLAlchemy + aiosqlite，完全独立于框架。
"""

from astrbot.api import logger
from pathlib import Path
from .config import DATA_DIR

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import String, Text, Boolean, BigInteger, select, text

Base = declarative_base()


class SkUser(Base):
    """森空岛用户表"""
    __tablename__ = "skland_user"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 跨平台用户标识（如 "qq:123456" 或 "telegram:987654"）
    platform_user_id: Mapped[str] = mapped_column(Text, unique=True)
    # 森空岛用户ID（API用，非平台ID）
    user_id: Mapped[str] = mapped_column(Text, nullable=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=True)
    cred: Mapped[str] = mapped_column(Text)
    cred_token: Mapped[str] = mapped_column(Text)
    # 统一消息来源（用于定时任务推送消息）
    umo: Mapped[str] = mapped_column(Text, nullable=True)
    binding_group: Mapped[str] = mapped_column(Text, nullable=True)


class Character(Base):
    """绑定的游戏角色表"""
    __tablename__ = "skland_characters"
    # 关联用户ID + 角色UID 组成复合主键
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(primary_key=True)
    role_id: Mapped[str] = mapped_column(String, nullable=True)
    app_code: Mapped[str] = mapped_column(Text)
    channel_master_id: Mapped[str] = mapped_column(Text)
    nickname: Mapped[str] = mapped_column(Text)
    isdefault: Mapped[bool] = mapped_column(default=False)


class GachaRecord(Base):
    """抽卡记录表"""
    __tablename__ = "skland_gacha_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column()  # 关联 SkUser.id
    char_pk_id: Mapped[int] = mapped_column()  # 关联 Character.id
    char_uid: Mapped[str] = mapped_column(Text)
    app_code: Mapped[str] = mapped_column(Text, default="arknights")
    """Game App Code: arknights / endfield"""
    item_type: Mapped[str] = mapped_column(Text, default="char")
    """Item Type: char / weapon"""
    pool_id: Mapped[str] = mapped_column(Text)
    pool_name: Mapped[str] = mapped_column(Text)
    char_id: Mapped[str] = mapped_column(Text)
    char_name: Mapped[str] = mapped_column(Text)
    rarity: Mapped[int] = mapped_column()
    is_new: Mapped[bool] = mapped_column()
    is_free: Mapped[bool] = mapped_column(default=False)
    gacha_ts: Mapped[int] = mapped_column(BigInteger)
    pos: Mapped[int] = mapped_column()


class StaminaAlert(Base):
    """体力预警状态表

    每个角色一行，记录是否已经发送过当前满体周期的预警。
    当体力从90%以下升到90%以上时发送一次；降到90%以下后重置。
    """
    __tablename__ = "skland_stamina_alert"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 关联 SkUser.id
    user_id: Mapped[int] = mapped_column()
    # 角色UID
    char_uid: Mapped[str] = mapped_column(Text)
    # 游戏类型：arknights / endfield
    game: Mapped[str] = mapped_column(String)
    # 是否已经发送过当前周期的预警
    alerted: Mapped[bool] = mapped_column(default=False)
    # 最后检查时间戳
    last_check_time: Mapped[int] = mapped_column(nullable=True)


# 模块级单例：避免重复创建引擎
_engine = None
_session_maker = None

# 默认数据库路径（AstrBot 插件规范目录）
_DEFAULT_DB_PATH = str(DATA_DIR / "skland.db")


async def init_db(db_path: str = None, force: bool = False):
    """初始化数据库连接并创建表（幂等，但检测文件是否被删除）

    Args:
        db_path: 数据库文件路径，默认为 data/plugin_data/astrbot_plugin_priestess/skland.db
        force: 强制重新初始化（用于数据库文件被删除后重建）
    """
    global _engine, _session_maker

    path = db_path or _DEFAULT_DB_PATH
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)

    # 如果引擎已存在，检查数据库文件是否还存在
    # 用户可能通过 rm 删除了数据库文件，但 _engine 仍然缓存着旧连接
    if _engine is not None and not force:
        if not path_obj.exists():
            # 数据库文件被删除了，需要强制重新初始化
            logger.info("[Skland] 检测到数据库文件缺失，重新初始化数据库...")
            force = True
        else:
            # 一切正常，复用现有连接
            return

    if _engine is not None and force:
        # 释放旧引擎的资源
        await _engine.dispose()
        _engine = None
        _session_maker = None

    if _engine is None:
        _engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 迁移：为旧表添加 umo 列（如果不存在）
            result = await conn.execute(
                text("SELECT name FROM pragma_table_info('skland_user') WHERE name = 'umo'")
            )
            if not result.fetchone():
                await conn.execute(text("ALTER TABLE skland_user ADD COLUMN umo TEXT"))
                logger.info("[Skland] 数据库迁移：skland_user 添加 umo 列")
            result = await conn.execute(
                text("SELECT name FROM pragma_table_info('skland_user') WHERE name = 'binding_group'")
            )
            if not result.fetchone():
                await conn.execute(text("ALTER TABLE skland_user ADD COLUMN binding_group TEXT"))
                logger.info("[Skland] 数据库迁移：添加独立的绑定群记录，旧账号需本人指定通知群")
        _session_maker = async_sessionmaker(_engine, expire_on_commit=False)
        logger.info(f"[Skland] 数据库已初始化: {path}")


async def get_session() -> AsyncSession:
    """获取异步数据库会话（惰性初始化，首次调用时自动创建数据库）"""
    global _session_maker
    if _session_maker is None:
        await init_db()
    return _session_maker()


async def get_user_by_platform(session: AsyncSession, platform_user_id: str) -> SkUser | None:
    """通过跨平台用户ID查询用户"""
    result = await session.execute(
        select(SkUser).where(SkUser.platform_user_id == platform_user_id)
    )
    return result.scalar_one_or_none()


async def get_default_ark_character(session: AsyncSession, user: SkUser) -> Character | None:
    """获取用户默认绑定的明日方舟角色"""
    result = await session.execute(
        select(Character).where(
            Character.id == user.id,
            Character.isdefault == True,
            Character.app_code == "arknights"
        )
    )
    return result.scalar_one_or_none()


async def get_default_ef_character(session: AsyncSession, user: SkUser) -> Character | None:
    """获取用户默认绑定的终末地角色"""
    result = await session.execute(
        select(Character).where(
            Character.id == user.id,
            Character.isdefault == True,
            Character.app_code == "endfield"
        )
    )
    return result.scalar_one_or_none()


async def get_ark_characters(session: AsyncSession, user: SkUser) -> list[Character]:
    """获取用户绑定的所有明日方舟角色"""
    result = await session.execute(
        select(Character).where(
            Character.id == user.id,
            Character.app_code == "arknights"
        )
    )
    return list(result.scalars().all())


async def get_ef_characters(session: AsyncSession, user: SkUser) -> list[Character]:
    """获取用户绑定的所有终末地角色"""
    result = await session.execute(
        select(Character).where(
            Character.id == user.id,
            Character.app_code == "endfield"
        )
    )
    return list(result.scalars().all())


async def get_all_users_with_umos(session: AsyncSession) -> list[SkUser]:
    """获取所有已保存 UMO 的用户（用于定时任务推送）"""
    result = await session.execute(
        select(SkUser).where(SkUser.umo.isnot(None))
    )
    return list(result.scalars().all())


async def get_stamina_alert(session: AsyncSession, user_id: int, char_uid: str, game: str) -> StaminaAlert | None:
    """获取指定角色的预警状态"""
    result = await session.execute(
        select(StaminaAlert).where(
            StaminaAlert.user_id == user_id,
            StaminaAlert.char_uid == char_uid,
            StaminaAlert.game == game
        )
    )
    return result.scalar_one_or_none()


async def get_gacha_records(session: AsyncSession, user_id: int, char_uid: str) -> list[GachaRecord]:
    """获取指定用户和角色的所有抽卡记录"""
    result = await session.execute(
        select(GachaRecord).where(
            GachaRecord.uid == user_id,
            GachaRecord.char_uid == char_uid,
        )
    )
    return list(result.scalars().all())


async def delete_gacha_records(session: AsyncSession, char_pk_id: int, char_uid: str):
    """删除指定角色的抽卡记录"""
    from sqlalchemy import delete
    await session.execute(
        delete(GachaRecord).where(
            GachaRecord.char_pk_id == char_pk_id,
            GachaRecord.char_uid == char_uid,
        )
    )


async def get_or_create_stamina_alert(session: AsyncSession, user_id: int, char_uid: str, game: str) -> StaminaAlert:
    """获取或创建角色的预警状态记录"""
    alert = await get_stamina_alert(session, user_id, char_uid, game)
    if alert is None:
        alert = StaminaAlert(user_id=user_id, char_uid=char_uid, game=game)
        session.add(alert)
        await session.flush()
    return alert
