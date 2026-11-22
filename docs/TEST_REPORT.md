# Test report

Latest run: 2026-11-22 on macOS (Python 3.13, Qt offscreen) for **v0.5.0**. GitHub Actions runs the same suite on Windows and Ubuntu for Python 3.12–3.14, then builds and smoke-tests the Windows executables (`TGMT5-Setup` and `TGMT5-Portable`).

```
$ QT_QPA_PLATFORM=offscreen pytest -q
299 passed, 1 deselected in 4.3s
```

v0.5.0 added `test_regressions_v050.py` (**42 tests**) from a full codebase audit & bug-testing session: zero-price/SL parser crashes, TP-inside-zone geometry, double-space `BUY  STOP`, comma decimals and conditional wording in follow-up management commands, `UNCERTAIN` recovery under `GatewayError`, pending-order recovery status, late reconciliation transitions, MONITOR-mode active-signal edit blocking, emergency-stop multi-leg abort, PAPER / rejected hourly-limit isolation, manual partial-close resilience, `MODIFY_TP` clearing and side validation, MT5 clock offset reset and expiration conversion, mixed-case and currency-suffix symbol resolution, netting commission accounting, offline XAUUSD preview accuracy, DB `load_config` MONITOR enforcement, structured payload & traceback secret redaction, Telethon duplicate-handler prevention on restart, MONITOR reconnect with expired pending orders, snapshot `GatewayError` resilience, worker-thread risk resets, and UI config import / slider precision.

Previous runs:
- **v0.4.0** (2026-11-14, macOS, Python 3.13, Qt offscreen): 257 passed (`test_regressions_v040.py` added 13 tests + 30 parser cases).
- **v0.3.0** (2026-11-13, Windows 11, Python 3.14.8): 214 passed.
- **v0.1.0** (2026-11-03, Linux container, Python 3.13, Qt offscreen): 174 passed.

| File | Tests | Covers |
|---|---|---|
| `test_regressions_v050.py` | 42 | All v0.5.0 audit fixes end to end across parser, recovery, execution, management, MT5 adapter, symbol resolver, risk/preview, DB/redaction, Telethon, service and UI |
| `test_regressions_v040.py` | 13 | The v0.4.0 bug fixes end to end through the engine, service, risk engine and MT5 adapter |
| `test_parser.py` | 87 | All seven example formats from the spec, aliases, limit/stop, multiple TPs, "TP open", thousands separators, timestamps and lot/risk noise, ambiguous cases, false positives, management variants |
| `test_sizing.py` | 14 | Never rounding up, EURUSD and XAUUSD sizing, non-standard contract size, JPY → USD conversion, tick-value fallback, min volume above budget, max volume and step, fixed lot, multi-TP split never exceeding the total, three TPs never tripling risk |
| `test_sizing_modes.py` | 16 | All 6 sizing methods, small-account policies, pre-trade review stamping, account-based sizing preview |
| `test_risk_profiles.py` | 10 | Built-in presets and user-defined named risk profiles (save, apply, rename, delete, validation) |
| `test_real_account.py` | 11 | REAL ACCOUNT mode: opt-in switch, typed confirmation, guard on every order, closing positions outside the mode, never imported |
| `test_config_compat.py` | 3 | Backward compatibility: settings saved by older versions (v0.1.0+) still load cleanly |
| `test_startup_shutdown.py` | 5 | Auto-connect at start-up (monitor-only) and bounded synchronous shutdown |
| `test_engine.py` | 46 | Full pipeline: duplicates, monitor mode, stale signals, unauthorised or disabled sources, review queue, price moved, wait-for-zone and expiry, zone REJECT/PENDING, explicit limits, spread, TP already reached, broker rejection, partial leg failure, timeout-but-executed reconciliation, timeout-not-executed single retry, transport exceptions, broker unreachable, filling-mode fallback, live account blocked + emergency, first-execution confirmation, emergency stop, limits, cross-source dedup, suffix resolution, edits, deletes, lockouts persisting across restart, consecutive-loss breaker, MT5 instability |
| `test_management.py` | 31 | Reply and context matching, ambiguity → review, BE (+offset, invalid), partial close, close all, modify SL/TP, cancel pending/waiting, TP-hit handling, TP1 → BE automation, trailing stop, netting account managed partials, FIRST_TP_ONLY, small-volume fallback, pending expiry, manual close in the terminal, start-up recovery |
| `test_service.py` | 19 | Async service with worker thread and a real SQLite file plus Alembic migration: monitor default, DEMO gating and confirmation, live/wrong account refused, account switched to live after connecting, emergency persistence, flatten-all leaves manual trades alone, paper mode isolation and cost model, 6 concurrent signals with max 2 trades → exactly 2 executed, bounded queue, disconnect → pause → reconnect → resume, review approve/dismiss, Telegram notifications, snapshot and analytics, config never persists execution enabled, secrets redacted from logs and DB |
| `test_telegram.py` | 6 | Telethon event conversion, chat filtering, edits/replies, deletions (incl. basic groups), handler exceptions, safe client options (`catch_up=False`, auto-reconnect) |
| `test_main.py` | 3 | Default data folder: `%LOCALAPPDATA%\TGMT5` normally; next to the `.exe` in the portable build; fallback when read-only |
| `test_ui_smoke.py` | 1 | Builds the main window, renders all 8 pages against live service data |
| `test_demo_e2e.py` | 1 (opt-in) | Real DEMO round trip. **Not run here** (needs Windows, an MT5 terminal and explicit approval) |

## What was NOT verified in this environment

- **The real MetaTrader5 package and terminal.** It is Windows-only. `RealMT5Gateway` is a thin mapping written against the documented API (`initialize`, `account_info`, `symbol_info`, `order_calc_profit`, `order_send`, …) and was not executed against a live terminal on macOS. Its server-time conversion, offset reset, and expiration handling are unit-tested with a stand-in module. Run `pytest -m demo_e2e` on Windows to verify against a live DEMO terminal.
- **The real Telegram network.** The live MTProto login flow, `iter_dialogs`, real event delivery and network reconnect behaviour of Telethon were not exercised against Telegram's servers; only the adapter's event conversion, handler deduplication, and error handling were tested with stub clients/events.
- **IC Markets-specific broker behaviour.** Symbol names, filling modes, stops levels and whether order comments are preserved come from the terminal at runtime. Reconciliation depends on the broker keeping the order comment (client tag). If a broker overwrote comments, an uncertain order would be marked `FAILED` and the position would appear as an "orphan" (magic number match) in the recovery report, so it would be flagged rather than duplicated.
- **Windows executables & installer (`TGMT5-Setup` / `TGMT5-Portable`).** Cannot be built on macOS; verified on every push/PR by the Windows GitHub Actions workflow (`build.yml`), which runs `pytest`, builds and signs both executables, and runs a 40-second `--sandbox` smoke test asserting clean startup and zero `ERROR`/`CRITICAL` log entries.
- **Visual review** used offscreen renders on macOS (`QT_QPA_PLATFORM=offscreen`) across both dark and light themes. On Windows the UI uses Segoe UI Variable.

## Known limitations

- Paper-trading positions are rebuilt from the database after a restart, but SL/TP hits that happen *while the app is closed* are not simulated for paper trades.
- Deletion events from private chats and basic (non-super) groups carry no chat ID. They are matched by message ID against private and basic-group sources only (never channels).
- The parser is English-oriented. Providers with unusual formats need aliases or the `implicit_market` template, or their messages end up in manual review.
- Correlation groups are user-defined lists, not computed correlations.
- The daily loss lockout uses UTC days.
