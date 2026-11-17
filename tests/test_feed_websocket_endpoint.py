from live.feed import websocket as feed_websocket


def test_usdm_kline_websocket_uses_market_endpoint(monkeypatch):
    monkeypatch.setattr(feed_websocket, "SYMBOL", "BTCUSDT")
    monkeypatch.setattr(feed_websocket, "INTERVAL", "1m")

    assert (
        feed_websocket.ws_url()
        == "wss://fstream.binance.com/market/ws/btcusdt@kline_1m"
    )
