from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.enums import GamePhase, GameStatus
from app.game_engine import GameEngine
from app.group_settings import GroupSettingsManager
from app.keyboards import owner_panel_keyboard, profile_dashboard_keyboard, settings_main_keyboard
from app.models import BotSetting, Game, User


@pytest.fixture
def settings() -> Settings:
    return Settings(BOT_TOKEN="test", BOT_USERNAME="testbot", ADMIN_IDS="1")


@pytest_asyncio.fixture
async def session_factory():
    db_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    yield factory
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_registration_deadline_waits_for_admin_without_group_notice(session_factory, settings):
    engine = GameEngine(settings, session_factory)
    async with session_factory() as session:
        game = Game(
            chat_id=-100123,
            creator_telegram_id=99,
            status=GameStatus.REGISTRATION.value,
            phase=GamePhase.REGISTRATION.value,
            active_key=1,
            registration_ends_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        session.add(game)
        await session.commit()
        game_id = game.id

    bot = MagicMock()
    bot.send_message = AsyncMock()
    await engine.close_registration(bot, game_id)

    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game.status == GameStatus.REGISTRATION.value
        assert game.registration_ends_at is None
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_keeps_registration_but_stops_interrupted_active_game(session_factory, settings):
    engine = GameEngine(settings, session_factory)
    async with session_factory() as session:
        registration = Game(
            chat_id=-100124,
            creator_telegram_id=99,
            status=GameStatus.REGISTRATION.value,
            phase=GamePhase.REGISTRATION.value,
            active_key=1,
            registration_ends_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        active = Game(
            chat_id=-100125,
            creator_telegram_id=99,
            status=GameStatus.ACTIVE.value,
            phase=GamePhase.NIGHT.value,
            active_key=1,
        )
        session.add_all([registration, active])
        await session.commit()
        registration_id, active_id = registration.id, active.id

    await engine.cleanup_stale_games_on_startup()

    async with session_factory() as session:
        registration = await session.get(Game, registration_id)
        active = await session.get(Game, active_id)
        assert registration.status == GameStatus.REGISTRATION.value
        assert registration.registration_ends_at is None
        assert active.status == GameStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_admin_time_value_overrides_old_group_time_and_is_global(session_factory, settings):
    engine = GameEngine(settings, session_factory)
    manager = GroupSettingsManager(session_factory)
    await manager.set_time_setting(-100126, "night_time", 180)

    assert await engine.group_timeout(-100126, "night_timeout") == settings.night_timeout
    ok, _ = await engine.set_global_game_time_seconds("night_timeout", 120)

    assert ok is True
    assert await engine.group_timeout(-100126, "night_timeout") == 120
    assert await engine.get_global_game_time_seconds("night_timeout") == 120

    async with session_factory() as session:
        saved = await session.scalar(
            select(BotSetting).where(BotSetting.key == "global_game_time_night_timeout")
        )
        assert saved is not None and saved.value == "120"


@pytest.mark.asyncio
async def test_news_label_is_editable_and_profile_uses_configured_link(session_factory, settings):
    engine = GameEngine(settings, session_factory)
    ok, _ = await engine.set_news_channel_title("My Mafia News")
    assert ok is True
    assert await engine.get_news_channel_title() == "My Mafia News"

    user = User(
        telegram_id=123,
        display_name="Player",
        dollar=5,
        diamonds=2,
        protection=0,
        killer_protection=0,
        vote_protection=0,
        drug_protection=0,
        miner_protection=0,
        mask=0,
        fake_document=0,
        use_protection=True,
        use_killer_protection=True,
        use_vote_protection=True,
        use_drug_protection=True,
        use_miner_protection=True,
        use_mask=True,
        use_fake_document=True,
        wins=1,
        total_games=2,
    )
    dashboard = engine.format_user_dashboard(
        user,
        news_url="https://t.me/new_news",
        news_title=await engine.get_news_channel_title(),
    )
    assert "My Mafia News" in dashboard
    assert "@WorldMafiaNews" not in dashboard


def test_profile_keyboard_matches_reference_layout_and_group_times_are_owner_only(settings):
    collapsed = profile_dashboard_keyboard(
        settings,
        news_url="https://t.me/example_news",
        news_label="My News",
    )
    collapsed_callbacks = [
        button.callback_data
        for row in collapsed.inline_keyboard
        for button in row
    ]
    assert collapsed.inline_keyboard[0][0].callback_data == "shop:open"
    assert collapsed.inline_keyboard[1][0].callback_data == "profile:inventory:show"
    assert not any(value and value.startswith("invtoggle:") for value in collapsed_callbacks)

    expanded = profile_dashboard_keyboard(settings, show_inventory=True)
    assert len(expanded.inline_keyboard[2]) == 4
    assert len(expanded.inline_keyboard[3]) == 3

    owner_callbacks = {
        button.callback_data
        for row in owner_panel_keyboard("super_admin").inline_keyboard
        for button in row
    }
    group_admin_callbacks = {
        button.callback_data
        for row in settings_main_keyboard().inline_keyboard
        for button in row
    }
    assert "owner:game_times" in owner_callbacks
    assert "settings:times" not in group_admin_callbacks
    assert "owner:game_times" not in {
        button.callback_data
        for row in owner_panel_keyboard("moderator").inline_keyboard
        for button in row
    }
