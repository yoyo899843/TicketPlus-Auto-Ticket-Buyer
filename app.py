"""Flask API + UI for Ticket Bot"""
from flask import Flask, jsonify, request, render_template
from bot import TicketBot

app = Flask(__name__)
bot = TicketBot()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def get_status():
    snap = bot.status_snapshot()
    return jsonify({
        "running": bot._running,
        "logged_in": snap["logged_in"],
        "message": bot._message,
        "areas": bot._areas,
        "page_url": snap["page_url"],
        "buy_running": bot._buy_running,
        "buy_message": bot._buy_message,
        "buy_sessions": bot._buy_sessions,
    })


# ── 開啟票區總覽 ────────────────────────────────

@app.route("/api/open-url", methods=["POST"])
def open_url():
    url = (request.json or {}).get("url")
    if not url:
        return jsonify({"error": "請提供 URL"}), 400
    try:
        bot.open_url(url)
        return jsonify({"status": "ok", "url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 第一步：偵測並點擊「立即購買」────────────────

@app.route("/api/wait-buy", methods=["POST"])
def wait_buy():
    data = request.json or {}
    date = (data.get("date") or "").strip() or None
    try:
        price = data.get("price")
        price = int(str(price).replace(",", "")) if price not in (None, "") else None
        quantity = int(data.get("quantity", 1))
        bot.start_wait_buy(date, quantity=quantity, price=price)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop-wait-buy", methods=["POST"])
def stop_wait_buy():
    bot.stop_wait_buy()
    return jsonify({"status": "ok"})


# ── 偵測價格 ────────────────────────────────────

@app.route("/api/prices", methods=["GET"])
def get_prices():
    try:
        return jsonify({"areas": bot.detect_prices()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 開始 / 停止搶票（自動偵測價格並隨機選一個）──

@app.route("/api/start", methods=["POST"])
def start_ticket():
    data = request.json or {}
    try:
        price = data.get("price")
        price = int(str(price).replace(",", "")) if price not in (None, "") else None
        bot.start_grab(int(data.get("quantity", 1)), price)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def stop_ticket():
    bot.stop_grab()
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5001)
