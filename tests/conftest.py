"""Shared fixtures. Tests replay recorded responses with respx and never hit the live API."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # UI tests never open a real window
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.db.repo import Database
from app.external.aion2exchange import BASE_URL, Aion2ExchangeSource

FIXTURES = Path(__file__).parent / "fixtures" / "api"

# The recordings were made at about this time; tests pin the clock just after.
RECORDED_AT = datetime(2026, 10, 13, 2, 45, tzinfo=UTC)
SERVER = 11304
ODYLE = 610610013

ROUTES = [
    (r"/api/servers$", "servers"),
    (r"/api/health$", "health"),
    (r"/api/collection$", "collection"),
    (r"/api/categories$", "categories"),
    (r"/api/overview$", "overview"),
    (r"/api/items$", "items"),
    (r"/api/items/\d+/prices$", "prices"),
    (r"/api/items/\d+/depth$", "depth"),
    (r"/api/items/\d+/depth/ladder$", "depth_ladder"),
    (r"/api/items/\d+/volume$", "volume"),
    (r"/api/items/\d+/observations$", "observations"),
    (r"/api/crafts$", "crafts"),
    (r"/api/crafts/model$", "crafts_model"),
    (r"/api/crafts/\d+$", "craft_detail"),
]


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Clock:
    def __init__(self, at: datetime = RECORDED_AT):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "market.db")
    yield database
    database.close()


@pytest.fixture
def api():
    """Every endpoint answers with its recorded response; tests override routes to break them."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        for pattern, name in ROUTES:
            router.get(path__regex=pattern, name=name).mock(
                return_value=httpx.Response(200, json=load(name))
            )
        yield router


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def source(db, api, clock, sleeps) -> Aion2ExchangeSource:
    src = Aion2ExchangeSource(db, min_interval_s=0, sleep=sleeps.append, now=clock)
    yield src
    src.close()


def break_api(router, response: httpx.Response | Exception) -> None:
    """Make every endpoint fail the same way."""
    for route in router.routes:
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
