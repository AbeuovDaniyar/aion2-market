"""Real item icons: dictionary lookup, download, caching and the UI fallback."""

from __future__ import annotations

import struct
import zlib

import httpx
import pytest
import respx

from app.external.icons import DICTIONARY_URL, IconFetcher, icon_files
from app.external.scheduler import refresh_once
from tests.conftest import ODYLE, SERVER



def _png() -> bytes:
    """A valid 1x1 red PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


PNG = _png()
IMAGE = "https://assets.playnccdn.com/static-aion2-gamedata/resources/Icon_Item_Gather_Od_A_c_001.png"


@pytest.fixture
def filled(db, source, clock):
    refresh_once(db, source, clock)
    return db


@pytest.fixture
def nc(api):
    """NCSOFT's dictionary and image CDN, mocked alongside the market API."""

    def lookup(request: httpx.Request) -> httpx.Response:
        name = request.url.params["searchKeyword"]
        assert request.url.params["exact"] == "true"
        if name == "Odyle":
            contents = [
                {"id": 999, "name": "Odyle (Bound)", "image": IMAGE.replace("_c_", "_x_"), "grade": "Common"},
                {"id": ODYLE, "name": "Odyle", "image": IMAGE, "grade": "Common"},
            ]
        else:
            contents = []
        return httpx.Response(200, json={"contents": contents, "pagination": {}})

    api.get(DICTIONARY_URL, name="dictionary").mock(side_effect=lookup)
    api.get(host="assets.playnccdn.com", path__regex=r"/static-aion2-gamedata/.*", name="image").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    return api


def fetcher(db, tmp_path) -> IconFetcher:
    return IconFetcher(db, tmp_path / "icons", min_interval_s=0, sleep=lambda s: None)


def test_icon_is_looked_up_by_exact_name_and_saved(filled, nc, tmp_path):
    db = filled
    db.set_watch(ODYLE)  # watched items go first
    icons = fetcher(db, tmp_path)
    assert icons.pending(SERVER, 1) == [(ODYLE, "Odyle")]
    assert icons.fetch_some(SERVER, 1) == 1
    assert icon_files(db) == {ODYLE: ("Icon_Item_Gather_Od_A_c_001.png", "Common")}
    assert (tmp_path / "icons" / "Icon_Item_Gather_Od_A_c_001.png").read_bytes() == PNG
    assert (ODYLE, "Odyle") not in icons.pending(SERVER, 100)


def test_each_item_is_asked_for_only_once(filled, nc, tmp_path):
    icons = fetcher(filled, tmp_path)
    total = 0
    while (done := icons.fetch_some(SERVER, 50)) > 0:
        total += done
    assert total == 20 and nc["dictionary"].call_count == 20
    assert nc["image"].call_count == 1  # 19 items had no dictionary entry
    assert icons.fetch_some(SERVER, 50) == 0  # nothing left, found or not
    assert nc["dictionary"].call_count == 20


def test_images_only_come_from_the_official_cdn(filled, nc, tmp_path):
    nc["dictionary"].mock(
        return_value=httpx.Response(
            200,
            json={"contents": [{"id": ODYLE, "name": "Odyle", "image": "https://evil.example/x.png"}]},
        )
    )
    filled.set_watch(ODYLE)
    icons = fetcher(filled, tmp_path)
    icons.fetch_some(SERVER, 1)
    assert icon_files(filled) == {} and nc["image"].call_count == 0
    assert icons._download("https://assets.playnccdn.com/a/../../evil.exe") is None
    assert icons._download("http://assets.playnccdn.com/a/x.png") is None


def test_failures_stop_the_fetcher_without_marking_items(filled, nc, tmp_path):
    nc["dictionary"].mock(side_effect=httpx.ConnectError("offline"))
    icons = fetcher(filled, tmp_path)
    assert icons.fetch_some(SERVER, 50) == 0
    assert nc["dictionary"].call_count == 5 and not icons.enabled()
    assert len(icons.pending(SERVER, 100)) == 20  # they will be tried again next start


def test_setting_turns_icons_off(filled, nc, tmp_path):
    filled.set_setting("real_item_icons", 0)
    assert fetcher(filled, tmp_path).fetch_some(SERVER, 5) == 0
    assert nc["dictionary"].call_count == 0


def test_window_shows_real_icons_when_downloaded(tmp_path, api, nc):
    from PySide6.QtWidgets import QApplication

    from app.external.aion2exchange import Aion2ExchangeSource
    from app.ui import icons as ui_icons
    from app.ui.main_window import MainWindow, style_charts
    from tests.conftest import shift_to_now
    from tests.test_ui import pump

    qapp = QApplication.instance() or QApplication([])
    style_charts(qapp)
    shift_to_now(api)
    window = MainWindow(
        tmp_path / "market.db",
        lambda db: Aion2ExchangeSource(db, min_interval_s=0, sleep=lambda s: None),
        icon_fetcher_factory=lambda db: IconFetcher(db, tmp_path / "icons", min_interval_s=0, sleep=lambda s: None),
    )
    try:
        pump(qapp, lambda: ODYLE in ui_icons._files and len(icon_files(window.db)) == 1)
        assert ui_icons.grade_of(ODYLE) == "Common"
        real = ui_icons.icon_for(ODYLE, "materials", "Gathered resources")
        fallback = ui_icons.item_icon("materials", "Gathered resources")
        assert real.cacheKey() != fallback.cacheKey()
        other = next(v for v in window.state.views if v.item_key != ODYLE)
        assert ui_icons.icon_for(other.item_key, other.section, other.category_name).cacheKey() == (
            ui_icons.item_icon(other.section, other.category_name).cacheKey()
        )
    finally:
        window.close()
        ui_icons.set_icon_files(tmp_path, {})


assert respx  # imported for the api fixture's router type
