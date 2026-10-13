"""The window, driven offscreen against the recorded responses."""

from __future__ import annotations

import os
import time

import pytest
from PySide6.QtWidgets import QApplication

from app.analysis import service
from app.external.aion2exchange import Aion2ExchangeSource
from app.external.scheduler import refresh_once
from app.ui.main_window import MainWindow, style_charts
from tests.conftest import ODYLE, SERVER


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    style_charts(app)
    return app


def pump(qapp, until, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not until():
        assert time.monotonic() < deadline, "timed out waiting for the UI"
        qapp.processEvents()
        time.sleep(0.01)


@pytest.fixture
def window(qapp, tmp_path, api):
    win = MainWindow(
        tmp_path / "market.db",
        lambda db: Aion2ExchangeSource(db, min_interval_s=0, sleep=lambda s: None),
    )
    win.show()
    pump(qapp, lambda: win.state.result is not None)
    yield win
    win.close()


def test_window_fills_from_a_refresh(window, qapp):
    assert window.state.server_id == SERVER
    assert window.items.table.model_.rowCount() == 20
    assert window.crafting.api.model_.rowCount() == 19
    assert window.status.server.currentData() == SERVER
    assert window.status.health.text() == "API healthy"
    assert "Listings:" in window.status.ages.text()
    assert not window.banner.isVisible()
    assert "API health: OK" in window.trust.text.toPlainText()
    assert any(o.kind == "Mover" for o in window.state.opportunities)


def test_selecting_an_item_loads_its_charts(window, qapp):
    window.open_item(ODYLE)
    assert window.tabs.currentWidget() is window.items
    assert window.items.title.text() == "Odyle"
    pump(qapp, lambda: window.items.status.text() == "")
    assert len(service.chart_points(window.db, SERVER, ODYLE)) == 142
    assert len(window.items.price_plot.listDataItems()) == 2  # lowest and average
    assert "7-day median" in window.items.stats.text()
    assert "sold in the last 28 days" in window.items.sales_note.text()
    assert "units in" in window.items.depth_note.text()
    for button in window.items._range_group.buttons():
        button.click()  # every range redraws without error

    window.items.watch.click()
    assert window.db.watched_keys() == [ODYLE]
    window.items.notes.setPlainText("buy under 1,100")
    window.items._save_notes()
    row = window.db.conn.execute("SELECT notes FROM items WHERE item_key = ?", (ODYLE,)).fetchone()
    assert row["notes"] == "buy under 1,100"

    shot = os.environ.get("UI_SCREENSHOT_DIR")
    if shot:
        qapp.processEvents()
        window.grab().save(os.path.join(shot, "items.png"))
        for index, name in enumerate(["opportunities", "items", "crafting", "flips", "trust", "settings"]):
            window.tabs.setCurrentIndex(index)
            qapp.processEvents()
            window.grab().save(os.path.join(shot, f"{name}.png"))


def test_search_filters_the_catalog(window):
    window.items.search.setText("odyle")
    names = [
        window.items.table.proxy.index(r, 0).data()
        for r in range(window.items.table.proxy.rowCount())
    ]
    assert names and all("odyle" in n.lower() for n in names)


def test_dismissing_an_opportunity_hides_it(window):
    before = len(window.state.opportunities)
    window.opportunities.table.selectRow(0)
    first = window.opportunities.table.selected()
    window.opportunities._dismiss()
    assert len(window.state.opportunities) == before - 1
    assert first.id not in {o.id for o in window.state.opportunities}
    window.state.restore_dismissed()
    assert len(window.state.opportunities) == before


def test_settings_save_and_validate(window):
    field = window.settings._widgets["sales_tax_pct"]
    field.setText("abc")
    window.settings._save()
    assert "must be a number" in window.settings.message.text()
    assert window.db.get_setting("sales_tax_pct") == "10"
    field.setText("8")
    window.settings._save()
    assert window.db.get_setting("sales_tax_pct") == "8"


def test_recipe_added_by_hand_is_valued(window, qapp):
    keys = [v.item_key for v in window.state.views if v.item_key != ODYLE][:1]
    window.db.save_recipe(
        {"output_key": ODYLE, "output_qty": 10, "success_rate": 0.9, "combo_probability": 0.0,
         "kina_cost": 100, "craft_seconds": 60},
        [(keys[0], 1, 500)],
    )
    window.state.reload()
    [row] = window.state.recipes
    # 0.9 x net(10 x 1,184) - 500 (NPC price beats the market) - 100 fee
    assert row.materials == 500
    assert row.ev == pytest.approx(0.9 * 0.9 * 11840 - 600)
    assert row.per_hour == pytest.approx(row.ev * 60)
    assert window.crafting.mine.model_.rowCount() == 1


def test_api_down_shows_the_banner(window, qapp, api):
    import httpx

    from tests.conftest import break_api

    break_api(api, httpx.ConnectError("offline"))
    seen = window.state.result
    window.worker.request_refresh(force=True)
    pump(qapp, lambda: window.state.result is not seen)
    assert window.banner.isVisibleTo(window) and "cannot be reached" in window.banner.text()
    assert window.status.health.text() == "API unreachable"
    assert window.items.table.model_.rowCount() == 20  # stored data is still shown


def test_demand_reference_server_is_fetched_and_used(db, source, clock):
    db.set_setting("server_id", 12107)
    result = refresh_once(db, source, clock)
    assert db.demand_server_ids(12107) == [11304]
    assert result.reference_updated == len(db.reference_server_ids(12107)) + 1
    odyle = next(v for v in service.item_views(db, 12107, clock.at) if v.item_key == ODYLE)
    assert odyle.demand.servers == 1 and odyle.demand.sold_avg == 1999
    assert odyle.demand.units_per_day == pytest.approx(1960380 / 28)
