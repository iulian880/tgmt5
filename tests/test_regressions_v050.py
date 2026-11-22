"""Regression & audit tests for v0.5.0 (full codebase audit & bug-testing session)."""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from tgmt5.config import AppConfig, MT5Settings, RiskSettings, SourceConfig
from tgmt5.domain.models import (
    Direction, ExecutionMode, ManagementType, MessageEventType, MessageKind, SignalStatus,
)
from tgmt5.execution.legs import LegState
from tgmt5.execution.management import close_leg, modify_leg_sl
from tgmt5.execution.recovery import recover
from tgmt5.logging_setup import RedactingFormatter, register_secret
from tgmt5.mt5.interface import (
    ACCOUNT_TRADE_MODE_REAL, ConnectParams, GatewayError, Position, Retcode, TradeRequest,
)
from tgmt5.mt5.real import RealMT5Gateway
from tgmt5.mt5.sim import FakeMT5Gateway, fx_spec, gold_spec
from tgmt5.mt5.symbols import SymbolResolver
from tgmt5.parsing.parser import SignalParser
from tgmt5.persistence.db import Database
from tgmt5.persistence.models import ExecutionRow, SignalRow
from tgmt5.risk.engine import RiskEngine, TradeProposal, position_open_risk
from tgmt5.risk.preview import ExampleSignal, build_preview
from tgmt5.secrets import MemoryKeyring, SecretStore
from tgmt5.services.app import AppService, ReadOnlyGateway
from tgmt5.telegram.client import FakeTelegramSource, TelethonSource

from tests.conftest import SOURCE, Harness, make_broker

P = SignalParser()
GOLD3 = "BUY XAUUSD NOW\nSL: 2650\nTP1: 2670\nTP2: 2680\nTP3: 2690"


# ============================================================ parser safety
@pytest.mark.parametrize("text", [
    "BUY EURUSD NOW SL 0 TP 1.0900",
    "BUY EURUSD @ 0 SL 1.0800 TP 1.0900",
    "BUY EURUSD 0-0.01 SL 0.005 TP 0.02",
    "BUY XAUUSD NOW SL 0.0 TP 2690",
])
def test_zero_price_or_sl_never_crashes_parser(text):
    r = P.parse(text)
    assert r.kind is MessageKind.AMBIGUOUS


def test_tp_inside_entry_zone_is_ambiguous():
    r = P.parse("BUY XAUUSD 2650-2660\nSL 2640\nTP 2656")
    assert r.kind is MessageKind.AMBIGUOUS
    assert any("wrong side" in why for why in r.reasons)


def test_double_space_buy_stop_not_misparsed_as_stop_loss():
    r = P.parse("BUY  STOP XAUUSD 2670\nSL 2655\nTP 2695")
    assert r.kind is MessageKind.NEW_SIGNAL
    assert r.signal.entry_price == 2670
    assert r.signal.stop_loss == 2655
    # Without SL, double-space BUY  STOP must NOT treat 2670 as stop loss
    r2 = P.parse("BUY  STOP XAUUSD 2670\nTP 2695")
    assert r2.kind is MessageKind.AMBIGUOUS


@pytest.mark.parametrize("text", [
    "Move SL to 1,0850",
    "New SL 2650,50",
    "Move TP to 1,0950",
    "Set SL to 0",
    "Update TP to 0",
])
def test_management_bad_comma_or_zero_goes_to_review(text):
    r = P.parse(text)
    assert r.kind is MessageKind.AMBIGUOUS, (text, r)


@pytest.mark.parametrize("text", [
    "Close 50% if price hits 2670",
    "Take 50% when it reaches 2680",
    "Close half once it breaks 1.0900",
    "Move SL to BE if price hits 2670",
    "Move SL to BE once TP1 hits",
    "Cancel pending if it drops below 2640",
    "Set SL to 2655 after price reaches 2670",
])
def test_conditional_management_commands_never_execute_immediately(text):
    r = P.parse(text)
    assert r.kind is MessageKind.AMBIGUOUS, (text, r)


def test_tp_open_with_full_take_profit_phrase():
    r = P.parse("BUY GOLD 2650\nSL 2640\nTP1 2660\nTAKE PROFIT 2: OPEN")
    assert r.kind is MessageKind.NEW_SIGNAL
    assert r.signal.take_profits == (2660.0,)


# ============================================================ recovery & UNCERTAIN lifecycle
def test_recovery_gateway_error_keeps_signal_uncertain(h, monkeypatch):
    """If broker queries fail during recovery, UNCERTAIN legs MUST NOT become FAILED."""
    h.broker.scripted.append("EXECUTE_THEN_TIMEOUT")
    h.engine._reconcile_leg = lambda s, leg: True
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert SignalStatus(h.signal(sid).status) is SignalStatus.UNCERTAIN

    h2 = Harness(db=h.db, broker=h.broker, cfg=h.cfg)
    # Broker answers initial positions/orders, then fails inside _reconcile_leg
    orig_pos = h2.broker.positions
    calls = {"n": 0}

    def fail_on_reconcile(symbol=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise GatewayError("IPC timeout during reconcile")
        return orig_pos(symbol)

    monkeypatch.setattr(h2.broker, "positions", fail_on_reconcile)
    rep = recover(h2.engine)
    assert rep.resolved_uncertain == []
    assert SignalStatus(h2.signal(sid).status) is SignalStatus.UNCERTAIN
    assert h2.signal(sid).executions[0].state == LegState.UNCERTAIN.value

    # Once broker recovers, periodic tick reconciles the leg AND restores signal status
    monkeypatch.setattr(h2.broker, "positions", orig_pos)
    h2.engine.on_tick()
    sig = h2.signal(sid)
    assert SignalStatus(sig.status) is SignalStatus.EXECUTED
    assert sig.executions[0].state == LegState.OPEN.value


def test_recovery_restores_pending_order_as_pending_placed(h):
    h.broker.scripted.append("EXECUTE_THEN_TIMEOUT")
    h.engine._reconcile_leg = lambda s, leg: True
    sid = h.send("BUY LIMIT XAUUSD 2655 SL 2645 TP 2675")
    assert SignalStatus(h.signal(sid).status) is SignalStatus.UNCERTAIN
    h2 = Harness(db=h.db, broker=h.broker, cfg=h.cfg)
    rep = recover(h2.engine)
    assert rep.resolved_uncertain
    assert SignalStatus(h2.signal(sid).status) is SignalStatus.PENDING_PLACED


# ============================================================ execution safety
def test_edit_of_active_signal_in_monitor_mode_never_modifies_broker(h):
    h.cfg.execution.apply_edits_to_open_trades = True
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690", msg_id=10)
    assert SignalStatus(h.signal(sid).status) is SignalStatus.EXECUTED
    h.engine.set_mode(ExecutionMode.MONITOR, ReadOnlyGateway(h.gw))
    n_req = len(h.broker.requests)
    h.send("BUY XAUUSD NOW SL 2655 TP 2690", msg_id=10, event=MessageEventType.EDIT)
    assert len(h.broker.requests) == n_req
    assert h.broker.positions()[0].sl == 2650


def test_emergency_stop_on_first_leg_aborts_remaining_legs(h):
    h.broker.trade_mode = ACCOUNT_TRADE_MODE_REAL
    sid = h.send(GOLD3)
    sig = h.signal(sid)
    assert h.risk.state.emergency_stop
    assert SignalStatus(sig.status) is SignalStatus.FAILED
    # Only the first leg ever reached the guard; legs 1 & 2 were aborted cleanly
    assert len(h.broker.requests) == 0
    assert all(l.state == LegState.REJECTED.value for l in sig.executions)
    assert any("aborted" in l.broker_comment for l in sig.executions[1:])


def test_paper_trades_do_not_consume_real_hourly_trade_limit():
    from tgmt5.mt5.sim import PaperGateway
    h = Harness()
    h.cfg.risk.max_trades_per_hour = 2
    h.cfg.execution.cross_source_dedup_seconds = 0
    pg = PaperGateway(h.broker, balance=10_000, extra_spread_points=0, slippage_points=0,
                      commission_per_lot=0)
    h.engine.set_mode(ExecutionMode.PAPER, pg)
    for i in range(3):
        h.send(f"BUY XAUUSD NOW SL {2645 + i} TP 2690")
    assert h.risk.state.trade_times == []
    # Switching back to DEMO still has the full hourly quota
    h.engine.set_mode(ExecutionMode.DEMO, h.gw)
    sid = h.send("BUY EURUSD NOW SL 1.0830 TP 1.0900")
    assert SignalStatus(h.signal(sid).status) is SignalStatus.EXECUTED


def test_failed_broker_rejection_does_not_burn_hourly_limit(h):
    h.cfg.risk.max_trades_per_hour = 1
    h.cfg.execution.cross_source_dedup_seconds = 0
    h.broker.scripted.append(Retcode.MARKET_CLOSED)
    s1 = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert SignalStatus(h.signal(s1).status) is SignalStatus.FAILED
    assert h.risk.state.trade_times == []
    s2 = h.send("BUY XAUUSD NOW SL 2649 TP 2690")
    assert SignalStatus(h.signal(s2).status) is SignalStatus.EXECUTED


# ============================================================ management edge cases
def test_close_leg_succeeds_after_manual_partial_close_in_mt5(h):
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690", msg_id=10)
    assert SignalStatus(h.signal(sid).status) is SignalStatus.EXECUTED
    pos = h.broker.positions()[0]
    orig_vol = pos.volume
    assert orig_vol >= 0.04
    # User manually closes half in MT5 terminal outside the app
    h.broker._close(h.broker._positions[pos.ticket], round(orig_vol / 2, 2), 2662.0, "manual partial")
    mid = h.send("Close all", reply_to=10)
    assert SignalStatus(h.signal(mid).status) is SignalStatus.EXECUTED
    assert h.broker.positions() == []


def test_close_leg_handles_gateway_error_after_uncertain_send(h, monkeypatch):
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690", msg_id=10)
    h.broker.scripted.append(Retcode.TIMEOUT)
    orig_pos = h.broker.positions
    calls = {"n": 0}

    def flaky_pos(symbol=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise GatewayError("disconnected during uncertain verify")
        return orig_pos(symbol)

    monkeypatch.setattr(h.broker, "positions", flaky_pos)
    with h.db.session() as s:
        leg = h.signal(sid).executions[0]
        leg = s.get(ExecutionRow, leg.id)
        ok, msg = close_leg(h.engine, s, leg, leg.volume, "test")
    assert not ok and "broker unreachable" in msg


def test_modify_tp_clears_tp_with_zero_and_validates_side(h):
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    with h.db.session() as s:
        leg = s.get(ExecutionRow, h.signal(sid).executions[0].id)
        # Clearing TP with 0.0 works
        ok, _ = modify_leg_sl(h.engine, s, leg, leg.sl, "clear tp", new_tp=0.0)
        assert ok and leg.tp is None
        assert h.broker.positions()[0].tp == 0.0
        # Invalid TP (below current bid for a BUY) is rejected
        ok2, msg2 = modify_leg_sl(h.engine, s, leg, leg.sl, "bad tp", new_tp=2655.0)
        assert not ok2 and "not valid" in msg2
        # Works even when leg.sl is None (no TypeError)
        leg.sl = None
        ok3, _ = modify_leg_sl(h.engine, s, leg, None, "set tp", new_tp=2695.0)
        assert ok3 and leg.tp == 2695.0


def test_modify_tp_instruction_rejects_implausible_price(h):
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690", msg_id=10)
    mid = h.send("New TP 5000", reply_to=10)
    assert SignalStatus(h.signal(mid).status) is SignalStatus.FAILED
    assert "implausible" in h.signal(mid).reject_reason


# ============================================================ MT5 adapter & symbol resolver
def test_real_gateway_resets_clock_offset_on_disconnect_and_converts_expiration():
    OFF1 = 3 * 3600
    sent_payloads = []

    class FakeMT5:
        offset = OFF1

        def initialize(self, **kw):
            return True

        def shutdown(self):
            return True

        def symbols_get(self):
            return [NS(name="EURUSD", visible=True, time=int(time.time() + self.offset - 2))]

        def account_info(self):
            return NS(login=1, server="S", company="C", currency="USD", balance=1000, equity=1000,
                      margin=0, margin_free=1000, leverage=100, trade_mode=0, margin_mode=2,
                      trade_allowed=True, name="n")

        def order_send(self, payload):
            sent_payloads.append(payload)
            return NS(retcode=Retcode.PLACED, order=1, deal=0, volume=0.01, price=1.08,
                      comment="", request_id=1)

    mod = FakeMT5()
    gw = RealMT5Gateway(module=mod)
    gw.connect(ConnectParams())
    assert gw.server_offset_seconds == OFF1

    exp_utc = int(time.time()) + 3600
    gw.send(TradeRequest(action=5, symbol="EURUSD", volume=0.01, type=2, price=1.08,
                         expiration=exp_utc))
    assert sent_payloads[-1]["expiration"] == exp_utc + OFF1

    # Disconnect and reconnect to a UTC+0 broker: old UTC+3 offset must be cleared
    gw.disconnect()
    assert gw.server_offset_seconds == 0.0
    mod.offset = 0
    gw.connect(ConnectParams())
    assert gw.server_offset_seconds == 0.0


def test_symbol_resolver_rejects_currency_suffix_and_supports_mixed_case():
    b = FakeMT5Gateway()
    b.add_symbol(gold_spec("GOLDEUR"), 2400.0, 2400.5)
    b.add_symbol(fx_spec("EURUSD"), 1.085, 1.0851)
    r = SymbolResolver(b)
    # GOLDEUR is Gold vs EUR, NOT Gold vs USD (XAUUSD)
    assert r.resolve("XAUUSD") is None

    b2 = FakeMT5Gateway()
    b2.add_symbol(gold_spec("Gold"), 2660.0, 2660.2)
    r2 = SymbolResolver(b2)
    assert r2.resolve("XAUUSD") == "Gold"


def test_netting_same_side_commission_deducted():
    b = make_broker(hedging=False)
    b.commission_per_lot = 10.0
    b.connect(ConnectParams())
    b.send(TradeRequest(action=1, symbol="EURUSD", volume=0.10, type=0))
    after_first = b.balance
    assert after_first == pytest.approx(10_000 - 0.5)
    b.send(TradeRequest(action=1, symbol="EURUSD", volume=0.10, type=0))
    assert b.balance == pytest.approx(after_first - 0.5)


# ============================================================ risk & preview
def test_position_open_risk_handles_zero_tick_size():
    b = make_broker()
    b.calc_profit_available = False
    spec = b.specs["EURUSD"]
    from dataclasses import replace
    b.specs["EURUSD"] = replace(spec, trade_tick_size=0.0)
    pos = Position(1, "EURUSD", 0, 0.1, 1.085, 1.085, 1.080, 1.095, 0.0, 1, "", 0.0)
    assert position_open_risk(b, pos) == float("inf")


def test_risk_engine_rr_check_handles_zero_sl_distance():
    store = {}
    re_eng = RiskEngine(lambda: RiskSettings(min_risk_reward=1.5),
                        lambda: store.get("x"), lambda v: store.__setitem__("x", v))
    acct = make_broker().account_info()
    # Connect broker to get account_info
    b = make_broker()
    b.connect(ConnectParams())
    acct = b.account_info()
    p = TradeProposal("EURUSD", "EURUSD", Direction.BUY, 1.085, 1.085, (0.01,), 10.0, 20.0, (1.090,))
    dec = re_eng.check(p, acct, [], 0)
    assert not dec.approved


def test_offline_preview_uses_gold_spec_for_xauusd():
    cfg = AppConfig()
    ex = [ExampleSignal("XAUUSD", Direction.BUY, 1, entry=2660.0, stop_loss=2654.0, label="recent")]
    pv = build_preview(None, None, cfg, ex, fallback_balance=10_000)
    row = pv.rows[0]
    assert row.ok
    # $6 stop loss on XAUUSD with $50 risk budget (0.5% of $10,000) -> ~0.08 lots ($48 risk), NOT 100x wrong
    assert row.sl_pips == pytest.approx(60.0)
    assert row.total == pytest.approx(0.08)
    assert row.risk_money == pytest.approx(48.0)


# ============================================================ persistence, logging & config
def test_db_load_config_forces_monitor_and_redacts_event_data(tmp_path):
    db = Database.at_path(tmp_path / "sec.db")
    # Simulate a tampered DB row with mode=LIVE
    cfg = AppConfig()
    raw = cfg.model_dump(mode="json")
    raw["mode"] = "LIVE"
    db.set_setting("app_config", raw)
    loaded = db.load_config()
    assert loaded.mode is ExecutionMode.MONITOR

    register_secret("top-secret-token-999")
    db.log_event("INFO", "test", "ok", {"nested": ["Bearer top-secret-token-999", {"k": "password=abc123"}]})
    ev = db.recent_events(limit=1)[0]
    assert "top-secret-token-999" not in str(ev.data)
    assert "abc123" not in str(ev.data)


def test_redacting_formatter_redacts_exception_tracebacks():
    register_secret("super-secret-pw-777")
    fmt = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("auth failed with super-secret-pw-777")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
        out = fmt.format(rec)
    assert "super-secret-pw-777" not in out
    assert "***" in out


def test_import_json_rejects_non_object():
    with pytest.raises(ValueError, match="object"):
        AppConfig.import_json("[1, 2, 3]")


# ============================================================ telegram listener dedup
async def test_telethon_start_twice_does_not_duplicate_handlers():
    class FakeClient:
        def __init__(self):
            self.handlers = []

        def is_connected(self):
            return True

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return NS(username="tester", first_name="T")

        def add_event_handler(self, fn, ev):
            self.handlers.append((fn, type(ev).__name__))

        def remove_event_handler(self, fn):
            self.handlers = [h for h in self.handlers if h[0] != fn]

    src = TelethonSource(SecretStore(MemoryKeyring()))
    src.client = FakeClient()

    async def noop(msg):
        pass

    await src.start([SOURCE], noop)
    await src.start([SOURCE], noop)
    assert len(src.client.handlers) == 3
    await src.stop()


# ============================================================ service reliability
async def test_service_reconnect_in_monitor_mode_with_expired_pending(tmp_path):
    broker = make_broker()
    tg = FakeTelegramSource()
    svc = AppService(tmp_path, secrets=SecretStore(MemoryKeyring()), telegram=tg,
                     gateway_factory=lambda: broker, db=Database.at_path(tmp_path / "t.db"))
    svc.config = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP")],
                           mt5=MT5Settings(login=broker.login, server=broker.server, auto_connect=False))
    svc.config.mark_all_reviewed()
    await svc.start()
    try:
        await svc.connect_mt5("pw")
        await svc.confirm_automated_execution()
        await svc.set_mode(ExecutionMode.DEMO)
        await tg.push(SOURCE, "BUY LIMIT XAUUSD 2655 SL 2645 TP 2675")
        await svc.queue.join()
        assert len(broker.orders()) == 1
        # Switch to MONITOR mode, expire pending order, simulate disconnect + reconnect
        await svc.set_mode(ExecutionMode.MONITOR)
        with svc.db.session() as s:
            leg = s.query(ExecutionRow).one()
            leg.created_at = leg.created_at - timedelta(days=2)
        broker.connected = False
        await svc.run_mt5(svc._reconnect)
        assert broker.connected
        assert len(broker.orders()) == 0
    finally:
        await svc.stop()


async def test_service_close_modify_guards_and_snapshot_resilience(tmp_path):
    broker = make_broker()
    tg = FakeTelegramSource()
    svc = AppService(tmp_path, secrets=SecretStore(MemoryKeyring()), telegram=tg,
                     gateway_factory=lambda: broker, db=Database.at_path(tmp_path / "t.db"))
    svc.config = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP")],
                           mt5=MT5Settings(login=broker.login, server=broker.server, auto_connect=False))
    svc.config.mark_all_reviewed()
    await svc.start()
    try:
        await svc.connect_mt5("pw")
        await svc.confirm_automated_execution()
        await svc.set_mode(ExecutionMode.DEMO)
        await tg.push(SOURCE, "BUY XAUUSD NOW SL 2650 TP 2690")
        await svc.queue.join()
        with svc.db.session() as s:
            leg_id = s.query(ExecutionRow).one().id

        # In PAPER mode, snapshot still reports real DEMO account as confirmed,
        # and closing a DEMO leg from PAPER mode is refused
        await svc.set_mode(ExecutionMode.PAPER)
        snap = await svc.snapshot()
        assert snap.confirmed is True
        ok, msg = await svc.close_position(leg_id)
        assert not ok and "another mode" in msg

        # Disconnect -> close_position returns clean error instead of AttributeError
        await svc.disconnect_mt5()
        ok2, msg2 = await svc.close_position(leg_id)
        assert not ok2 and "not connected" in msg2

        # Transient GatewayError inside snapshot never crashes the UI poll
        await svc.connect_mt5("pw")
        orig_pos = broker.positions
        broker.positions = lambda symbol=None: (_ for _ in ()).throw(GatewayError("blip"))
        snap2 = await svc.snapshot()
        assert snap2.positions == []
        broker.positions = orig_pos

        # Reset breaker / drawdown through AppService (worker thread)
        svc.risk.state.breaker_tripped = True
        svc.risk.state.consecutive_losses = 5
        await svc.reset_breaker()
        assert not svc.risk.state.breaker_tripped and svc.risk.state.consecutive_losses == 0
        svc.risk.state.drawdown_lockout = True
        eq = await svc.reset_drawdown_lockout()
        assert not svc.risk.state.drawdown_lockout and eq > 0
    finally:
        await svc.stop()


async def test_ui_config_import_and_risk_slider(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from tgmt5.__main__ import build_service
    from tgmt5.ui.main_window import MainWindow

    svc = build_service(tmp_path, sandbox=True)
    win = MainWindow(svc)
    await svc.start()
    try:
        risk_page = win.pages[3]
        # Typing a fine decimal into pct spinbox is NOT snapped to 0.05 increments
        risk_page.pct.setValue(0.33)
        assert risk_page.pct.value() == pytest.approx(0.33)
        # Importing a config updates RiskPage and SourcesPage immediately
        imported = AppConfig(sources=[SourceConfig(chat_id=-100999, title="Imported Feed")])
        imported.risk.risk_percent = 0.75
        await svc.save_config(imported, imported=True)
        app.processEvents()
        assert risk_page.pct.value() == pytest.approx(0.75)
        sources_page = win.pages[4]
        assert sources_page.src.rowCount() == 1
        assert sources_page.src.item(0, 2).text() == "Imported Feed"
    finally:
        win.close()
        await svc.stop()
