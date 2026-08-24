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
    lalu menormalkan jumlah desimalnya agar sesuai dengan step tersebut."""
    step = Decimal(step_str).normalize()
    if step == 0:
        return value
    quotient = (value / step).to_integral_value(rounding=rounding)
    result = quotient * step
    decimals = max(0, -step.as_tuple().exponent)
    quant = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    return result.quantize(quant, rounding=rounding)


_exchange_info_cache = {"data": None, "fetched_at": 0}


def dec_to_str(d: Decimal) -> str:
    """WAJIB pakai ini, BUKAN str(d) langsung - str(Decimal) Python otomatis
    berpindah ke notasi ilmiah (mis. '1.2E-7') untuk angka sangat kecil."""
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
        params = {"symbol": symbol.replace('/', '').upper(), "leverage": int(leverage)}
        return self._request("POST", "/fapi/v1/leverage", params)

    def get_ticker_price(self, symbol):
        sym = symbol.replace('/', '').upper()
        url = f"{self.base_url}/fapi/v1/ticker/price?symbol={sym}"
        res = requests.get(url, headers={"X-MBX-APIKEY": self.api_key}, timeout=15, verify=False)
        res.raise_for_status()
        return float(res.json()["price"])

    def get_klines(self, symbol, interval, limit=100):
        sym = symbol.replace('/', '').upper()
        url = f"{self.base_url}/fapi/v1/klines?symbol={sym}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=15, verify=False)
        res.raise_for_status()
        return res.json()

    def get_exchange_info(self, symbol):
        """PENTING: /fapi/v1/exchangeInfo TIDAK punya parameter query sama sekali,
        selalu mengembalikan SEMUA simbol. Kita WAJIB mencari simbol yang tepat
        sendiri di dalam daftar itu - jangan asal ambil elemen pertama."""
        sym = symbol.replace('/', '').upper()
        now = time.time()
        if _exchange_info_cache["data"] is None or (now - _exchange_info_cache["fetched_at"]) > 600:
            url = f"{self.base_url}/fapi/v1/exchangeInfo"
            res = requests.get(url, timeout=15, verify=False)
            res.raise_for_status()
            _exchange_info_cache["data"] = res.json()
            _exchange_info_cache["fetched_at"] = now
        for s in _exchange_info_cache["data"].get("symbols", []):
            if s.get("symbol") == sym:
                return s
        raise Exception(f"Simbol {sym} tidak ditemukan di Binance Futures. Cek ejaan simbolnya.")

    def get_symbol_filters(self, symbol):
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

    def create_algo_order(self, symbol, side, order_type, quantity_str, trigger_price_str, reduce_only=False):
        sym = symbol.replace('/', '').upper()
        params = {
            "algoType": "CONDITIONAL", "symbol": sym, "side": side.upper(),
            "type": order_type.upper(), "quantity": quantity_str, "triggerPrice": trigger_price_str,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        result = self._request("POST", "/fapi/v1/algoOrder", params)
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
        # PENTING: endpoint GET ini "openAlgoOrders" (bukan "algoOpenOrders").
        sym = symbol.replace('/', '').upper()
        result = self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": sym})
        return result if isinstance(result, list) else []

    def cleanup_orphan_orders(self, symbol, log_fn=None):
        """Binance TIDAK menghubungkan TP dan SL. Kalau salah satu kena duluan dan
        menutup posisi, yang satu lagi tertinggal menggantung. Dicek 2x dengan jeda
        sebelum benar-benar dibatalkan (jaga-jaga race condition)."""
        def _log(msg):
            if log_fn:
                log_fn(msg)

        sym = symbol.replace('/', '').upper()

        def _cek_sekali():
            open_orders = self.get_open_algo_orders(sym)
            pos_amt = self.get_position_amt(sym)
            entry_pending = any(not o.get("reduceOnly", False) for o in open_orders)
            sisa = [o for o in open_orders if o.get("reduceOnly", False)]
            return open_orders, pos_amt, entry_pending, sisa

        open_orders, pos_amt, entry_pending, sisa = _cek_sekali()
        if not open_orders:
            return 0, "Tidak ada order terbuka untuk simbol ini."
        if pos_amt != 0:
            return 0, "Posisi masih terbuka - order yang ada masih relevan, tidak disentuh."
        if entry_pending:
            return 0, "Masih menunggu order entry ter-trigger - kondisi normal, tidak disentuh."
        if not sisa:
            return 0, "Tidak ada yang perlu dibersihkan."

        _log(f"{sym}: [Cek-1] Terindikasi order yatim ({len(sisa)} order, ids={[o.get('algoId') for o in sisa]}, pos={pos_amt}). Verifikasi ulang...")
        time.sleep(2)

        open_orders2, pos_amt2, entry_pending2, sisa2 = _cek_sekali()
        if pos_amt2 != 0 or entry_pending2 or not sisa2:
            _log(f"{sym}: [Cek-2] Kondisi berubah - DIBATALKAN TIDAK JADI (kemungkinan race condition).")
            return 0, "Verifikasi ulang tidak konsisten - order TIDAK dibatalkan (jaga-jaga)."

        ids_final = [o.get('algoId') for o in sisa2]
        _log(f"{sym}: [Cek-2] Terkonfirmasi. Membatalkan order yatim ids={ids_final}...")
        self.cancel_all_algo_orders(sym)
        return len(sisa2), f"{len(sisa2)} order TP/SL yatim (terkonfirmasi 2x) dibatalkan. ids={ids_final}"

    def get_income_history(self, start_time_ms, income_type="REALIZED_PNL"):
        params = {"incomeType": income_type, "startTime": start_time_ms, "limit": 1000}
        return self._request("GET", "/fapi/v1/income", params)


def _extract_claude_text(data):
    content_blocks = data.get("content", [])
    for block in content_blocks:
        if block.get("type") == "text" and "text" in block:
            return block["text"]
    stop_reason = data.get("stop_reason", "")
    if stop_reason == "max_tokens":
        raise Exception(
            "Respons Claude terpotong karena max_tokens habis sebelum menghasilkan teks "
            f"(kemungkinan terpakai untuk 'thinking'). Raw: {json.dumps(data)[:500]}"
        )
    raise Exception(f"Tidak ada blok teks pada respons Claude (stop_reason={stop_reason}). Raw: {json.dumps(data)[:500]}")


def call_claude_vision_api(api_key, image_path, prompt):
    clean_key = api_key.strip()
    with open(image_path, "rb") as image_file:
        encoded_image = base64.b64encode(image_file.read()).decode("utf-8")
    ext = os.path.splitext(image_path)[1].lower()
    media_type = "image/png" if ext == ".png" else "image/jpeg"
    headers = {"x-api-key": clean_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {
        "model": "claude-sonnet-5", "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded_image}},
            {"type": "text", "text": prompt}
        ]}]
    }
    res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90, verify=False)
    res.raise_for_status()
    return _extract_claude_text(res.json())


def call_claude_text_api(api_key, prompt):
    clean_key = api_key.strip()
    headers = {"x-api-key": clean_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": "claude-sonnet-5", "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}
    res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90, verify=False)
    res.raise_for_status()
    return _extract_claude_text(res.json())


def parse_json_setup(raw_response):
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

    # ---------- Dialog helpers (dipakai bersama semua bagian) ----------
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
        def tutup_dialog(e):
            _close_dialog(dialog)
        dialog = ft.AlertDialog(
            title=ft.Text(judul), content=ft.Text(pesan),
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
            title=ft.Text("Konfirmasi Order Nyata"), content=ft.Text(ringkasan_text),
            actions=[ft.TextButton("BATAL", on_click=on_batal), ft.TextButton("YA, LANJUTKAN", on_click=on_ya)]
        )
        _show_dialog(dialog)
        await event.wait()
        return hasil["confirmed"]

    # ---------- Inti logika validasi + eksekusi (dipakai manual & 4 tab) ----------
    async def evaluasi_dan_eksekusi(setup, sym, margin, lev, max_loss_value, is_auto, log_fn):
        """log_fn(msg, is_error=False) - dipanggil untuk setiap langkah.
        Manual (is_auto=False) juga menampilkan dialog konfirmasi & popup error.
        Auto (is_auto=True) langsung eksekusi tanpa dialog apapun."""

        def _log(msg, is_error=False):
            log_fn(f"{sym}: {msg}", is_error)

        binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
        loop = asyncio.get_running_loop()
        arah = setup.get('arah')

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
                f"untuk {sym}. Tidak membuka posisi baru di atasnya."
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
                tampilkan_peringatan("Ukuran Posisi Terlalu Kecil", f"Quantity ({size_dec}) di bawah minimum ({min_qty}).")
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
        max_loss = abs(max_loss_value)

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
            f"PnL 24 Jam: {total_pnl_24h:.2f} USDT\n\nOrder akan LANGSUNG dieksekusi. Lanjutkan?"
        )

        if is_auto:
            _log(f"MODE OTOMATIS - eksekusi TANPA konfirmasi. Setup: arah={arah} entry={entry_dec} tp={tp_dec} sl={sl_dec} qty={size_dec}")
        else:
            _log("Menunggu konfirmasi Anda...")
            setuju = await minta_konfirmasi(ringkasan)
            if not setuju:
                _log("Dibatalkan oleh pengguna.")
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
                rollback_msg = f"GAGAL membatalkan otomatis! CEK MANUAL DI BINANCE. Error: {cancel_err}"
            _log(f"ORDER DITOLAK/GAGAL - {order_err}. {rollback_msg}", is_error=True)
            if not is_auto:
                tampilkan_peringatan("Order Ditolak/Gagal - Rollback Dijalankan", f"{order_err}\n\n{rollback_msg}")
            return "error"

        _log("TRIPLE SHOT BERHASIL (Entry/TP/SL semua aktif).")
        return "success"

    # ================= BAGIAN 1: MODE FOTO (MANUAL) =================
    saved_api_ai = await sp.get("api_ai") or ""
    saved_api_bin = await sp.get("api_bin") or ""
    saved_api_sec = await sp.get("api_sec") or ""
    saved_margin_m = await sp.get("m_margin") or "10"
    saved_lev_m = await sp.get("m_leverage") or "20"
    saved_maxloss_m = await sp.get("m_maxloss") or "50"

    async def save_shared(e):
        await sp.set("api_ai", api_ai.value or "")
        await sp.set("api_bin", api_bin.value or "")
        await sp.set("api_sec", api_sec.value or "")

    async def save_manual(e):
        await sp.set("m_margin", input_margin_m.value or "")
        await sp.set("m_leverage", input_lev_m.value or "")
        await sp.set("m_maxloss", input_maxloss_m.value or "")

    api_ai = ft.TextField(label="Claude API Key", password=True, can_reveal_password=True, value=saved_api_ai, on_blur=save_shared)
    api_bin = ft.TextField(label="Binance API Key", password=True, can_reveal_password=True, value=saved_api_bin, on_blur=save_shared)
    api_sec = ft.TextField(label="Binance Secret", password=True, can_reveal_password=True, value=saved_api_sec, on_blur=save_shared)

    input_symbol_m = ft.TextField(label="Simbol (opsional - kosongkan agar dibaca otomatis dari screenshot)", value="")
    input_margin_m = ft.TextField(label="Margin (USDT)", value=saved_margin_m, on_blur=save_manual)
    input_lev_m = ft.TextField(label="Leverage", value=saved_lev_m, on_blur=save_manual)
    input_maxloss_m = ft.TextField(label="Batas Rugi 24 Jam (USDT)", value=saved_maxloss_m, on_blur=save_manual)

    path_foto = ft.Text("Belum ada foto dipilih")
    layar_log = ft.Text("Status: Standby", color=ft.Colors.YELLOW)
    launch_button = ft.Button("LUNCURKAN OTOMATIS (FOTO)", bgcolor=ft.Colors.RED_800, color=ft.Colors.WHITE)
    cleanup_button_m = ft.Button("BERSIHKAN ORDER SISA (SIMBOL DI ATAS)", bgcolor=ft.Colors.ORANGE_800, color=ft.Colors.WHITE)

    def manual_log_fn(msg, is_error=False):
        layar_log.value = f"Status: {msg}"
        layar_log.color = ft.Colors.RED if is_error else ft.Colors.BLUE
        page.update()

    async def pick_files_click(e):
        files = await file_picker.pick_files()
        if files and len(files) > 0:
            path_foto.value = files[0].path
            path_foto.update()

    async def luncurkan_execution():
        launch_button.disabled = True
        launch_button.text = "MEMPROSES..."
        page.update()
        try:
            if not path_foto.value or "Belum ada" in path_foto.value:
                tampilkan_peringatan("Foto Belum Dipilih", "Silakan upload foto chart terlebih dahulu.")
                return
            manual_log_fn("Menghubungi Claude AI...")

            prompt = """
            Anda adalah sistem penembak jitu trading crypto. Analisis chart/gambar ini dengan sangat teliti.

            Langkah 1: Baca nama pair trading yang tertera di judul/header chart. Tuliskan hasilnya sebagai
            kode simbol futures USDT-M tanpa spasi/garis miring, contoh: BTCUSDT, ETHUSDT, SOLUSDT.
            Jika judul pair tidak bisa dibaca di gambar, kosongkan field "simbol".

            Langkah 2: Tentukan setup Stop Market (Entri, Take Profit, Stop Loss) berdasarkan analisis
            teknikal chart tersebut.

            Keluarkan HANYA format JSON murni tanpa kata-kata pembuka/penutup:
            {
                "sinyal": "VALID",
                "simbol": "BTCUSDT",
                "arah": "BUY",
                "pemicu_masuk": 0.00000,
                "take_profit": 0.00000,
                "stop_loss": 0.00000
            }
            Jika tidak ada momentum atau chart kurang jelas, ubah "sinyal" menjadi "TIDAK VALID".
            """
            loop = asyncio.get_running_loop()
            raw_response = await loop.run_in_executor(None, lambda: call_claude_vision_api(api_ai.value, path_foto.value, prompt))

            try:
                setup = parse_json_setup(raw_response)
            except Exception as pe:
                tampilkan_peringatan("Gagal Parse Respons AI", str(pe))
                return

            if setup.get('sinyal') != "VALID":
                tampilkan_peringatan("Sinyal Tidak Valid", "AI menilai chart kurang jelas / tidak ada momentum aman.")
                return

            manual_sym = input_symbol_m.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
            ai_sym = str(setup.get('simbol', '') or '').strip().upper().replace('/', '').replace('-', '').replace(' ', '').replace('PERP', '')

            if manual_sym:
                sym = manual_sym
            elif ai_sym:
                if not ai_sym.endswith('USDT'):
                    ai_sym += 'USDT'
                sym = ai_sym
            else:
                tampilkan_peringatan("Simbol Tidak Terdeteksi", "AI gagal baca simbol dan kolom Simbol kosong.")
                return

            try:
                margin_v = float(input_margin_m.value)
                lev_v = int(input_lev_m.value)
                maxloss_v = float(input_maxloss_m.value or "0")
            except ValueError:
                tampilkan_peringatan("Input Tidak Valid", "Margin/Leverage/Batas Rugi harus berupa angka.")
                return

            await evaluasi_dan_eksekusi(setup, sym, margin_v, lev_v, maxloss_v, is_auto=False, log_fn=manual_log_fn)

        except requests.exceptions.HTTPError as err:
            tampilkan_peringatan("Gagal REST API", str(err))
        except Exception as ex:
            tampilkan_peringatan("Terjadi Kesalahan", str(ex))
        finally:
            launch_button.disabled = False
            launch_button.text = "LUNCURKAN OTOMATIS (FOTO)"
            page.update()

    launch_button.on_click = lambda e: asyncio.create_task(luncurkan_execution())

    async def cleanup_manual_click():
        cleanup_button_m.disabled = True
        page.update()
        try:
            sym = input_symbol_m.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
            if not sym:
                tampilkan_peringatan("Simbol Kosong", "Isi kolom Simbol dulu untuk cek order sisa.")
                return
            if not sym.endswith("USDT"):
                sym += "USDT"
            binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
            loop = asyncio.get_running_loop()
            jumlah, pesan = await loop.run_in_executor(None, lambda: binance.cleanup_orphan_orders(sym, log_fn=lambda m: manual_log_fn(m)))
            tampilkan_peringatan("Hasil Pengecekan" if jumlah == 0 else "Order Sisa Dibersihkan", f"{sym}: {pesan}")
        except Exception as ex:
            tampilkan_peringatan("Gagal Cek Order Sisa", str(ex))
        finally:
            cleanup_button_m.disabled = False
            page.update()

    cleanup_button_m.on_click = lambda e: asyncio.create_task(cleanup_manual_click())

    # ================= BAGIAN 2: 4 TAB MODE OTOMATIS INDEPENDEN =================
    def build_coin_panel(idx):
        prefix = f"t{idx}_"
        label = f"Koin {idx}"

        async def save_tab(e):
            await sp.set(prefix + "symbol", in_symbol.value or "")
            await sp.set(prefix + "margin", in_margin.value or "")
            await sp.set(prefix + "leverage", in_lev.value or "")
            await sp.set(prefix + "maxloss", in_maxloss.value or "")
            await sp.set(prefix + "candle", in_candle.value or "")
            await sp.set(prefix + "interval", in_interval.value or "")

        in_symbol = ft.TextField(label=f"Simbol {label}", value="")
        in_margin = ft.TextField(label="Margin (USDT)", value="10", on_blur=save_tab)
        in_lev = ft.TextField(label="Leverage", value="20", on_blur=save_tab)
        in_maxloss = ft.TextField(label="Batas Rugi 24 Jam (USDT)", value="50", on_blur=save_tab)
        in_candle = ft.TextField(label="Interval Candle (mis. 15m, 1h)", value="15m", on_blur=save_tab)
        in_interval = ft.TextField(label="Jalankan Tiap (menit)", value="30", on_blur=save_tab)
        in_symbol.on_blur = save_tab

        switch_t = ft.Switch(label=f"MODE OTOMATIS {label} — TANPA konfirmasi", value=False)
        status_t = ft.Text(f"{label}: NONAKTIF", color=ft.Colors.GREY)
        log_list_t = ft.ListView(height=180, spacing=2, auto_scroll=True)
        cleanup_button_t = ft.Button(f"BERSIHKAN ORDER SISA {label}", bgcolor=ft.Colors.ORANGE_800, color=ft.Colors.WHITE)
        task_holder = {"task": None}

        def catat_log_t(msg, is_error=False):
            ts = time.strftime("%H:%M:%S")
            log_list_t.controls.append(ft.Text(f"[{ts}] {msg}", size=12, color=ft.Colors.RED_200 if is_error else None))
            if len(log_list_t.controls) > 500:
                log_list_t.controls.pop(0)
            log_list_t.update()

        async def siklus_t():
            sym = in_symbol.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
            if not sym:
                catat_log_t(f"{label}: Simbol kosong - isi dulu kolom Simbol.")
                return "error"
            if not sym.endswith("USDT"):
                sym += "USDT"

            interval_candle = (in_candle.value or "15m").strip()
            binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
            loop = asyncio.get_running_loop()

            try:
                jumlah, pesan = await loop.run_in_executor(None, lambda: binance.cleanup_orphan_orders(sym, log_fn=catat_log_t))
                if jumlah > 0:
                    catat_log_t(f"{sym}: [Housekeeping] {pesan}")
            except Exception as e:
                catat_log_t(f"{sym}: Gagal cek order sisa (lanjut tetap) - {e}")

            try:
                klines = await loop.run_in_executor(None, lambda: binance.get_klines(sym, interval_candle, 100))
            except Exception as e:
                catat_log_t(f"{sym}: Gagal ambil data candlestick - {e}")
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
                catat_log_t(f"{sym}: Gagal hubungi Claude - {e}")
                return "error"

            try:
                setup = parse_json_setup(raw_response)
            except Exception as pe:
                catat_log_t(f"{sym}: {pe}")
                return "error"

            if setup.get('sinyal') != "VALID":
                catat_log_t(f"{sym}: Tidak ada sinyal valid pada siklus ini.")
                return "no_signal"

            catat_log_t(f"{sym}: Sinyal {setup.get('arah')} terdeteksi (entry={setup.get('pemicu_masuk')}, TP={setup.get('take_profit')}, SL={setup.get('stop_loss')}). Memvalidasi & mengeksekusi...")

            try:
                margin_v = float(in_margin.value)
                lev_v = int(in_lev.value)
                maxloss_v = float(in_maxloss.value or "0")
            except ValueError:
                catat_log_t(f"{sym}: Margin/Leverage/Batas Rugi {label} tidak valid.")
                return "error"

            return await evaluasi_dan_eksekusi(setup, sym, margin_v, lev_v, maxloss_v, is_auto=True, log_fn=catat_log_t)

        async def loop_t():
            catat_log_t(f"=== {label} MODE OTOMATIS AKTIF ===")
            while switch_t.value:
                try:
                    status = await siklus_t()
                except Exception as e:
                    catat_log_t(f"ERROR TAK TERDUGA: {e}")
                    status = "error"

                if status in ("error", "batas_rugi"):
                    catat_log_t(f"{label} DIHENTIKAN otomatis (circuit breaker). Aktifkan switch lagi kalau ingin lanjut.")
                    switch_t.value = False
                    status_t.value = f"{label}: BERHENTI OTOMATIS (lihat log)"
                    status_t.color = ft.Colors.RED
                    switch_t.update()
                    status_t.update()
                    break

                try:
                    interval_menit = max(1.0, float(in_interval.value or "15"))
                except ValueError:
                    interval_menit = 15.0
                status_t.value = f"{label}: AKTIF (siklus berikutnya ~{interval_menit:.0f} menit lagi)"
                status_t.color = ft.Colors.GREEN
                status_t.update()
                await asyncio.sleep(interval_menit * 60)

            task_holder["task"] = None

        def on_switch_change(e):
            if switch_t.value:
                if task_holder["task"] is None:
                    status_t.value = f"{label}: AKTIF (memulai...)"
                    status_t.color = ft.Colors.GREEN
                    status_t.update()
                    task_holder["task"] = asyncio.create_task(loop_t())
            else:
                catat_log_t(f"{label}: Mode otomatis dimatikan manual.")
                status_t.value = f"{label}: NONAKTIF"
                status_t.color = ft.Colors.GREY
                status_t.update()
                t = task_holder["task"]
                if t is not None:
                    t.cancel()
                    task_holder["task"] = None

        switch_t.on_change = on_switch_change

        async def cleanup_click_t():
            cleanup_button_t.disabled = True
            page.update()
            try:
                sym = in_symbol.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
                if not sym:
                    tampilkan_peringatan("Simbol Kosong", f"Isi dulu kolom Simbol {label}.")
                    return
                if not sym.endswith("USDT"):
                    sym += "USDT"
                binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
                loop = asyncio.get_running_loop()
                jumlah, pesan = await loop.run_in_executor(None, lambda: binance.cleanup_orphan_orders(sym, log_fn=catat_log_t))
                catat_log_t(f"{sym}: [Cek Manual] {pesan}")
                tampilkan_peringatan("Hasil Pengecekan" if jumlah == 0 else "Order Sisa Dibersihkan", f"{sym}: {pesan}")
            except Exception as ex:
                tampilkan_peringatan("Gagal Cek Order Sisa", str(ex))
            finally:
                cleanup_button_t.disabled = False
                page.update()

        cleanup_button_t.on_click = lambda e: asyncio.create_task(cleanup_click_t())

        async def load_saved():
            in_symbol.value = await sp.get(prefix + "symbol") or ""
            in_margin.value = await sp.get(prefix + "margin") or "10"
            in_lev.value = await sp.get(prefix + "leverage") or "20"
            in_maxloss.value = await sp.get(prefix + "maxloss") or "50"
            in_candle.value = await sp.get(prefix + "candle") or "15m"
            in_interval.value = await sp.get(prefix + "interval") or "30"

        panel = ft.Column([
            in_symbol, in_margin, in_lev, in_maxloss, in_candle, in_interval,
            cleanup_button_t,
            switch_t, status_t,
            ft.Text("Log Aktivitas:", size=12, weight="bold"),
            ft.Container(content=log_list_t, bgcolor=ft.Colors.BLACK26, padding=5, border_radius=5),
        ], spacing=10)
        return panel, load_saved

    panel1, load1 = build_coin_panel(1)
    panel2, load2 = build_coin_panel(2)
    panel3, load3 = build_coin_panel(3)
    panel4, load4 = build_coin_panel(4)
    await load1()
    await load2()
    await load3()
    await load4()

    tabs_otomatis = ft.Tabs(
        selected_index=0,
        tabs=[
            ft.Tab(text="Koin 1", content=panel1),
            ft.Tab(text="Koin 2", content=panel2),
            ft.Tab(text="Koin 3", content=panel3),
            ft.Tab(text="Koin 4", content=panel4),
        ],
    )

    page.add(
        ft.Column([
            ft.Text("PUSAT KOMANDO SNIPER", size=20, weight="bold"),
            api_ai, api_bin, api_sec,
            ft.Divider(),
            ft.Text("Mode Foto (manual, dengan konfirmasi)", size=14, weight="bold"),
            input_symbol_m, input_margin_m, input_lev_m, input_maxloss_m,
            ft.Button("UPLOAD FOTO", on_click=pick_files_click),
            path_foto,
            launch_button,
            layar_log,
            cleanup_button_m,
            ft.Divider(),
            ft.Text("Mode Otomatis Berkala — 4 Koin Independen (TANPA konfirmasi)", size=14, weight="bold", color=ft.Colors.RED),
            ft.Text("Tiap tab punya simbol, margin, leverage, interval, dan circuit breaker sendiri-sendiri.", size=11, color=ft.Colors.GREY),
            tabs_otomatis,
        ])
    )

if __name__ == "__main__":
    ft.run(main)