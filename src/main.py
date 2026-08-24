import flet as ft
import json
import os
import asyncio
import base64
import requests
import hmac
import hashlib
import time
import math
import urllib3
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from urllib.parse import urlencode

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def round_step(value: Decimal, step_str: str, rounding=ROUND_DOWN) -> Decimal:
    """Membulatkan `value` ke kelipatan `step_str` (mis. stepSize/tickSize dari Binance),
    lalu menormalkan jumlah desimalnya agar sesuai dengan step tersebut.
    Ini yang membuat bot bisa dipakai di SEMUA simbol, bukan cuma BTC/ETH.

    PENTING: Binance sering mengirim stepSize/tickSize dengan nol berlebih di belakang
    (mis. "0.00100000" bukan "0.001"). Kalau tidak dinormalisasi dulu, jumlah desimal
    yang dihitung akan salah (8 desimal, padahal harusnya 3) dan Binance menolak order
    dengan error -1111 "Precision is over the maximum defined for this asset."."""
    step = Decimal(step_str).normalize()
    if step == 0:
        return value
    quotient = (value / step).to_integral_value(rounding=rounding)
    result = quotient * step
    decimals = max(0, -step.as_tuple().exponent)
    quant = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    return result.quantize(quant, rounding=rounding)


def dec_to_str(d: Decimal) -> str:
    """Konversi Decimal ke string untuk dikirim ke Binance. WAJIB pakai ini,
    BUKAN str(d) langsung - karena str(Decimal) Python otomatis berpindah ke
    notasi ilmiah (mis. '1.2E-7') untuk angka sangat kecil, seperti harga
    token receh (SATS, SPELL, DOGS, dll yang harganya < 0.0001). Binance
    menolak/salah-parsing notasi ilmiah, jadi harus dipaksa fixed-point."""
    return format(d, 'f')


# --- BINANCE FUTURES REST CLIENT ---
class BinanceFuturesAPI:
    def __init__(self, api_key, secret_key):
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.base_url = "https://fapi.binance.com"

    def _generate_signature(self, query_string):
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def _headers(self):
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        }

    def _request(self, method, endpoint, params=None):
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)

        query_string = urlencode(params)
        signature = self._generate_signature(query_string)
        url = f"{self.base_url}{endpoint}?{query_string}&signature={signature}"

        if method.upper() == "GET":
            res = requests.get(url, headers=self._headers(), timeout=15, verify=False)
        elif method.upper() == "POST":
            res = requests.post(url, headers=self._headers(), timeout=15, verify=False)
        else:
            res = requests.request(method.upper(), url, headers=self._headers(), timeout=15, verify=False)

        if not res.ok:
            safe_params = {k: v for k, v in params.items() if k != "timestamp"}
            raise requests.exceptions.HTTPError(
                f"HTTP {res.status_code} dari {endpoint} | params dikirim: {safe_params} | respons Binance: {res.text}",
                response=res
            )
        return res.json()

    def set_leverage(self, symbol, leverage):
        params = {
            "symbol": symbol.replace('/', '').upper(),
            "leverage": int(leverage)
        }
        return self._request("POST", "/fapi/v1/leverage", params)

    def get_ticker_price(self, symbol):
        sym = symbol.replace('/', '').upper()
        url = f"{self.base_url}/fapi/v1/ticker/price?symbol={sym}"
        res = requests.get(url, headers={"X-MBX-APIKEY": self.api_key}, timeout=15, verify=False)
        res.raise_for_status()
        return float(res.json()["price"])

    def get_klines(self, symbol, interval, limit=100):
        # Endpoint publik (data candlestick asli), tidak perlu signature.
        sym = symbol.replace('/', '').upper()
        url = f"{self.base_url}/fapi/v1/klines?symbol={sym}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=15, verify=False)
        res.raise_for_status()
        return res.json()

    def get_exchange_info(self, symbol):
        # Endpoint publik, tidak perlu signature.
        sym = symbol.replace('/', '').upper()
        url = f"{self.base_url}/fapi/v1/exchangeInfo?symbol={sym}"
        res = requests.get(url, timeout=15, verify=False)
        res.raise_for_status()
        data = res.json()
        symbols = data.get("symbols", [])
        if not symbols:
            raise Exception(f"Simbol {sym} tidak ditemukan di Binance Futures. Cek ejaan simbolnya.")
        return symbols[0]

    def get_symbol_filters(self, symbol):
        """Ambil stepSize (kelipatan quantity), tickSize (kelipatan harga),
        dan minNotional untuk simbol tertentu. Ini kunci fix masalah desimal."""
        info = self.get_exchange_info(symbol)
        filters = {f['filterType']: f for f in info.get('filters', [])}
        lot = filters.get('LOT_SIZE', {})
        pricef = filters.get('PRICE_FILTER', {})
        min_notional_f = filters.get('MIN_NOTIONAL', {})
        return {
            "qty_step": lot.get('stepSize', '0.001'),
            "min_qty": lot.get('minQty', '0.001'),
            "price_tick": pricef.get('tickSize', '0.01'),
            "min_notional": min_notional_f.get('notional', '5'),
        }

    def create_order(self, symbol, side, order_type, quantity_str, stop_price_str=None, reduce_only=False):
        params = {
            "symbol": symbol.replace('/', '').upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity_str,
        }
        if stop_price_str is not None:
            params["stopPrice"] = stop_price_str
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params)

    def create_algo_order(self, symbol, side, order_type, quantity_str, trigger_price_str, reduce_only=False):
        # Sejak 9 Des 2025 Binance memindahkan order kondisional
        # (STOP_MARKET, TAKE_PROFIT_MARKET, dll) ke endpoint Algo Order.
        # Parameter harganya bernama "triggerPrice", BUKAN "stopPrice".
        sym = symbol.replace('/', '').upper()
        params = {
            "algoType": "CONDITIONAL",
            "symbol": sym,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity_str,
            "triggerPrice": trigger_price_str,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        result = self._request("POST", "/fapi/v1/algoOrder", params)

        # PENTING: HTTP 200 TIDAK berarti order aktif! Binance bisa menerima
        # request (HTTP 200) tapi menolak order-nya sendiri di matching engine
        # (algoStatus jadi REJECTED, bukan NEW) — misalnya karena gagal cek margin.
        status = result.get("algoStatus", "")
        if status != "NEW":
            raise Exception(
                f"Order {order_type} {side} DITOLAK oleh matching engine Binance "
                f"(algoStatus={status}, algoId={result.get('algoId')}). "
                f"qty={quantity_str}, triggerPrice={trigger_price_str}"
            )
        return result

    def cancel_all_algo_orders(self, symbol):
        sym = symbol.replace('/', '').upper()
        return self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": sym})

    def get_position_amt(self, symbol):
        sym = symbol.replace('/', '').upper()
        result = self._request("GET", "/fapi/v3/positionRisk", {"symbol": sym})
        if isinstance(result, list):
            return sum(float(p.get("positionAmt", 0)) for p in result)
        return 0.0

    def get_open_algo_orders(self, symbol):
        # PENTING: endpoint GET ini urutannya "openAlgoOrders" (bukan "algoOpenOrders"
        # seperti endpoint DELETE cancel_all_algo_orders di bawah - dua nama yang mirip
        # tapi urutan katanya kebalik). Salah tulis di sini bikin cleanup gagal diam-diam.
        sym = symbol.replace('/', '').upper()
        result = self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
        return result if isinstance(result, list) else []

    def cleanup_orphan_orders(self, symbol):
        """Binance TIDAK menghubungkan TP dan SL (dikonfirmasi di changelog resmi:
        conditional order bergantung pada POSISI, bukan pada order lawannya). Jadi kalau
        salah satu (TP atau SL) kena duluan dan menutup posisi, yang satu lagi TIDAK
        otomatis terbatalkan - dia 'tertinggal' menggantung selamanya sampai dibatalkan manual.

        Deteksi: kalau posisi sudah flat (0) DAN tidak ada order entry (non-reduceOnly)
        yang masih menunggu, TAPI masih ada order reduceOnly (TP/SL) terbuka -> itu pasti
        order yatim dari posisi yang sudah ditutup duluan. Aman dibatalkan.
        Kalau entry masih ada & pending, itu kondisi normal menunggu trigger - tidak disentuh."""
        sym = symbol.replace('/', '').upper()
        open_orders = self.get_open_algo_orders(sym)
        if not open_orders:
            return 0, "Tidak ada order terbuka untuk simbol ini."

        pos_amt = self.get_position_amt(sym)
        if pos_amt != 0:
            return 0, "Posisi masih terbuka - order yang ada masih relevan, tidak disentuh."

        entry_masih_menunggu = any(not o.get("reduceOnly", False) for o in open_orders)
        if entry_masih_menunggu:
            return 0, "Masih menunggu order entry ter-trigger - kondisi normal, tidak disentuh."

        sisa = [o for o in open_orders if o.get("reduceOnly", False)]
        if sisa:
            self.cancel_all_algo_orders(sym)
            return len(sisa), f"{len(sisa)} order TP/SL yatim (posisi sudah flat, entry sudah tidak ada) dibatalkan."
        return 0, "Tidak ada yang perlu dibersihkan."

    def get_income_history(self, start_time_ms, income_type="REALIZED_PNL"):
        params = {
            "incomeType": income_type,
            "startTime": start_time_ms,
            "limit": 1000
        }
        return self._request("GET", "/fapi/v1/income", params)


def _extract_claude_text(data):
    content_blocks = data.get("content", [])
    for block in content_blocks:
        if block.get("type") == "text" and "text" in block:
            return block["text"]

    # Tidak ada blok teks. Cek apakah ini karena budget token habis duluan
    # oleh proses "thinking" sebelum sempat menghasilkan jawaban (kasus umum).
    stop_reason = data.get("stop_reason", "")
    if stop_reason == "max_tokens":
        raise Exception(
            "Respons Claude terpotong karena max_tokens habis sebelum menghasilkan teks "
            "(kemungkinan besar terpakai untuk 'thinking'). Naikkan nilai max_tokens di kode. "
            f"Raw: {json.dumps(data)[:500]}"
        )
    raise Exception(f"Tidak ada blok teks pada respons Claude (stop_reason={stop_reason}). Raw: {json.dumps(data)[:500]}")


def call_claude_vision_api(api_key, image_path, prompt):
    clean_key = api_key.strip()
    with open(image_path, "rb") as image_file:
        raw_bytes = image_file.read()
        encoded_image = base64.b64encode(raw_bytes).decode("utf-8")
    ext = os.path.splitext(image_path)[1].lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    headers = {
        "x-api-key": clean_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded_image}},
                {"type": "text", "text": prompt}
            ]
        }]
    }
    res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90, verify=False)
    res.raise_for_status()
    return _extract_claude_text(res.json())


def call_claude_text_api(api_key, prompt):
    """Versi teks-murni (tanpa gambar) - dipakai mode otomatis berkala yang
    menganalisis data candlestick numerik langsung dari Binance, bukan screenshot."""
    clean_key = api_key.strip()
    headers = {
        "x-api-key": clean_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}]
    }
    res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90, verify=False)
    res.raise_for_status()
    return _extract_claude_text(res.json())


def parse_json_setup(raw_response):
    """Parsing JSON dari teks AI, dengan pesan error yang jelas (bukan JSONDecodeError opak)."""
    raw_text = raw_response.strip().replace('```json', '').replace('```', '')
    start = raw_text.find('{')
    end = raw_text.rfind('}') + 1
    if start == -1 or end <= start:
        raise Exception(f"AI tidak mengembalikan JSON. Teks asli: {raw_text[:500]}")
    try:
        return json.loads(raw_text[start:end])
    except json.JSONDecodeError as jde:
        raise Exception(f"Gagal parse JSON dari AI: {jde}. Teks asli: {raw_text[:500]}")


# --- APLIKASI FLET UTAMA ---
async def main(page: ft.Page):
    page.title = "Sniper Bot Mobile"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO

    sp = ft.SharedPreferences()
    file_picker = ft.FilePicker()

    saved_api_ai = await sp.get("api_ai") or ""
    saved_api_bin = await sp.get("api_bin") or ""
    saved_api_sec = await sp.get("api_sec") or ""
    saved_margin = await sp.get("margin") or "10"
    saved_leverage = await sp.get("leverage") or "20"
    saved_max_loss = await sp.get("max_loss") or "50"

    async def save_data(e):
        await sp.set("api_ai", api_ai.value or "")
        await sp.set("api_bin", api_bin.value or "")
        await sp.set("api_sec", api_sec.value or "")
        await sp.set("margin", input_margin.value or "")
        await sp.set("leverage", input_lev.value or "")
        await sp.set("max_loss", input_max_loss.value or "")

    api_ai = ft.TextField(label="Claude API Key", password=True, can_reveal_password=True, value=saved_api_ai, on_blur=save_data)
    api_bin = ft.TextField(label="Binance API Key", password=True, can_reveal_password=True, value=saved_api_bin, on_blur=save_data)
    api_sec = ft.TextField(label="Binance Secret", password=True, can_reveal_password=True, value=saved_api_sec, on_blur=save_data)
    input_margin = ft.TextField(label="Margin (USDT)", value=saved_margin, on_blur=save_data)
    input_lev = ft.TextField(label="Leverage", value=saved_leverage, on_blur=save_data)
    input_symbol = ft.TextField(label="Simbol (wajib diisi untuk Mode Otomatis, opsional untuk mode foto)", value="")
    input_max_loss = ft.TextField(label="Batas Rugi 24 Jam (USDT)", value=saved_max_loss, on_blur=save_data)
    input_candle_interval = ft.TextField(label="Interval Candle (mis. 15m, 1h, 4h)", value="15m")
    input_auto_interval = ft.TextField(label="Jalankan Analisis Tiap (menit)", value="15")

    path_foto = ft.Text("Belum ada foto dipilih")
    layar_log = ft.Text("Status: Standby", color=ft.Colors.YELLOW)

    log_list = ft.ListView(height=200, spacing=2, auto_scroll=True)

    def catat_log(msg):
        ts = time.strftime("%H:%M:%S")
        log_list.controls.append(ft.Text(f"[{ts}] {msg}", size=12))
        if len(log_list.controls) > 150:
            log_list.controls.pop(0)
        log_list.update()

    launch_button = ft.Button("LUNCURKAN OTOMATIS (FOTO)", bgcolor=ft.Colors.RED_800, color=ft.Colors.WHITE)
    cleanup_button = ft.Button("BERSIHKAN ORDER SISA (SIMBOL DI ATAS)", bgcolor=ft.Colors.ORANGE_800, color=ft.Colors.WHITE)
    switch_auto = ft.Switch(label="MODE OTOMATIS BERKALA — TANPA konfirmasi manual", value=False)
    auto_status_text = ft.Text("Mode otomatis: NONAKTIF", color=ft.Colors.GREY)

    auto_task_holder = {"task": None}

    async def pick_files_click(e):
        files = await file_picker.pick_files()
        if files and len(files) > 0:
            path_foto.value = files[0].path
            path_foto.update()

    def _show_dialog(dialog):
        if hasattr(page, "open"):
            page.open(dialog)
        else:
            if dialog not in page.overlay:
                page.overlay.append(dialog)
            dialog.open = True
            page.update()

    def _close_dialog(dialog):
        if hasattr(page, "close"):
            page.close(dialog)
        else:
            dialog.open = False
            page.update()

    def tampilkan_peringatan(judul, pesan):
        layar_log.value = f"Status: {judul}\n\nDetail: {pesan}"
        layar_log.color = ft.Colors.RED
        layar_log.update()

        def tutup_dialog(e):
            _close_dialog(dialog)

        dialog = ft.AlertDialog(
            title=ft.Text(judul),
            content=ft.Text(pesan),
            actions=[ft.TextButton("OK", on_click=tutup_dialog)]
        )
        _show_dialog(dialog)

    async def minta_konfirmasi(ringkasan_text):
        hasil = {"confirmed": False}
        event = asyncio.Event()

        def on_ya(e):
            hasil["confirmed"] = True
            _close_dialog(dialog)
            event.set()

        def on_batal(e):
            hasil["confirmed"] = False
            _close_dialog(dialog)
            event.set()

        dialog = ft.AlertDialog(
            title=ft.Text("Konfirmasi Order Nyata"),
            content=ft.Text(ringkasan_text),
            actions=[
                ft.TextButton("BATAL", on_click=on_batal),
                ft.TextButton("YA, LANJUTKAN", on_click=on_ya),
            ]
        )
        _show_dialog(dialog)
        await event.wait()
        return hasil["confirmed"]

    async def evaluasi_dan_eksekusi(setup, sym, is_auto=False):
        """Inti logika validasi + eksekusi, dipakai baik oleh mode foto (manual)
        maupun mode otomatis berkala. is_auto=True -> lewati dialog konfirmasi,
        dan pakai log berjalan (bukan popup) sebagai output utama."""

        def _log(msg, is_error=False):
            catat_log(f"{sym}: {msg}")
            if not is_auto:
                layar_log.value = f"Status: {msg}"
                layar_log.color = ft.Colors.RED if is_error else ft.Colors.BLUE
                page.update()

        binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
        loop = asyncio.get_running_loop()
        margin = float(input_margin.value)
        lev = int(input_lev.value)
        arah = setup.get('arah')

        # Cegah PENUMPUKAN order: kalau sudah ada posisi atau order aktif untuk
        # simbol ini, jangan buka set entry/TP/SL baru di atasnya. Ini akar
        # masalah "order menumpuk jadi 4/5" yang terjadi sebelumnya.
        _log("Mengecek posisi/order aktif yang sudah ada...")
        try:
            pos_amt_cek = await loop.run_in_executor(None, lambda: binance.get_position_amt(sym))
            order_aktif_cek = await loop.run_in_executor(None, lambda: binance.get_open_algo_orders(sym))
        except Exception as e:
            _log(f"Gagal cek posisi/order aktif - {e}", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Gagal Cek Status Posisi", str(e))
            return "error"

        if pos_amt_cek != 0 or order_aktif_cek:
            pesan = (
                f"Sudah ada posisi (amt={pos_amt_cek}) dan/atau {len(order_aktif_cek)} order terbuka "
                f"untuk {sym}. Tidak membuka posisi baru di atasnya - selesaikan/tutup dulu yang ada."
            )
            _log(pesan)
            if not is_auto:
                tampilkan_peringatan("Sudah Ada Posisi/Order Aktif", pesan)
            return "sudah_aktif"

        _log("Mengambil data pasar & aturan presisi...")
        try:
            current_price = await loop.run_in_executor(None, lambda: binance.get_ticker_price(sym))
            filters = await loop.run_in_executor(None, lambda: binance.get_symbol_filters(sym))
        except Exception as e:
            _log(f"Gagal ambil data pasar - {e}", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Gagal Ambil Data Pasar", str(e))
            return "error"

        try:
            entry = Decimal(str(setup['pemicu_masuk']))
            tp = Decimal(str(setup['take_profit']))
            sl = Decimal(str(setup['stop_loss']))
        except (InvalidOperation, KeyError, TypeError):
            _log("Data harga dari AI tidak valid.", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Data AI Tidak Valid", "Respons AI tidak berisi angka harga yang bisa dibaca.")
            return "error"

        current_price_dec = Decimal(str(current_price))
        masalah = []
        if arah == 'BUY':
            if not (sl < entry < tp):
                masalah.append(f"Urutan harga tidak masuk akal untuk BUY. SL={sl}, Entry={entry}, TP={tp}")
        elif arah == 'SELL':
            if not (tp < entry < sl):
                masalah.append(f"Urutan harga tidak masuk akal untuk SELL. TP={tp}, Entry={entry}, SL={sl}")
        else:
            masalah.append(f"Arah tidak dikenali: '{arah}'")

        deviasi = abs(entry - current_price_dec) / current_price_dec if current_price_dec != 0 else Decimal(1)
        if deviasi > Decimal("0.10"):
            masalah.append(f"Entry menyimpang {float(deviasi)*100:.1f}% dari harga pasar ({current_price}).")

        if masalah:
            pesan = "\n".join(masalah)
            _log(f"Validasi gagal - {pesan}", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Validasi Harga Gagal", pesan)
            return "error"

        raw_size = Decimal(str((margin * lev))) / current_price_dec
        size_dec = round_step(raw_size, filters['qty_step'], ROUND_DOWN)
        min_qty = Decimal(filters['min_qty'])
        if size_dec < min_qty or size_dec <= 0:
            _log(f"Quantity ({size_dec}) di bawah minimum ({min_qty}).", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Ukuran Posisi Terlalu Kecil", f"Quantity ({size_dec}) di bawah minimum ({min_qty}). Naikkan margin/leverage.")
            return "error"

        notional = size_dec * current_price_dec
        min_notional = Decimal(filters['min_notional'])
        if notional < min_notional:
            _log(f"Notional ({notional:.2f}) di bawah minimum ({min_notional}).", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Notional Terlalu Kecil", f"Nilai order ({notional:.2f} USDT) di bawah minimum ({min_notional} USDT).")
            return "error"

        entry_dec = round_step(entry, filters['price_tick'], ROUND_HALF_UP)
        tp_dec = round_step(tp, filters['price_tick'], ROUND_HALF_UP)
        sl_dec = round_step(sl, filters['price_tick'], ROUND_HALF_UP)

        start_24h_ms = int(time.time() * 1000) - 24 * 3600 * 1000
        try:
            income_list = await loop.run_in_executor(None, lambda: binance.get_income_history(start_24h_ms))
        except Exception as e:
            _log(f"Gagal cek riwayat PnL - {e}", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Gagal Cek Batas Rugi", str(e))
            return "error"
        total_pnl_24h = sum(float(item.get('income', 0)) for item in income_list)
        max_loss = abs(float(input_max_loss.value or "0"))

        if max_loss > 0 and total_pnl_24h <= -max_loss:
            _log(f"BATAS RUGI TERCAPAI: PnL 24 jam {total_pnl_24h:.2f} USDT <= -{max_loss:.2f} USDT.", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Batas Rugi Tercapai", f"PnL 24 jam: {total_pnl_24h:.2f} USDT. Eksekusi dihentikan.")
            return "batas_rugi"

        ringkasan = (
            f"Simbol: {sym}\nArah: {arah}\nHarga Pasar: {current_price}\n"
            f"Entry: {dec_to_str(entry_dec)}\nTP: {dec_to_str(tp_dec)}\nSL: {dec_to_str(sl_dec)}\nQuantity: {dec_to_str(size_dec)}\n"
            f"Notional: {notional:.2f} USDT\nMargin: {margin} | Leverage: {lev}x\n"
            f"stepSize: {filters['qty_step']} | tickSize: {filters['price_tick']}\n"
            f"PnL 24 Jam: {total_pnl_24h:.2f} USDT\n\n"
            f"Order akan LANGSUNG dieksekusi dengan uang sungguhan. Lanjutkan?"
        )

        if is_auto:
            _log(f"MODE OTOMATIS - eksekusi TANPA konfirmasi. Setup: arah={arah} entry={entry_dec} tp={tp_dec} sl={sl_dec} qty={size_dec}")
        else:
            layar_log.value = "Status: Menunggu konfirmasi Anda..."
            layar_log.color = ft.Colors.YELLOW
            page.update()
            setuju = await minta_konfirmasi(ringkasan)
            if not setuju:
                layar_log.value = "Status: Dibatalkan oleh pengguna."
                layar_log.color = ft.Colors.YELLOW
                page.update()
                catat_log(f"{sym}: Dibatalkan oleh pengguna.")
                return "batal"

        _log("Mengeksekusi order ke Binance...")
        try:
            await loop.run_in_executor(None, lambda: binance.set_leverage(sym, lev))
            side_u, side_p = ('BUY', 'SELL') if arah == 'BUY' else ('SELL', 'BUY')
            qty_str = dec_to_str(size_dec)

            await loop.run_in_executor(None, lambda: binance.create_algo_order(
                sym, side_u, 'STOP_MARKET', qty_str, trigger_price_str=dec_to_str(entry_dec)
            ))
            await loop.run_in_executor(None, lambda: binance.create_algo_order(
                sym, side_p, 'TAKE_PROFIT_MARKET', qty_str, trigger_price_str=dec_to_str(tp_dec), reduce_only=True
            ))
            await loop.run_in_executor(None, lambda: binance.create_algo_order(
                sym, side_p, 'STOP_MARKET', qty_str, trigger_price_str=dec_to_str(sl_dec), reduce_only=True
            ))
        except Exception as order_err:
            try:
                await loop.run_in_executor(None, lambda: binance.cancel_all_algo_orders(sym))
                rollback_msg = "Semua order untuk simbol ini otomatis DIBATALKAN demi keamanan."
            except Exception as cancel_err:
                rollback_msg = f"GAGAL membatalkan otomatis! CEK MANUAL DI BINANCE SEKARANG. Error: {cancel_err}"
            _log(f"ORDER DITOLAK/GAGAL - {order_err}. {rollback_msg}", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Order Ditolak/Gagal - Rollback Dijalankan", f"{order_err}\n\n{rollback_msg}")
            return "error"

        _log("TRIPLE SHOT BERHASIL (Entry/TP/SL semua aktif).")
        if not is_auto:
            layar_log.value = "Status: TRIPLE SHOT BERHASIL! (Entry, TP, SL semua aktif)"
            layar_log.color = ft.Colors.GREEN
            page.update()
        return "success"

    async def luncurkan_execution():
        launch_button.disabled = True
        launch_button.text = "MEMPROSES..."
        page.update()

        try:
            if not path_foto.value or "Belum ada" in path_foto.value:
                tampilkan_peringatan("Foto Belum Dipilih", "Silakan upload foto chart terlebih dahulu sebelum meluncurkan bot.")
                return

            layar_log.value = "Status: Menghubungi Claude AI..."
            layar_log.color = ft.Colors.BLUE
            page.update()

            prompt = """
            Anda adalah sistem penembak jitu trading crypto. Analisis chart/gambar ini dengan sangat teliti.

            Langkah 1: Baca nama pair trading yang tertera di judul/header chart (biasanya di pojok kiri atas,
            format seperti "BTCUSDT", "ETH/USDT", atau "SOL/USDT Perp"). Tuliskan hasilnya sebagai kode simbol
            futures USDT-M tanpa spasi/garis miring, contoh: BTCUSDT, ETHUSDT, SOLUSDT.
            Jika judul pair sama sekali tidak terlihat/tidak bisa dibaca di gambar, kosongkan field "simbol".

            Langkah 2: Perhatikan struktur harga terbaru yang tertera di chart.
            Tentukan setup Stop Market (Entri, Take Profit, dan Stop Loss) berdasarkan analisis teknikal chart tersebut.

            Keluarkan HANYA format JSON murni tanpa kata-kata pembuka/penutup. Format wajib:
            {
                "sinyal": "VALID",
                "simbol": "BTCUSDT",
                "arah": "BUY",
                "pemicu_masuk": 0.00000,
                "take_profit": 0.00000,
                "stop_loss": 0.00000
            }
            Jika tidak ada momentum atau chart kurang jelas untuk dianalisis, ubah status "sinyal" menjadi "TIDAK VALID".
            """

            loop = asyncio.get_running_loop()
            raw_response = await loop.run_in_executor(
                None, lambda: call_claude_vision_api(api_ai.value, path_foto.value, prompt)
            )

            try:
                setup = parse_json_setup(raw_response)
            except Exception as pe:
                tampilkan_peringatan("Gagal Parse Respons AI", str(pe))
                return

            if setup.get('sinyal') != "VALID":
                tampilkan_peringatan("Sinyal Tidak Valid", "AI menilai chart saat ini kurang jelas atau tidak ada momentum yang aman untuk masuk pasar.")
                return

            manual_sym = input_symbol.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
            ai_sym_raw = str(setup.get('simbol', '') or '').strip().upper()
            ai_sym = ai_sym_raw.replace('/', '').replace('-', '').replace(' ', '').replace('PERP', '')

            if manual_sym:
                sym = manual_sym
            elif ai_sym:
                if not ai_sym.endswith('USDT'):
                    ai_sym += 'USDT'
                sym = ai_sym
            else:
                tampilkan_peringatan(
                    "Simbol Tidak Terdeteksi",
                    "AI tidak berhasil membaca nama pair dari screenshot, dan kolom Simbol dikosongkan. "
                    "Isi manual kolom Simbol, atau upload screenshot yang judul pair-nya terlihat jelas."
                )
                return

            await evaluasi_dan_eksekusi(setup, sym, is_auto=False)

        except requests.exceptions.HTTPError as err:
            tampilkan_peringatan("Gagal REST API", str(err))
        except Exception as ex:
            tampilkan_peringatan("Terjadi Kesalahan", f"{str(ex)}")
        finally:
            launch_button.disabled = False
            launch_button.text = "LUNCURKAN OTOMATIS (FOTO)"
            page.update()

    def on_luncurkan_click(e):
        asyncio.create_task(luncurkan_execution())

    launch_button.on_click = on_luncurkan_click

    async def bersihkan_order_sisa_click():
        cleanup_button.disabled = True
        cleanup_button.text = "MEMERIKSA..."
        page.update()
        try:
            sym = input_symbol.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
            if not sym:
                tampilkan_peringatan("Simbol Kosong", "Isi kolom Simbol dulu untuk cek order sisa.")
                return
            if not sym.endswith("USDT"):
                sym += "USDT"
            binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
            loop = asyncio.get_running_loop()
            jumlah, pesan = await loop.run_in_executor(None, lambda: binance.cleanup_orphan_orders(sym))
            catat_log(f"{sym}: [Cek Manual] {pesan}")
            if jumlah > 0:
                tampilkan_peringatan("Order Sisa Dibersihkan", f"{sym}: {pesan}")
            else:
                tampilkan_peringatan("Hasil Pengecekan", f"{sym}: {pesan}")
        except Exception as ex:
            tampilkan_peringatan("Gagal Cek Order Sisa", str(ex))
        finally:
            cleanup_button.disabled = False
            cleanup_button.text = "BERSIHKAN ORDER SISA (SIMBOL DI ATAS)"
            page.update()

    def on_cleanup_click(e):
        asyncio.create_task(bersihkan_order_sisa_click())

    cleanup_button.on_click = on_cleanup_click

    # ---------- MODE OTOMATIS BERKALA (tanpa screenshot, tanpa konfirmasi) ----------

    async def jalankan_siklus_otomatis():
        sym = input_symbol.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
        if not sym:
            catat_log("Simbol kosong - Mode Otomatis butuh kolom Simbol diisi manual (tidak ada screenshot untuk dibaca otomatis).")
            return "error"
        if not sym.endswith("USDT"):
            sym += "USDT"

        interval_candle = (input_candle_interval.value or "15m").strip()
        binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
        loop = asyncio.get_running_loop()

        # Bersihkan dulu order TP/SL yatim (kalau ada) dari siklus sebelumnya,
        # sebelum menganalisis dan mempertimbangkan posisi baru.
        try:
            jumlah, pesan = await loop.run_in_executor(None, lambda: binance.cleanup_orphan_orders(sym))
            if jumlah > 0:
                catat_log(f"{sym}: [Housekeeping] {pesan}")
        except Exception as e:
            catat_log(f"{sym}: Gagal cek order sisa (lanjut tetap) - {e}")

        try:
            klines = await loop.run_in_executor(None, lambda: binance.get_klines(sym, interval_candle, 100))
        except Exception as e:
            catat_log(f"{sym}: Gagal ambil data candlestick - {e}")
            return "error"

        candle_terakhir = klines[-30:] if len(klines) >= 30 else klines
        ringkasan_candle = "\n".join(
            f"{i}: Open={k[1]} High={k[2]} Low={k[3]} Close={k[4]} Volume={k[5]}"
            for i, k in enumerate(candle_terakhir)
        )

        prompt_text = f"""Anda adalah sistem analisis teknikal trading crypto untuk {sym} pada timeframe {interval_candle}.
Berikut data {len(candle_terakhir)} candlestick historis terakhir (paling lama ke paling baru), format: index: Open, High, Low, Close, Volume:
{ringkasan_candle}

Tentukan setup Stop Market (Entri, Take Profit, Stop Loss) berdasarkan analisis teknikal murni dari angka di atas.
Keluarkan HANYA JSON murni tanpa kata pembuka/penutup, format wajib:
{{
    "sinyal": "VALID",
    "arah": "BUY",
    "pemicu_masuk": 0.00000,
    "take_profit": 0.00000,
    "stop_loss": 0.00000
}}
Jika tidak ada momentum/setup yang cukup jelas, set "sinyal" menjadi "TIDAK VALID"."""

        try:
            raw_response = await loop.run_in_executor(None, lambda: call_claude_text_api(api_ai.value, prompt_text))
        except Exception as e:
            catat_log(f"{sym}: Gagal hubungi Claude - {e}")
            return "error"

        try:
            setup = parse_json_setup(raw_response)
        except Exception as pe:
            catat_log(f"{sym}: {pe}")
            return "error"

        if setup.get('sinyal') != "VALID":
            catat_log(f"{sym}: Tidak ada sinyal valid pada siklus ini.")
            return "no_signal"

        catat_log(f"{sym}: Sinyal {setup.get('arah')} terdeteksi (entry={setup.get('pemicu_masuk')}, TP={setup.get('take_profit')}, SL={setup.get('stop_loss')}). Memvalidasi & mengeksekusi...")

        return await evaluasi_dan_eksekusi(setup, sym, is_auto=True)

    async def loop_otomatis():
        catat_log("=== MODE OTOMATIS AKTIF ===")
        while switch_auto.value:
            try:
                status = await jalankan_siklus_otomatis()
            except Exception as e:
                catat_log(f"ERROR TAK TERDUGA di siklus otomatis: {e}")
                status = "error"

            if status in ("error", "batas_rugi"):
                catat_log("Mode otomatis DIHENTIKAN otomatis (circuit breaker). Perbaiki masalahnya lalu aktifkan switch lagi kalau ingin lanjut.")
                switch_auto.value = False
                auto_status_text.value = "Mode otomatis: BERHENTI OTOMATIS (lihat log)"
                auto_status_text.color = ft.Colors.RED
                switch_auto.update()
                auto_status_text.update()
                break

            try:
                interval_menit = max(1.0, float(input_auto_interval.value or "15"))
            except ValueError:
                interval_menit = 15.0
            catat_log(f"Siklus selesai (status={status}). Menunggu {interval_menit:.0f} menit sampai siklus berikutnya...")
            auto_status_text.value = f"Mode otomatis: AKTIF (siklus berikutnya ~{interval_menit:.0f} menit lagi)"
            auto_status_text.color = ft.Colors.GREEN
            auto_status_text.update()
            await asyncio.sleep(interval_menit * 60)

        auto_task_holder["task"] = None

    def on_switch_auto_change(e):
        if switch_auto.value:
            if auto_task_holder["task"] is None:
                auto_status_text.value = "Mode otomatis: AKTIF (memulai siklus pertama...)"
                auto_status_text.color = ft.Colors.GREEN
                auto_status_text.update()
                auto_task_holder["task"] = asyncio.create_task(loop_otomatis())
        else:
            catat_log("Mode otomatis dimatikan oleh pengguna.")
            auto_status_text.value = "Mode otomatis: NONAKTIF"
            auto_status_text.color = ft.Colors.GREY
            auto_status_text.update()
            t = auto_task_holder["task"]
            if t is not None:
                t.cancel()
                auto_task_holder["task"] = None

    switch_auto.on_change = on_switch_auto_change

    page.add(
        ft.Column([
            ft.Text("PUSAT KOMANDO SNIPER", size=20, weight="bold"),
            api_ai, api_bin, api_sec, input_margin, input_lev, input_symbol, input_max_loss,
            ft.Divider(),
            ft.Text("Mode Foto (manual, dengan konfirmasi)", size=14, weight="bold"),
            ft.Button("UPLOAD FOTO", on_click=pick_files_click),
            path_foto,
            launch_button,
            layar_log,
            ft.Divider(),
            ft.Text("Perawatan Order (cek/bersihkan order TP/SL yang tertinggal)", size=14, weight="bold", color=ft.Colors.ORANGE),
            cleanup_button,
            ft.Divider(),
            ft.Text("Mode Otomatis Berkala (data candlestick asli, TANPA konfirmasi)", size=14, weight="bold", color=ft.Colors.RED),
            input_candle_interval,
            input_auto_interval,
            switch_auto,
            auto_status_text,
            ft.Text("Log Aktivitas:", size=12, weight="bold"),
            ft.Container(content=log_list, bgcolor=ft.Colors.BLACK26, padding=5, border_radius=5),
        ])
    )

if __name__ == "__main__":
    ft.run(main)