import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ..models.base import Base

logger = logging.getLogger(__name__)

# Global variables for database connection
_engine = None
_session_factory = None


def get_database_url() -> str:
    """Get database URL from environment or use default"""
    import os

    return os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/telegram_agent.db")


async def _migrate_split_settings(engine) -> None:
    """Copy existing user_settings rows into the 4 context-specific tables.

    Idempotent: uses INSERT OR IGNORE so rows already present are skipped.
    Only runs when user_settings table exists and has rows.
    """
    async with engine.begin() as conn:
        # Check if user_settings table exists
        result = await conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='user_settings'"
            )
        )
        if not result.fetchone():
            return

        # Check if there are any rows to migrate
        count = (
            await conn.execute(text("SELECT COUNT(*) FROM user_settings"))
        ).scalar()
        if not count:
            return

        # Voice settings
        try:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO voice_settings"
                    " (user_id, voice_enabled, voice_model,"
                    "  emotion_style, response_mode)"
                    " SELECT user_id, voice_enabled, voice_model,"
                    "  emotion_style, response_mode"
                    " FROM user_settings"
                )
            )
            logger.info("Migrated voice_settings from user_settings")
        except Exception as e:
            logger.warning(f"voice_settings migration skipped: {e}")

        # Accountability profiles
        try:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO accountability_profiles"
                    " (user_id, partner_personality, partner_voice_override,"
                    "  check_in_time, struggle_threshold, celebration_style,"
                    "  auto_adjust_personality, check_in_times,"
                    "  reminder_style, timezone)"
                    " SELECT user_id, partner_personality,"
                    "  partner_voice_override, check_in_time,"
                    "  struggle_threshold, celebration_style,"
                    "  auto_adjust_personality, check_in_times,"
                    "  reminder_style, timezone"
                    " FROM user_settings"
                )
            )
            logger.info("Migrated accountability_profiles from user_settings")
        except Exception as e:
            logger.warning(f"accountability_profiles migration skipped: {e}")

        # Privacy settings
        try:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO privacy_settings"
                    " (user_id, privacy_level, data_retention,"
                    "  health_data_consent)"
                    " SELECT user_id, privacy_level, data_retention,"
                    "  health_data_consent"
                    " FROM user_settings"
                )
            )
            logger.info("Migrated privacy_settings from user_settings")
        except Exception as e:
            logger.warning(f"privacy_settings migration skipped: {e}")

        # Life weeks settings
        try:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO life_weeks_settings"
                    " (user_id, date_of_birth, life_weeks_enabled,"
                    "  life_weeks_day, life_weeks_time,"
                    "  life_weeks_reply_destination,"
                    "  life_weeks_custom_path)"
                    " SELECT user_id, date_of_birth, life_weeks_enabled,"
                    "  life_weeks_day, life_weeks_time,"
                    "  life_weeks_reply_destination,"
                    "  life_weeks_custom_path"
                    " FROM user_settings"
                )
            )
            logger.info("Migrated life_weeks_settings from user_settings")
        except Exception as e:
            logger.warning(f"life_weeks_settings migration skipped: {e}")


async def init_database() -> None:
    """Initialize database connection and create tables"""
    global _engine, _session_factory

    database_url = get_database_url()
    logger.info(f"Initializing database: {database_url}")

    # Create async engine
    # For SQLite: enable WAL mode and set busy_timeout to prevent deadlocks
    connect_args = {}
    if "sqlite" in database_url:
        connect_args = {
            "timeout": 10.0,  # Increase busy timeout from default 5s to 10s
        }

    _engine = create_async_engine(
        database_url,
        echo=False,  # Set to True for SQL debugging
        poolclass=NullPool if "sqlite" in database_url else None,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    # Create session factory
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )

    # Create all tables
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created/verified")

    # Enable WAL mode for SQLite (allows concurrent reads during writes)
    if "sqlite" in database_url:
        async with _engine.begin() as conn:
            result = await conn.execute(text("PRAGMA journal_mode=WAL"))
            mode = result.scalar()
            logger.info(f"Enabled SQLite WAL mode: {mode}")
            # Also increase busy timeout at the connection level
            await conn.execute(text("PRAGMA busy_timeout = 10000"))

    # Migrate: add tts_provider column if missing
    async with _engine.begin() as conn:
        try:
            await conn.execute(
                text(
                    "ALTER TABLE chats ADD COLUMN tts_provider VARCHAR(20) NOT NULL DEFAULT ''"
                )
            )
            logger.info("Added tts_provider column to chats table")
        except Exception:
            pass  # already exists

    # Migrate: add whisper_use_locale column if missing
    async with _engine.begin() as conn:
        try:
            await conn.execute(
                text(
                    "ALTER TABLE chats ADD COLUMN whisper_use_locale BOOLEAN NOT NULL DEFAULT 0"
                )
            )
            logger.info("Added whisper_use_locale column to chats table")
        except Exception:
            pass  # already exists

    # Migrate: add thinking_effort column if missing (Opus 4.6 adaptive thinking)
    async with _engine.begin() as conn:
        try:
            await conn.execute(
                text(
                    "ALTER TABLE chats ADD COLUMN thinking_effort VARCHAR(10) DEFAULT 'medium'"
                )
            )
            logger.info("Added thinking_effort column to chats table")
        except Exception:
            pass  # already exists

    # Migrate: add clean_responses column if missing
    async with _engine.begin() as conn:
        try:
            await conn.execute(
                text(
                    "ALTER TABLE chats ADD COLUMN clean_responses BOOLEAN NOT NULL DEFAULT 0"
                )
            )
            logger.info("Added clean_responses column to chats table")
        except Exception:
            pass  # already exists

    # Migrate: add life weeks columns if missing
    async with _engine.begin() as conn:
        columns = [
            ("date_of_birth", "VARCHAR(10)"),
            ("life_weeks_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            ("life_weeks_day", "INTEGER"),
            ("life_weeks_time", "VARCHAR(10) NOT NULL DEFAULT '09:00'"),
            (
                "life_weeks_reply_destination",
                "VARCHAR(50) NOT NULL DEFAULT 'daily_note'",
            ),
            ("life_weeks_custom_path", "VARCHAR(255)"),
        ]
        for col_name, col_type in columns:
            try:
                await conn.execute(
                    text(f"ALTER TABLE user_settings ADD COLUMN {col_name} {col_type}")
                )
                logger.info(f"Added {col_name} column to user_settings table")
            except Exception:
                pass  # already exists

    # Migrate: add accountability partner columns if missing
    async with _engine.begin() as conn:
        partner_columns = [
            ("partner_personality", "VARCHAR(50) NOT NULL DEFAULT 'supportive'"),
            ("partner_voice_override", "VARCHAR(50)"),
            ("check_in_time", "VARCHAR(10) NOT NULL DEFAULT '19:00'"),
            ("struggle_threshold", "INTEGER NOT NULL DEFAULT 3"),
            ("celebration_style", "VARCHAR(50) NOT NULL DEFAULT 'moderate'"),
            ("auto_adjust_personality", "BOOLEAN NOT NULL DEFAULT 0"),
        ]
        for col_name, col_type in partner_columns:
            try:
                await conn.execute(
                    text(f"ALTER TABLE user_settings ADD COLUMN {col_name} {col_type}")
                )
                logger.info(f"Added {col_name} column to user_settings table")
            except Exception:
                pass  # already exists

    # Migrate: add topics_sessions_enabled column if missing
    async with _engine.begin() as conn:
        try:
            await conn.execute(
                text(
                    "ALTER TABLE chats ADD COLUMN topics_sessions_enabled BOOLEAN NOT NULL DEFAULT 0"
                )
            )
            logger.info("Added topics_sessions_enabled column to chats table")
        except Exception:
            pass  # already exists

    # Migrate: copy user_settings rows into context-specific tables (#222)
    await _migrate_split_settings(_engine)

    # Drop orphan tables left by test model classes registered in Base.metadata
    # (test models inheriting from Base pollute create_all; this cleans up production)
    _orphan_tables = [
        "test_plugin_data",
        "test_mixin_data",
        "another_test_table",
        "model1_table",
        "model2_table",
        "lifecycle_test_table",
        "special_table_123",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ]
    async with _engine.begin() as conn:
        for table_name in _orphan_tables:
            try:
                await conn.execute(text(f"DROP TABLE IF EXISTS [{table_name}]"))
            except Exception:
                pass
        logger.debug("Orphan table cleanup complete")

    # Initialize vector database support
    try:
        from ..core.vector_db import get_vector_db

        vector_db = get_vector_db()
        await vector_db.initialize_vector_support()
        logger.info("Vector database support initialized")
    except Exception as e:
        logger.warning(
            f"Vector database initialization failed (continuing without vector search): {e}"
        )


async def close_database() -> None:
    """Close database connection"""
    if _engine:
        await _engine.dispose()
        logger.info("Database connection closed")


def get_engine():
    """Get the database engine (must be initialized first)."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _engine


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session context manager"""
    if not _session_factory:
        await init_database()

    async with _session_factory() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.error(f"Database session error: {e}")
            raise
        finally:
            await session.close()


async def get_db_session_dependency() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database session"""
    async with get_db_session() as session:
        yield session


# Utility functions for common database operations
async def health_check(timeout_seconds: float = 5.0) -> bool:
    """Check if database is accessible with timeout protection."""
    logger.info("Performing database health check")
    try:
        async with asyncio.timeout(timeout_seconds):
            if not _session_factory:
                logger.warning(
                    "No session factory available, attempting to initialize database"
                )
                await init_database()
                if not _session_factory:
                    logger.error("Failed to initialize database session factory")
                    return False
                logger.info("Database initialized during health check")

            logger.debug("Opening database session for health check")
            async with get_db_session() as session:
                # Simple query to test connection
                logger.debug("Executing test query")
                result = await session.execute(text("SELECT 1"))
                value = result.scalar()
                logger.debug(f"Test query result: {value}")
                is_healthy = value == 1

                if is_healthy:
                    logger.info("Database health check successful")
                else:
                    logger.warning(
                        f"Database health check query returned unexpected value: {value}"
                    )

                return is_healthy
    except asyncio.TimeoutError:
        logger.error(f"Database health check timed out after {timeout_seconds}s")
        return False
    except Exception as e:
        logger.error(f"Database health check failed: {e}", exc_info=True)
        # Log more specific error types for better diagnostics
        if "connection" in str(e).lower():
            logger.error("Connection error detected - database may be unreachable")
        elif "timeout" in str(e).lower():
            logger.error("Timeout error detected - database may be overloaded")
        elif "authentication" in str(e).lower() or "permission" in str(e).lower():
            logger.error("Authentication error detected - check credentials")
        return False


async def get_user_count() -> int:
    """Get total number of users"""
    try:
        async with get_db_session() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM users"))
            return result.scalar() or 0
    except Exception as e:
        logger.error(f"Error getting user count: {e}")
        return 0


async def get_chat_count() -> int:
    """Get total number of chats"""
    try:
        async with get_db_session() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM chats"))
            return result.scalar() or 0
    except Exception as e:
        logger.error(f"Error getting chat count: {e}")
        return 0


async def get_image_count() -> int:
    """Get total number of processed images"""
    try:
        async with get_db_session() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM images"))
            return result.scalar() or 0
    except Exception as e:
        logger.error(f"Error getting image count: {e}")
        return 0


async def get_embedding_stats() -> dict:
    """Get statistics about embeddings in the database"""
    try:
        async with get_db_session() as session:
            from sqlalchemy import func, select

            from ..models.image import Image

            # Total completed images
            total_result = await session.execute(
                select(func.count(Image.id)).where(
                    Image.processing_status == "completed"
                )
            )
            total_images = total_result.scalar() or 0

            # Images with embeddings
            with_embeddings_result = await session.execute(
                select(func.count(Image.id)).where(
                    Image.processing_status == "completed", Image.embedding.isnot(None)
                )
            )
            with_embeddings = with_embeddings_result.scalar() or 0

            # Images without embeddings
            without_embeddings = total_images - with_embeddings

            # Coverage percentage
            coverage = (with_embeddings / total_images * 100) if total_images > 0 else 0

            return {
                "total_images": total_images,
                "with_embeddings": with_embeddings,
                "without_embeddings": without_embeddings,
                "coverage_percentage": coverage,
            }

    except Exception as e:
        logger.error(f"Error getting embedding stats: {e}")
        return {
            "total_images": 0,
            "with_embeddings": 0,
            "without_embeddings": 0,
            "coverage_percentage": 0,
        }


async def get_images_without_embeddings_count(user_id: Optional[int] = None) -> int:
    """Get count of images without embeddings that have accessible files"""
    try:
        async with get_db_session() as session:
            from sqlalchemy import func, select

            from ..models.chat import Chat
            from ..models.image import Image

            query = select(func.count(Image.id)).where(
                Image.embedding.is_(None), Image.processing_status == "completed"
            )

            if user_id:
                query = query.join(Image.chat).where(Chat.user_id == user_id)

            result = await session.execute(query)
            return result.scalar() or 0

    except Exception as e:
        logger.error(f"Error getting images without embeddings count: {e}")
        return 0


async def get_user_by_telegram_id(
    session: AsyncSession,
    telegram_user_id: int,
    user_repo: Optional[object] = None,
) -> Optional["User"]:  # noqa: F821
    """Get user by Telegram user ID.

    Args:
        session: SQLAlchemy async session (used when user_repo is None).
        telegram_user_id: The Telegram-assigned user ID.
        user_repo: Optional UserRepository instance. When provided, the
            session parameter is ignored and the repository is used instead.
    """
    try:
        if user_repo is not None:
            return await user_repo.get_by_telegram_id(telegram_user_id)

        from sqlalchemy import select

        from ..models.user import User

        result = await session.execute(
            select(User).where(User.user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()
    except Exception as e:
        logger.error(f"Error getting user by telegram_id {telegram_user_id}: {e}")
        return None


async def get_chat_by_telegram_id(
    session: AsyncSession,
    telegram_chat_id: int,
    chat_repo: Optional[object] = None,
) -> Optional["Chat"]:  # noqa: F821
    """Get chat by Telegram chat ID.

    Args:
        session: SQLAlchemy async session (used when chat_repo is None).
        telegram_chat_id: The Telegram-assigned chat ID.
        chat_repo: Optional ChatRepository instance. When provided, the
            session parameter is ignored and the repository is used instead.
    """
    try:
        if chat_repo is not None:
            return await chat_repo.get_by_telegram_id(telegram_chat_id)

        from sqlalchemy import select

        from ..models.chat import Chat

        result = await session.execute(
            select(Chat).where(Chat.chat_id == telegram_chat_id)
        )
        return result.scalar_one_or_none()
    except Exception as e:
        logger.error(f"Error getting chat by telegram_chat_id {telegram_chat_id}: {e}")
        return None
