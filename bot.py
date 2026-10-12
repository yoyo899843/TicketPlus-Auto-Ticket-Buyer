"""Playwright 搶票引擎"""
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from playwright.sync_api import sync_playwright

# 持久化瀏覽器 profile：cookie、localStorage 都存在這裡，重開程式仍保持登入
PROFILE_DIR = Path(__file__).parent / "browser_profile"

# Playwright sync API 綁定建立它的執行緒，所有呼叫都必須在同一條 worker 執行緒上執行
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
_worker_ident = None


def _mark_worker():
    global _worker_ident
    _worker_ident = threading.get_ident()


_executor.submit(_mark_worker).result()


def on_worker(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if threading.get_ident() == _worker_ident:
            return fn(*args, **kwargs)
        return _executor.submit(fn, *args, **kwargs).result()
    return wrapper


# 讀取票區總覽：兩種版面都適用
#  - 開賣後：.seats-area > .v-expansion-panel，標題含「剩餘 N」與「NT.1,880」
#  - 未開賣：.price-group（依價格分組）> .seats-area > .v-expansion-panel，沒有剩餘數
_READ_BUY_SESSIONS_JS = """() => {
    const container = document.querySelector('#buyTicket');
    if (!container) return [];
    return [...container.querySelectorAll('.sesstion-item')].map((item, i) => {
        const text = item.innerText.replace(/\\s+/g, ' ').trim();
        const dm = text.match(/(\\d{4}-\\d{2}-\\d{2})/);
        const nameEl = item.querySelector('.text-regular');
        const btn = item.querySelector('button.nextBtn:not([disabled])');
        return {
            index: i,
            name: nameEl ? nameEl.innerText.replace(/\\s+/g, ' ').trim() : text,
            date: dm ? dm[1] : null,
            hasButton: !!btn,
        };
    });
}"""


_READ_AREAS_JS = """() => {
    const groups = [...document.querySelectorAll('.price-group > .v-expansion-panel')];
    return [...document.querySelectorAll('.seats-area > .v-expansion-panel')].map((p, i) => {
        const h = p.querySelector(':scope > button.v-expansion-panel-header');
        const t = h.innerText.replace(/\\s+/g, ' ').trim();
        const pm = t.match(/NT\\.?\\s*([\\d,]+)/);
        const rm = t.match(/剩餘\\s*(\\d+)/);
        return {
            index: i,
            group: groups.findIndex(g => g.contains(p)),
            name: t.replace(/剩餘\\s*\\d+/, '').replace(/NT\\.?\\s*[\\d,]+/, '').trim(),
            price: pm ? parseInt(pm[1].replace(/,/g, '')) : null,
            remaining: rm ? parseInt(rm[1]) : null,
            disabled: h.disabled || p.classList.contains('v-expansion-panel--disabled'),
            active: p.classList.contains('v-expansion-panel--active'),
        };
    });
}"""


class TicketBot:
    """Ticket Plus 搶票 Bot，管理一個 Chromium session。"""

    def __init__(self):
        self._pw = None
        self._context = None
        self._page = None
        self._running = False
        self._stop_event = threading.Event()
        self._message = ""
        self._areas = []
        self._cache = {"logged_in": False, "page_url": ""}

        # 第一步：票區總覽頁 #buyTicket 區塊的「立即購買」偵測/點擊
        self._buy_running = False
        self._buy_stop_event = threading.Event()
        self._buy_message = ""
        self._buy_sessions = []

    # ── 瀏覽器 ──────────────────────────────────

    def _alive(self) -> bool:
        """使用者手動關掉視窗後，頁面會被關閉，需要重新啟動。"""
        try:
            return bool(self._page) and not self._page.is_closed()
        except Exception:
            return False

    @on_worker
    def start(self):
        if self._context and not self._alive():
            self.stop()
        if self._context:
            return
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, locale="zh-TW", args=["--lang=zh-TW"])
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(5_000)  # 避免單一動作卡 30 秒擋住其他請求

    @on_worker
    def stop(self):
        self.stop_grab()
        for attr in ("_context", "_pw"):
            obj = getattr(self, attr)
            if obj:
                try:
                    obj.close() if attr == "_context" else obj.stop()
                except Exception:
                    pass
        self._page = self._context = self._pw = None

    @on_worker
    def open_url(self, url: str):
        """直接前往票區總覽頁。"""
        self.start()
        self._page.goto(url)
        self._wait_for_areas()
        return True

    @on_worker
    def page_url(self) -> str:
        self._cache["page_url"] = self._page.url if self._page else ""
        return self._cache["page_url"]

    def status_snapshot(self) -> dict:
        """給狀態輪詢用：搶票進行中 worker 很忙，直接回傳快取，不排隊等待。"""
        if not self._running and not self._buy_running:
            self.is_logged_in()
            self.page_url()
        return dict(self._cache)

    @on_worker
    def is_logged_in(self) -> bool:
        """頁首有「登出」連結就代表已登入（比單看 cookie 準確）。"""
        try:
            logged_in = bool(self._page) and self._page.locator("header :text('登出')").count() > 0
        except Exception:
            logged_in = False
        self._cache["logged_in"] = logged_in
        return logged_in

    # ── 第一步：票區總覽頁的「立即購買」偵測/點擊 ──
    # 開賣前 #buyTicket 底下的場次沒有可點的按鈕；開賣後每個場次（.sesstion-item）
    # 會有一個日期欄位和旁邊的「立即購買」按鈕。

    def _wait_for_buy_ticket(self, timeout_ms=15_000):
        try:
            self._page.wait_for_selector("#buyTicket", timeout=timeout_ms)
        except Exception:
            pass

    @on_worker
    def detect_buy_sessions(self) -> list[dict]:
        """讀取 #buyTicket 底下各場次的日期與是否有可點擊的「立即購買」按鈕。"""
        if not self._page:
            raise RuntimeError("請先開啟票區總覽網址")
        self._wait_for_buy_ticket()
        self._buy_sessions = self._page.evaluate(_READ_BUY_SESSIONS_JS)
        return self._buy_sessions

    @on_worker
    def _click_buy_session(self, target_date: str = None) -> bool:
        """找到符合日期（留空則取第一個）且有按鈕的場次並點擊，回傳是否成功。"""
        sessions = self.detect_buy_sessions()
        ok = [s for s in sessions if s["hasButton"] and (not target_date or s["date"] == target_date)]
        if not ok:
            return False
        s = ok[0]
        item = self._page.locator("#buyTicket .sesstion-item").nth(s["index"])
        item.locator("button.nextBtn").first.click()
        self._buy_message = f"已點擊「{s['name']}」（{s['date'] or '未知日期'}）立即購買"
        return True

    @on_worker
    def _reload_page(self):
        self._page.reload()

    @on_worker
    def _wait_after_buy_click(self):
        """點了「立即購買」後等待跳轉到選位頁（.seats-area 出現）。"""
        self._wait_for_areas()

    def _buy_loop(self, target_date, quantity: int, price):
        first = True
        try:
            while not self._buy_stop_event.is_set():
                try:
                    if not first:
                        self._reload_page()
                    if self._click_buy_session(target_date):
                        self._wait_after_buy_click()
                        self._buy_message += "，自動進入搶票流程…"
                        self.start_grab(quantity, price)
                        break
                    self._buy_message = "#buyTicket 尚無可購買按鈕，重新整理中…" + (
                        f"（等待日期 {target_date}）" if target_date else "")
                except Exception as e:
                    self._buy_message = f"嘗試失敗：{e}"
                first = False
                self._buy_stop_event.wait(random.uniform(0.1, 0.3))
            else:
                self._buy_message = "已停止"
        finally:
            self._buy_running = False

    def start_wait_buy(self, target_date: str = None, quantity: int = 1, price=None):
        """背景重複偵測 #buyTicket 按鈕，出現後點擊符合日期的場次，
        成功後自動接著偵測價格並開始搶票，不需手動點擊後續步驟。"""
        if self._buy_running:
            raise RuntimeError("已在偵測中")
        if not self._page:
            raise RuntimeError("請先開啟票區總覽網址")
        self._buy_stop_event.clear()
        self._buy_running = True
        self._buy_message = "開始偵測立即購買按鈕…"
        threading.Thread(target=self._buy_loop, args=(target_date, quantity, price), daemon=True).start()

    def stop_wait_buy(self):
        self._buy_stop_event.set()

    # ── 票區偵測 ────────────────────────────────

    def _wait_for_areas(self, timeout_ms=15_000):
        try:
            self._page.wait_for_selector(".seats-area", timeout=timeout_ms)
        except Exception:
            pass

    def _expand_groups(self):
        """未開賣版面的票區藏在價格群組裡，逐一展開讓它們出現在 DOM。"""
        groups = self._page.locator(".price-group > .v-expansion-panel")
        for i in range(groups.count()):
            g = groups.nth(i)
            if "v-expansion-panel--active" not in (g.get_attribute("class") or ""):
                g.locator("> button.v-expansion-panel-header").click()
                self._page.wait_for_timeout(300)

    @on_worker
    def detect_prices(self) -> list[dict]:
        """偵測頁面上所有票區與價格。"""
        if not self._page:
            raise RuntimeError("請先開啟票區總覽網址")
        self._wait_for_areas()
        self._expand_groups()
        self._areas = self._page.evaluate(_READ_AREAS_JS)
        return self._areas

    @staticmethod
    def _is_available(area: dict) -> bool:
        if area["disabled"] or area["price"] is None:
            return False
        return area["remaining"] is None or area["remaining"] > 0

    # ── 搶票 ────────────────────────────────────

    def _pick_area(self, areas, quantity, price=None):
        """price 指定時只搶該價格；否則在偵測到的價格中隨機挑一個，再從該價格的有票票區隨機挑一個。"""
        ok = [a for a in areas if self._is_available(a)
              and (a["remaining"] is None or a["remaining"] >= quantity)
              and (price is None or a["price"] == price)]
        if not ok:
            return None
        price = random.choice(sorted({a["price"] for a in ok}))
        return random.choice([a for a in ok if a["price"] == price])

    @on_worker
    def _attempt(self, quantity: int, first: bool, price=None) -> bool:
        """做一次嘗試：重新整理 → 偵測 → 隨機選價 → 加票 → 下一步。成功回傳 True。"""
        page = self._page
        if not first:
            refresh = page.locator('button:has-text("更新票數")')
            if refresh.count():
                refresh.first.click()
                page.wait_for_timeout(500)
            else:
                page.reload()
        areas = self.detect_prices()
        if not areas:
            self._message = "找不到票區，等待頁面載入…"
            return False
        area = self._pick_area(areas, quantity, price)
        if not area:
            self._message = "目前沒有可購買的" + (f" NT.{price:,} " if price else "") + "票區，持續重試中…"
            return False

        self._message = f"嘗試 {area['name']} NT.{area['price']:,} × {quantity}"
        if area["group"] >= 0:
            group = page.locator(".price-group > .v-expansion-panel").nth(area["group"])
            if "v-expansion-panel--active" not in (group.get_attribute("class") or ""):
                group.locator("> button.v-expansion-panel-header").click()
                page.wait_for_timeout(300)
        panel = page.locator(".seats-area > .v-expansion-panel").nth(area["index"])
        if "v-expansion-panel--active" not in (panel.get_attribute("class") or ""):
            panel.locator("> button.v-expansion-panel-header").click()
            page.wait_for_timeout(300)

        plus = panel.locator(".count-button button:has(.mdi-plus)")
        if not plus.count():
            self._message = f"{area['name']} 尚無法選票（可能未開賣），重試中…"
            return False
        for _ in range(quantity):
            plus.first.click()
            page.wait_for_timeout(100)

        next_btn = page.locator("button.nextBtn:not([disabled])")
        try:
            next_btn.first.wait_for(timeout=2_000)
        except Exception:
            self._message = f"{area['name']} 下一步無法點擊，重試中…"
            return False
        next_btn.first.click()
        self._message = f"已選 {area['name']} NT.{area['price']:,} × {quantity}，請在瀏覽器完成後續步驟"
        return True

    def _grab_loop(self, quantity: int, price=None):
        first = True
        try:
            while not self._stop_event.is_set():
                try:
                    if self._attempt(quantity, first, price):
                        break
                except Exception as e:
                    self._message = f"嘗試失敗：{e}"
                first = False
                self._stop_event.wait(random.uniform(0.1, 0.3))
            else:
                self._message = "已停止"
        finally:
            self._running = False

    def start_grab(self, quantity: int = 1, price=None):
        """背景重複嘗試，直到成功或被停止。"""
        if self._running:
            raise RuntimeError("已在搶票中")
        if not self._page:
            raise RuntimeError("請先開啟票區總覽網址")
        self._stop_event.clear()
        self._running = True
        self._message = "開始搶票…"
        threading.Thread(target=self._grab_loop, args=(quantity, price), daemon=True).start()

    def stop_grab(self):
        self._stop_event.set()
