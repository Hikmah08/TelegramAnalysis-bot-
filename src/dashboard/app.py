import os
import secrets
from fastapi import FastAPI, Request, Response, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional, List
from ..paper.engine import PaperAccount
from ..paper.account_service import AccountService
from ..storage.database import StorageManager
from ..strategy.mtf_models import MultiTimeframeSetup
from ..strategy.sequence_tracker import SequenceTracker
from ..strategy.config import StrategyConfig
from ..backtest.engine import BacktestEngine
from ..backtest.models import BacktestConfig
from ..backtest.historical_manager import HistoricalDataManager
from ..data.models import Timeframe
from ..data.instruments import get_instrument_spec


class BacktestRunRequest(BaseModel):
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"
    initial_capital: float = 10000.0
    risk_pct: float = 1.0
    target_rr: float = 4.0
    spread_pips: float = 0.0
    fee_pct: float = 0.0006
    slippage_pct: float = 0.0002
    candle_limit: int = 500


class WatchlistAddRequest(BaseModel):
    symbol: str


class SettingsUpdateRequest(BaseModel):
    key: str
    value: str


def create_dashboard_app(
    account: Optional[PaperAccount] = None,
    last_scan_data: Optional[Dict[str, Any]] = None,
    pending_approvals: Optional[Dict[int, MultiTimeframeSetup]] = None,
    storage: Optional[StorageManager] = None,
) -> FastAPI:
    """
    FastAPI Web Dashboard with Multi-User Session Authentication.
    Users authenticate via single-use login tokens obtained from Telegram.
    Cookies store session_id (HttpOnly, SameSite=Lax).
    All data is strictly scoped to the authenticated user from the session.
    """
    app = FastAPI(title="ICT/SMC Trading Analysis Bot Dashboard")
    store = storage or StorageManager()
    acct_svc = AccountService(storage=store)
    hist_mgr = HistoricalDataManager()
    approvals_store = pending_approvals if pending_approvals is not None else {}
    scan_data = last_scan_data if last_scan_data is not None else {}

    # In-memory backtest results storage per user: {user_id: [results]}
    completed_backtests: Dict[str, List[Dict[str, Any]]] = {}

    def get_current_user(request: Request) -> str:
        """
        Extracts authenticated user_id strictly from server-side session cookie.
        NEVER trusts user_id from query params, body, or headers!
        """
        session_id = request.cookies.get("session_id")
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Please request a login link from Telegram via /dashboard",
            )
        user_id = store.get_dashboard_session_user(session_id)
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or invalid. Please request a new login link via /dashboard in Telegram",
            )
        return user_id

    # Single-use Token Exchange Route
    @app.get("/auth/login")
    def login_with_token(token: str, response: Response):
        """
        Validates single-use token against SHA-256 hash in DB, marks as used,
        generates session, and sets secure HttpOnly cookie.
        """
        user_id = store.consume_dashboard_token(token)
        if not user_id:
            return HTMLResponse(
                content="<h2 style='color:#ef4444;font-family:sans-serif;'>Invalid or expired login token. Please request a new link from Telegram with /dashboard.</h2>",
                status_code=400,
            )
        session_id = store.create_dashboard_session(user_id=user_id, duration_days=7)
        redirect = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        secure_cookie = os.getenv("SECURE_COOKIES", "").strip().lower() in ("true", "1", "yes")
        redirect.set_cookie(
            key="session_id",
            value=session_id,
            httponly=True,
            samesite="lax",
            max_age=7 * 86400,
            secure=secure_cookie,
        )
        return redirect

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        session_id = request.cookies.get("session_id")
        if session_id:
            store.delete_dashboard_session(session_id)
        redirect = RedirectResponse(url="/auth/login-required", status_code=status.HTTP_302_FOUND)
        redirect.delete_cookie("session_id")
        return redirect

    @app.get("/auth/login-required", response_class=HTMLResponse)
    def login_required_page():
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Authentication Required - ICT/SMC Trading Copilot</title>
            <style>
                body { font-family: -apple-system, sans-serif; background: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                .card { background: #1e293b; padding: 32px; border-radius: 12px; border: 1px solid #334155; text-align: center; max-width: 420px; }
                h1 { font-size: 20px; margin-bottom: 12px; color: #f59e0b; }
                p { color: #94a3b8; font-size: 14px; line-height: 1.5; }
                code { background: #0f172a; padding: 4px 8px; border-radius: 4px; color: #38bdf8; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>🔒 Secure Session Required</h1>
                <p>To access your isolated dashboard, please open Telegram and send:</p>
                <p><code>/dashboard</code></p>
                <p>Click the <b>OPEN FULL DASHBOARD</b> button to generate a single-use secure login token.</p>
            </div>
        </body>
        </html>
        """

    @app.get("/api/status")
    def get_status(user_id: str = Depends(get_current_user)):
        metrics = acct_svc.get_metrics(user_id=user_id)
        max_pos = int(store.get_setting("max_open_positions", 20, user_id=user_id))
        user_backtests = completed_backtests.get(user_id, [])
        return {
            "user_id": user_id,
            "account": {
                "starting_balance": metrics.starting_balance,
                "current_balance": metrics.current_balance,
                "equity": metrics.equity,
                "realized_pnl": metrics.realized_pnl,
                "unrealized_pnl": metrics.unrealized_pnl,
                "total_pnl": metrics.total_pnl,
                "total_pnl_pct": metrics.total_pnl_pct,
                "open_positions": f"{metrics.open_positions_count}/{max_pos}",
                "open_positions_count": metrics.open_positions_count,
                "max_open_positions": max_pos,
                "closed_positions": metrics.closed_trades_count,
                "win_rate_pct": metrics.win_rate_pct,
                "max_drawdown_pct": metrics.max_drawdown_pct,
            },
            "open_positions": metrics.open_positions,
            "last_scan": {
                "timestamp": scan_data.get("timestamp"),
                "symbol": scan_data.get("symbol", "BTCUSDT"),
                "bias_4h": scan_data.get("bias_4h"),
                "setups_count": len(scan_data.get("setups", [])),
                "pending_approvals": len(approvals_store),
            },
            "backtests_count": len(user_backtests),
        }

    @app.post("/api/approve_trade/{setup_id}")
    def approve_trade(setup_id: int, user_id: str = Depends(get_current_user)):
        if setup_id not in approvals_store:
            return {"status": "error", "message": f"Setup #{setup_id} not found or already processed."}

        # Check user's open positions limit
        open_pos = store.get_open_positions(user_id=user_id)
        max_pos = int(store.get_setting("max_open_positions", 20, user_id=user_id))
        if len(open_pos) >= max_pos:
            return {
                "status": "error",
                "message": f"Maximum open paper positions ({max_pos}) reached! Please close an existing position first.",
            }

        setup = approvals_store.pop(setup_id)
        risk_pct = float(store.get_setting("risk_per_trade_pct", 1.0, user_id=user_id))
        cur_bal = float(store.get_setting("current_balance", 10000.0, user_id=user_id))
        risk_unit = abs(setup.entry_price - setup.stop_loss)
        dollar_risk = cur_bal * (risk_pct / 100.0)
        pos_size = dollar_risk / risk_unit if risk_unit > 0 else 0.0

        pos_id = store.open_paper_trade(
            symbol=scan_data.get("symbol", "BTCUSDT"),
            direction=setup.direction.value,
            entry_price=setup.entry_price,
            stop_loss=setup.stop_loss,
            take_profit=(setup.entry_price + (risk_unit * 4.0)) if setup.direction.value == "bullish" else (setup.entry_price - (risk_unit * 4.0)),
            position_size=pos_size,
            user_id=user_id,
        )
        return {"status": "success", "message": f"Setup #{setup_id} approved and entered into virtual paper trading!", "position_id": pos_id}

    @app.post("/api/reject_trade/{setup_id}")
    def reject_trade(setup_id: int, user_id: str = Depends(get_current_user)):
        if setup_id in approvals_store:
            del approvals_store[setup_id]
            return {"status": "success", "message": f"Setup #{setup_id} rejected and dismissed."}
        return {"status": "error", "message": "Setup not found."}

    @app.get("/api/watchlist")
    def get_watchlist(user_id: str = Depends(get_current_user)):
        return {"watchlist": store.get_watchlist(user_id=user_id)}

    @app.post("/api/watchlist/add")
    def add_watchlist(req: WatchlistAddRequest, user_id: str = Depends(get_current_user)):
        from ..data.instruments import is_supported_symbol, get_instrument_spec
        clean = req.symbol.strip().upper().replace("/", "").replace("-", "")
        if not is_supported_symbol(clean):
            raise HTTPException(status_code=400, detail=f"Unsupported symbol '{clean}'")
        spec = get_instrument_spec(clean)
        store.add_watchlist_symbol(clean, asset_class=spec.asset_class.value, user_id=user_id)
        return {"status": "success", "symbol": clean, "asset_class": spec.asset_class.value}

    @app.delete("/api/watchlist/remove/{symbol}")
    def remove_watchlist(symbol: str, user_id: str = Depends(get_current_user)):
        clean = symbol.strip().upper().replace("/", "").replace("-", "")
        removed = store.remove_watchlist_symbol(clean, user_id=user_id)
        return {"status": "success" if removed else "not_found", "symbol": clean}

    @app.get("/api/positions")
    def get_positions(user_id: str = Depends(get_current_user)):
        metrics = acct_svc.get_metrics(user_id=user_id)
        return {"open_positions": metrics.open_positions}

    @app.post("/api/positions/close/{position_id}")
    def close_position_manually(position_id: int, user_id: str = Depends(get_current_user)):
        pos = store.get_paper_position(position_id, user_id=user_id)
        if not pos:
            raise HTTPException(status_code=404, detail=f"Position #{position_id} not found or access denied.")
        if pos["status"] != "OPEN":
            raise HTTPException(status_code=400, detail=f"Position #{position_id} is already closed.")

        exit_price = pos["entry_price"]
        size = float(pos["position_size"])
        fee = size * exit_price * 0.0006
        pnl = -fee

        store.close_paper_trade_atomic(
            position_id=position_id,
            user_id=user_id,
            exit_price=exit_price,
            exit_reason="manual",
            pnl=pnl,
            fee_paid=fee,
            notification_event={
                "event_type": "trade_closed_manual",
                "symbol": pos["symbol"],
                "pnl": pnl,
                "exit_price": exit_price,
                "message": f"🛑 <b>POSITION #{position_id} CLOSED MANUALLY</b>\n\n<b>Symbol:</b> {pos['symbol']}\n<b>Exit:</b> {exit_price:.2f}\n<b>P&L:</b> ${pnl:+,.2f}",
            },
        )
        return {"status": "success", "message": f"Position #{position_id} closed manually."}

    @app.get("/api/history")
    def get_trade_history(user_id: str = Depends(get_current_user)):
        closed = store.get_closed_positions(user_id=user_id)
        return {"count": len(closed), "history": closed}

    @app.get("/api/settings")
    def get_settings(user_id: str = Depends(get_current_user)):
        return {"settings": store.get_all_settings(user_id=user_id)}

    @app.post("/api/settings/update")
    def update_settings(req: SettingsUpdateRequest, user_id: str = Depends(get_current_user)):
        key = req.key.lower().strip()
        val = req.value.strip()
        if key in ("risk", "risk_per_trade_pct"):
            num = float(val)
            if not (0.1 <= num <= 10.0):
                raise HTTPException(status_code=400, detail="Risk % must be between 0.1 and 10.0")
            store.set_setting("risk_per_trade_pct", str(num), user_id=user_id)
        elif key in ("max_pos", "max_open_positions"):
            num = int(val)
            if not (1 <= num <= 50):
                raise HTTPException(status_code=400, detail="Max positions must be between 1 and 50")
            store.set_setting("max_open_positions", str(num), user_id=user_id)
        else:
            store.set_setting(key, val, user_id=user_id)
        return {"status": "success", "key": key, "value": val}

    @app.post("/api/backtest/run")
    def run_backtest_endpoint(req: BacktestRunRequest, user_id: str = Depends(get_current_user)):
        """
        Runs an isolated historical backtest.
        Never modifies paper trading balance. Results saved to user's isolated history.
        """
        tf_enum = Timeframe(req.timeframe)
        df = hist_mgr.fetch_and_store(req.symbol, tf_enum, limit=req.candle_limit)

        if df.empty:
            return {"status": "error", "message": f"Could not retrieve historical data for {req.symbol}."}

        strat_cfg = StrategyConfig(swing_left_bars=2, swing_right_bars=2)
        tracker = SequenceTracker(timeframe=tf_enum.value, config=strat_cfg)

        bt_cfg = BacktestConfig(
            initial_capital=req.initial_capital,
            risk_per_trade_pct=req.risk_pct,
            target_risk_reward=req.target_rr,
            spread_pips=req.spread_pips,
            fee_pct=req.fee_pct,
            slippage_pct=req.slippage_pct,
        )
        engine = BacktestEngine(config=bt_cfg)

        meta = hist_mgr.get_dataset_metadata(req.symbol, tf_enum)
        metrics, trades = engine.run(df, tracker, symbol=req.symbol, dataset_metadata=meta)

        if user_id not in completed_backtests:
            completed_backtests[user_id] = []

        res = {
            "id": len(completed_backtests[user_id]) + 1,
            "metrics": metrics.to_dict(),
            "trades_count": len(trades),
            "trades": [t.to_dict() for t in trades],
        }
        completed_backtests[user_id].append(res)
        return {"status": "success", "result": res}

    @app.get("/api/backtest/history")
    def get_backtest_history(user_id: str = Depends(get_current_user)):
        user_backtests = completed_backtests.get(user_id, [])
        return {"count": len(user_backtests), "results": user_backtests}

    @app.get("/", response_class=HTMLResponse)
    def render_dashboard(request: Request):
        session_id = request.cookies.get("session_id")
        user_id = store.get_dashboard_session_user(session_id) if session_id else None
        if not user_id:
            return RedirectResponse(url="/auth/login-required", status_code=status.HTTP_302_FOUND)

        metrics = acct_svc.get_metrics(user_id=user_id)
        max_pos = int(store.get_setting("max_open_positions", 20, user_id=user_id))
        pnl_color = "#10b981" if metrics.total_pnl >= 0 else "#ef4444"

        # Render Pending Approvals Queue
        approvals_html = ""
        for setup_id, s in approvals_store.items():
            approvals_html += f"""
            <div style="background: #1e293b; padding: 16px; border-radius: 8px; margin-bottom: 12px; border-left: 4px solid #f59e0b;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div style="font-weight: bold; font-size: 16px; color: #f8fafc;">
                        CANDIDATE #{setup_id}: {s.direction.value.upper()} @ {s.entry_price:.2f} (Stop: {s.stop_loss:.2f}) [4:1 R:R Target]
                    </div>
                    <div>
                        <button onclick="approveTrade({setup_id})" style="background: #10b981; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: bold; margin-right: 8px;">ENTER PAPER TRADE</button>
                        <button onclick="rejectTrade({setup_id})" style="background: #ef4444; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: bold;">REJECT</button>
                    </div>
                </div>
                <div style="color: #94a3b8; font-size: 13px; margin-top: 6px;">
                    {'<br>'.join(s.audit_trail)}
                </div>
            </div>
            """

        if not approvals_html:
            approvals_html = "<div style='color: #64748b; padding: 12px; background: #1e293b; border-radius: 8px;'>No trade setups currently awaiting review.</div>"

        # Render Open Paper Positions with Close Button
        open_pos_html = ""
        for p in metrics.open_positions:
            pos_id = p['position_id']
            open_pos_html += f"""
            <div style="background: #1e293b; padding: 16px; border-radius: 8px; margin-bottom: 12px; border-left: 4px solid {'#10b981' if p['direction'].lower() == 'bullish' else '#ef4444'};">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <span style="font-weight: bold; font-size: 16px; color: #f8fafc;">
                            #{pos_id} {p['direction'].upper()} {p['symbol']}
                        </span>
                        <span style="color: #94a3b8; font-size: 13px; margin-left: 12px;">
                            Entry: {p['entry_price']:.2f} | Stop Loss: {p['stop_loss']:.2f} | Target (4:1): {p['take_profit']:.2f}
                        </span>
                    </div>
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-weight: bold; font-size: 15px; color: {'#10b981' if p.get('unrealized_pnl', 0) >= 0 else '#ef4444'};">
                            ${p.get('unrealized_pnl', 0):+,.2f}
                        </span>
                        <button onclick="closePosition({pos_id})" style="background: #dc2626; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: bold;">CLOSE</button>
                    </div>
                </div>
            </div>
            """

        if not open_pos_html:
            open_pos_html = "<div style='color: #64748b; padding: 12px; background: #1e293b; border-radius: 8px;'>No active open paper positions.</div>"

        # Render Watchlist
        user_wl = store.get_watchlist(user_id=user_id)
        wl_html = ""
        for item in user_wl:
            sym = item["symbol"]
            ac = item.get("asset_class", "CRYPTO")
            wl_html += f"""
            <div style="display: inline-flex; align-items: center; gap: 8px; background: #0f172a; border: 1px solid #334155; padding: 6px 12px; border-radius: 20px; margin: 4px;">
                <span style="font-weight: bold; font-size: 13px;">{sym}</span>
                <span style="color: #64748b; font-size: 11px;">{ac}</span>
                <button onclick="removeWatchlist('{sym}')" style="background: none; border: none; color: #ef4444; cursor: pointer; font-size: 14px; padding: 0 4px;">&times;</button>
            </div>
            """

        # Render Closed Trades History
        closed_trades = store.get_closed_positions(user_id=user_id)
        history_html = ""
        for ct in closed_trades[:15]:
            pnl_val = ct.get("pnl", 0.0)
            history_html += f"""
            <tr style="border-bottom: 1px solid #334155;">
                <td style="padding: 10px 8px;">#{ct['position_id']}</td>
                <td style="padding: 10px 8px; font-weight: bold;">{ct['symbol']}</td>
                <td style="padding: 10px 8px; color: {'#10b981' if ct['direction'].lower() == 'bullish' else '#ef4444'}; font-weight: bold;">{ct['direction'].upper()}</td>
                <td style="padding: 10px 8px;">${ct['entry_price']:.2f}</td>
                <td style="padding: 10px 8px;">${ct.get('exit_price', 0.0):.2f}</td>
                <td style="padding: 10px 8px; text-transform: uppercase; font-size: 12px; color: #94a3b8;">{ct.get('exit_reason', 'N/A')}</td>
                <td style="padding: 10px 8px; font-weight: bold; color: {'#10b981' if pnl_val >= 0 else '#ef4444'};">${pnl_val:+,.2f}</td>
            </tr>
            """
        if not history_html:
            history_html = "<tr><td colspan='7' style='padding: 16px; text-align: center; color: #64748b;'>No completed trade history recorded yet.</td></tr>"

        # User Settings
        settings_dict = store.get_all_settings(user_id=user_id)
        curr_risk = settings_dict.get("risk_per_trade_pct", "1.0")
        curr_max_pos = settings_dict.get("max_open_positions", "20")

        user_record = store.get_user(user_id) or {}
        display_name = user_record.get("username") or user_record.get("first_name") or user_id

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>ICT/SMC Trading Bot Dashboard</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
                .container {{ max-width: 1000px; margin: 0 auto; }}
                .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 16px; margin-bottom: 24px; }}
                .badge {{ background: #334155; padding: 4px 10px; border-radius: 9999px; font-size: 12px; }}
                .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }}
                .card {{ background: #1e293b; padding: 20px; border-radius: 8px; border: 1px solid #334155; }}
                .card-title {{ font-size: 13px; color: #94a3b8; text-transform: uppercase; margin-bottom: 8px; }}
                .card-value {{ font-size: 24px; font-weight: bold; }}
                .section-box {{ background: #1e293b; padding: 24px; border-radius: 8px; border: 1px solid #334155; margin-top: 24px; }}
                input, select, button {{ padding: 8px 12px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: white; }}
                button.primary {{ background: #3b82f6; border: none; cursor: pointer; font-weight: bold; }}
                table {{ width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }}
                th {{ padding: 8px; color: #94a3b8; border-bottom: 1px solid #475569; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div>
                        <h1 style="margin: 0; font-size: 24px;">ICT/SMC Trading Copilot</h1>
                        <div style="color: #94a3b8; font-size: 13px; margin-top: 4px;">Logged in as: <b>{display_name}</b> (ID: {user_id}) &bull; Isolated Multi-User Paper Account</div>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <div class="badge">Open Positions: {metrics.open_positions_count}/{max_pos}</div>
                        <form action="/auth/logout" method="post" style="margin: 0;">
                            <button type="submit" style="background: #334155; color: #f8fafc; font-size: 12px; padding: 4px 10px; border-radius: 6px; border: none; cursor: pointer;">Logout</button>
                        </form>
                    </div>
                </div>

                <div class="grid">
                    <div class="card">
                        <div class="card-title">Virtual Equity</div>
                        <div class="card-value">${metrics.equity:,.2f}</div>
                    </div>
                    <div class="card">
                        <div class="card-title">Realized Net PnL</div>
                        <div class="card-value" style="color: {pnl_color};">${metrics.realized_pnl:+,.2f}</div>
                    </div>
                    <div class="card">
                        <div class="card-title">Win Rate</div>
                        <div class="card-value">{metrics.win_rate_pct:.1f}%</div>
                    </div>
                    <div class="card">
                        <div class="card-title">Max Drawdown</div>
                        <div class="card-value">{metrics.max_drawdown_pct:.2f}%</div>
                    </div>
                </div>

                <h2 style="font-size: 18px; margin-bottom: 16px;">Trade Approval Queue (Manual Review)</h2>
                {approvals_html}

                <h2 style="font-size: 18px; margin-top: 32px; margin-bottom: 16px;">Active Open Paper Positions ({metrics.open_positions_count}/{max_pos})</h2>
                {open_pos_html}

                <div class="section-box">
                    <h2 style="font-size: 18px; margin-top: 0;">📋 My Watchlist</h2>
                    <div style="margin-bottom: 12px;">{wl_html}</div>
                    <div style="display: flex; gap: 8px; align-items: center; margin-top: 12px;">
                        <input id="newSymbol" placeholder="e.g. BTCUSDT, EURUSD, XAUUSD" style="text-transform: uppercase;" />
                        <button class="primary" onclick="addWatchlist()">Add Symbol</button>
                    </div>
                </div>

                <div class="section-box">
                    <h2 style="font-size: 18px; margin-top: 0;">📜 Closed Trade History (Recent 15)</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>ID</th><th>Symbol</th><th>Direction</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net P&L</th>
                            </tr>
                        </thead>
                        <tbody>
                            {history_html}
                        </tbody>
                    </table>
                </div>

                <div class="section-box">
                    <h2 style="font-size: 18px; margin-top: 0;">⚙️ Account Settings</h2>
                    <div style="display: flex; gap: 16px; flex-wrap: wrap;">
                        <div>
                            <label style="font-size: 12px; display: block; margin-bottom: 4px;">Risk Per Trade (%):</label>
                            <input id="userRiskPct" type="number" step="0.1" value="{curr_risk}" style="width: 90px;" />
                        </div>
                        <div>
                            <label style="font-size: 12px; display: block; margin-bottom: 4px;">Max Open Positions:</label>
                            <input id="userMaxPos" type="number" value="{curr_max_pos}" style="width: 90px;" />
                        </div>
                        <div style="align-self: flex-end;">
                            <button class="primary" onclick="saveSettings()">Save Settings</button>
                        </div>
                    </div>
                </div>

                <div class="section-box">
                    <h2 style="font-size: 18px; margin-top: 0;">🔬 Historical Backtesting Engine (Multi-Asset)</h2>
                    <p style="color: #94a3b8; font-size: 13px;">Test the ICT/SMC 4:1 strategy against real historical data for Crypto, Forex, and Metals. Isolated from paper trading balance.</p>
                    <div style="display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px;">
                        <div>
                            <label style="font-size: 12px; display: block; margin-bottom: 4px;">Symbol:</label>
                            <select id="btSymbol">
                                <option value="BTCUSDT">BTCUSDT (Crypto)</option>
                                <option value="ETHUSDT">ETHUSDT (Crypto)</option>
                                <option value="EURUSD">EURUSD (Forex)</option>
                                <option value="GBPUSD">GBPUSD (Forex)</option>
                                <option value="USDJPY">USDJPY (Forex)</option>
                                <option value="XAUUSD">XAUUSD (Gold)</option>
                                <option value="XAGUSD">XAGUSD (Silver)</option>
                            </select>
                        </div>
                        <div>
                            <label style="font-size: 12px; display: block; margin-bottom: 4px;">Timeframe:</label>
                            <select id="btTf">
                                <option value="15m">15M</option>
                                <option value="5m">5M</option>
                                <option value="1h">1H</option>
                                <option value="4h">4H</option>
                            </select>
                        </div>
                        <div>
                            <label style="font-size: 12px; display: block; margin-bottom: 4px;">Candles:</label>
                            <input id="btCandles" type="number" value="300" style="width: 80px;" />
                        </div>
                        <div>
                            <label style="font-size: 12px; display: block; margin-bottom: 4px;">Risk %:</label>
                            <input id="btRisk" type="number" step="0.5" value="1.0" style="width: 70px;" />
                        </div>
                        <div style="align-self: flex-end;">
                            <button class="primary" onclick="runBacktest()">Run Backtest</button>
                        </div>
                    </div>
                    <div id="btResults" style="color: #cbd5e1; font-size: 14px; background: #0f172a; padding: 16px; border-radius: 6px; display: none;"></div>
                </div>
            </div>

            <script>
                async function approveTrade(id) {{
                    const res = await fetch('/api/approve_trade/' + id, {{ method: 'POST' }});
                    const data = await res.json();
                    alert(data.message);
                    window.location.reload();
                }}
                async function rejectTrade(id) {{
                    const res = await fetch('/api/reject_trade/' + id, {{ method: 'POST' }});
                    const data = await res.json();
                    alert(data.message);
                    window.location.reload();
                }}
                async function closePosition(posId) {{
                    if (!confirm('Close position #' + posId + ' manually?')) return;
                    const res = await fetch('/api/positions/close/' + posId, {{ method: 'POST' }});
                    const data = await res.json();
                    if (res.ok) {{
                        alert(data.message);
                        window.location.reload();
                    }} else {{
                        alert('Error: ' + (data.detail || data.message));
                    }}
                }}
                async function addWatchlist() {{
                    const sym = document.getElementById('newSymbol').value.trim();
                    if (!sym) return;
                    const res = await fetch('/api/watchlist/add', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ symbol: sym }})
                    }});
                    const data = await res.json();
                    if (res.ok) {{
                        window.location.reload();
                    }} else {{
                        alert('Error: ' + (data.detail || 'Could not add symbol'));
                    }}
                }}
                async function removeWatchlist(sym) {{
                    const res = await fetch('/api/watchlist/remove/' + sym, {{ method: 'DELETE' }});
                    if (res.ok) window.location.reload();
                }}
                async function saveSettings() {{
                    const risk = document.getElementById('userRiskPct').value;
                    const maxPos = document.getElementById('userMaxPos').value;
                    await fetch('/api/settings/update', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ key: 'risk_per_trade_pct', value: risk }})
                    }});
                    await fetch('/api/settings/update', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ key: 'max_open_positions', value: maxPos }})
                    }});
                    alert('Settings updated successfully!');
                    window.location.reload();
                }}
                async function runBacktest() {{
                    const resDiv = document.getElementById('btResults');
                    resDiv.style.display = 'block';
                    resDiv.innerHTML = '<i>Running simulation on historical dataset...</i>';

                    const payload = {{
                        symbol: document.getElementById('btSymbol').value,
                        timeframe: document.getElementById('btTf').value,
                        candle_limit: parseInt(document.getElementById('btCandles').value),
                        risk_pct: parseFloat(document.getElementById('btRisk').value),
                        target_rr: 4.0
                    }};

                    const res = await fetch('/api/backtest/run', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(payload)
                    }});
                    const data = await res.json();
                    if (data.status === 'success') {{
                        const m = data.result.metrics;
                        resDiv.innerHTML = `
                            <b>Backtest Completed Successfully:</b><br>
                            • Total Trades: ${{m.total_trades}} (Win Rate: ${{m.win_rate_pct}}%)<br>
                            • Final Equity: $${{m.final_equity.toLocaleString()}} (Return: ${{m.return_on_capital_pct}}%)<br>
                            • Profit Factor: ${{m.profit_factor}} | Average R-Multiple: ${{m.average_r_multiple}}R<br>
                            • Max Drawdown: ${{m.max_drawdown_pct}}% ($${{m.max_drawdown_amount.toLocaleString()}})<br>
                            • Long Trades: ${{m.long_performance.trades}} | Short Trades: ${{m.short_performance.trades}}<br>
                            • Consecutive Wins: ${{m.consecutive_wins_max}} | Consecutive Losses: ${{m.consecutive_losses_max}}
                        `;
                    }} else {{
                        resDiv.innerHTML = '<span style="color:#ef4444;">Backtest failed: ' + data.message + '</span>';
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content)

    return app
