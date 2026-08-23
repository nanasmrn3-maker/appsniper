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
    Ini yang membuat bot bisa dipakai di SEMUA simbol, bukan cuma BTC/ETH."""
    step = Decimal(step_str)
    if step == 0:
        return value
    quotient = (value / step).to_integral_value(rounding=rounding)
    result = quotient * step
    decimals = max(0, -step.as_tuple().exponent)
    quant = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    return result.quantize(quant, rounding=rounding)


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

        res.raise_for_status()
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
        return self._request("POST", "/fapi/v1/algoOrder", params)

    def get_income_history(self, start_time_ms, income_type="REALIZED_PNL"):
        params = {
            "incomeType": income_type,
            "startTime": start_time_ms,
            "limit": 1000
        }
        return self._request("GET", "/fapi/v1/income", params)


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
    input_symbol = ft.TextField(label="Simbol (Opsional - kosongkan agar dibaca otomatis dari screenshot)", value="")
    input_max_loss = ft.TextField(label="Batas Rugi 24 Jam (USDT)", value=saved_max_loss, on_blur=save_data)

    path_foto = ft.Text("Belum ada foto dipilih")
    layar_log = ft.Text("Status: Standby", color=ft.Colors.YELLOW)

    launch_button = ft.Button("LUNCURKAN OTOMATIS", bgcolor=ft.Colors.RED_800, color=ft.Colors.WHITE)

    async def pick_files_click(e):
        files = await file_picker.pick_files()
        if files and len(files) > 0:
            path_foto.value = files[0].path
            path_foto.update()

    def _show_dialog(dialog):
        # Kompatibel dengan versi Flet lama (page.dialog + dialog.open) maupun
        # versi baru (page.open). Ini yang bikin popup akhirnya benar-benar tampil
        # berapapun versi Flet yang ke-install di CI.
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
        """Fitur #1: Dialog konfirmasi wajib sebelum order nyata dikirim ke Binance."""
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

    def call_claude_api(api_key, image_path, prompt):
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
            "max_tokens": 1000,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded_image
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }]
        }

        url = "https://api.anthropic.com/v1/messages"
        res = requests.post(url, headers=headers, json=payload, timeout=90, verify=False)
        res.raise_for_status()
        data = res.json()

        content_blocks = data.get("content", [])
        if not content_blocks:
            raise Exception(f"Respons Claude tidak berisi content. Raw: {json.dumps(data)[:500]}")

        for block in content_blocks:
            if block.get("type") == "text" and "text" in block:
                return block["text"]

        raise Exception(f"Tidak ada blok teks pada respons Claude. Raw: {json.dumps(data)[:500]}")

    async def luncurkan_execution():
        # Fitur #4: kunci tombol selama proses berjalan, cegah eksekusi ganda akibat tap dobel.
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
                None, lambda: call_claude_api(api_ai.value, path_foto.value, prompt)
            )

            raw_text = raw_response.strip().replace('```json', '').replace('```', '')
            start = raw_text.find('{')
            end = raw_text.rfind('}') + 1

            if start == -1 or end <= start:
                tampilkan_peringatan(
                    "AI Tidak Mengembalikan JSON",
                    f"Respons Claude tidak mengandung format JSON yang diharapkan. "
                    f"Kemungkinan chart kurang jelas atau simbol kurang dikenali AI.\n\n"
                    f"Teks asli dari AI:\n{raw_text[:800]}"
                )
                return

            try:
                setup = json.loads(raw_text[start:end])
            except json.JSONDecodeError as jde:
                tampilkan_peringatan(
                    "Gagal Parse JSON dari AI",
                    f"Error: {jde}\n\nTeks asli dari AI:\n{raw_text[:800]}"
                )
                return

            if setup.get('sinyal') != "VALID":
                tampilkan_peringatan("Sinyal Tidak Valid", "AI menilai chart saat ini kurang jelas atau tidak ada momentum yang aman untuk masuk pasar.")
                return

            binance = BinanceFuturesAPI(api_bin.value, api_sec.value)

            manual_sym = input_symbol.value.strip().replace('/', '').replace('-', '').replace(' ', '').upper()
            ai_sym_raw = str(setup.get('simbol', '') or '').strip().upper()
            ai_sym = ai_sym_raw.replace('/', '').replace('-', '').replace(' ', '').replace('PERP', '')

            if manual_sym:
                sym = manual_sym  # kolom manual diisi -> ini menang (override)
            elif ai_sym:
                if not ai_sym.endswith('USDT'):
                    ai_sym += 'USDT'
                sym = ai_sym  # kolom manual kosong -> pakai hasil baca AI dari screenshot
            else:
                tampilkan_peringatan(
                    "Simbol Tidak Terdeteksi",
                    "AI tidak berhasil membaca nama pair dari screenshot, dan kolom Simbol dikosongkan. "
                    "Isi manual kolom Simbol, atau upload screenshot yang judul pair-nya terlihat jelas."
                )
                return

            margin = float(input_margin.value)
            lev = int(input_lev.value)
            arah = setup['arah']

            layar_log.value = "Status: Mengambil data pasar & aturan simbol..."
            page.update()

            # Ambil harga pasar & aturan presisi (stepSize/tickSize) simbol ini secara dinamis.
            # Ini yang membuat bot bekerja untuk SEMUA koin, tidak cuma BTC/ETH.
            current_price = await loop.run_in_executor(None, lambda: binance.get_ticker_price(sym))
            filters = await loop.run_in_executor(None, lambda: binance.get_symbol_filters(sym))

            try:
                entry = Decimal(str(setup['pemicu_masuk']))
                tp = Decimal(str(setup['take_profit']))
                sl = Decimal(str(setup['stop_loss']))
            except (InvalidOperation, KeyError, TypeError):
                tampilkan_peringatan("Data AI Tidak Valid", "Respons AI tidak berisi angka harga yang bisa dibaca.")
                return

            # Fitur #2: validasi kewajaran harga sebelum eksekusi.
            current_price_dec = Decimal(str(current_price))
            masalah = []

            if arah == 'BUY':
                if not (sl < entry < tp):
                    masalah.append(f"Urutan harga tidak masuk akal untuk BUY (harus SL < Entry < TP). Diterima: SL={sl}, Entry={entry}, TP={tp}")
            elif arah == 'SELL':
                if not (tp < entry < sl):
                    masalah.append(f"Urutan harga tidak masuk akal untuk SELL (harus TP < Entry < SL). Diterima: TP={tp}, Entry={entry}, SL={sl}")
            else:
                masalah.append(f"Arah tidak dikenali dari AI: '{arah}'")

            deviasi = abs(entry - current_price_dec) / current_price_dec if current_price_dec != 0 else Decimal(1)
            if deviasi > Decimal("0.10"):
                masalah.append(f"Harga entry ({entry}) menyimpang {float(deviasi)*100:.1f}% dari harga pasar saat ini ({current_price}). Kemungkinan AI salah membaca chart.")

            if masalah:
                tampilkan_peringatan("Validasi Harga Gagal", "\n".join(masalah))
                return

            # Bulatkan quantity & harga sesuai stepSize/tickSize resmi Binance untuk simbol ini.
            raw_size = Decimal(str((margin * lev))) / current_price_dec
            size_dec = round_step(raw_size, filters['qty_step'], ROUND_DOWN)
            min_qty = Decimal(filters['min_qty'])

            if size_dec < min_qty or size_dec <= 0:
                tampilkan_peringatan(
                    "Ukuran Posisi Terlalu Kecil",
                    f"Quantity hasil hitung ({size_dec}) di bawah minimum simbol ini ({min_qty}). Naikkan margin atau leverage."
                )
                return

            notional = size_dec * current_price_dec
            min_notional = Decimal(filters['min_notional'])
            if notional < min_notional:
                tampilkan_peringatan(
                    "Notional Terlalu Kecil",
                    f"Nilai order ({notional:.2f} USDT) di bawah minimum Binance untuk simbol ini ({min_notional} USDT). Naikkan margin atau leverage."
                )
                return

            entry_dec = round_step(entry, filters['price_tick'], ROUND_HALF_UP)
            tp_dec = round_step(tp, filters['price_tick'], ROUND_HALF_UP)
            sl_dec = round_step(sl, filters['price_tick'], ROUND_HALF_UP)

            # Fitur #3: batas rugi 24 jam terakhir (rolling), dicek ke riwayat income asli dari Binance.
            layar_log.value = "Status: Mengecek batas rugi 24 jam..."
            page.update()

            start_24h_ms = int(time.time() * 1000) - 24 * 3600 * 1000
            income_list = await loop.run_in_executor(None, lambda: binance.get_income_history(start_24h_ms))
            total_pnl_24h = sum(float(item.get('income', 0)) for item in income_list)
            max_loss = abs(float(input_max_loss.value or "0"))

            if max_loss > 0 and total_pnl_24h <= -max_loss:
                tampilkan_peringatan(
                    "Batas Rugi Tercapai",
                    f"Total PnL 24 jam terakhir: {total_pnl_24h:.2f} USDT (batas: -{max_loss:.2f} USDT). "
                    f"Eksekusi dihentikan untuk mencegah kerugian lebih lanjut. Ubah 'Batas Rugi 24 Jam' kalau ingin lanjut."
                )
                return

            # Fitur #1: konfirmasi eksplisit sebelum order nyata dikirim.
            sumber_simbol = "diisi manual" if manual_sym else "dibaca otomatis dari screenshot oleh AI"
            ringkasan = (
                f"Simbol: {sym} ({sumber_simbol})\n"
                f"Arah: {arah}\n"
                f"Harga Pasar Saat Ini: {current_price}\n"
                f"Entry (Stop): {entry_dec}\n"
                f"Take Profit: {tp_dec}\n"
                f"Stop Loss: {sl_dec}\n"
                f"Quantity: {size_dec}\n"
                f"Estimasi Notional: {notional:.2f} USDT\n"
                f"Margin: {margin} USDT | Leverage: {lev}x\n"
                f"PnL 24 Jam Terakhir: {total_pnl_24h:.2f} USDT\n\n"
                f"Order ini akan LANGSUNG dieksekusi dengan uang sungguhan. Lanjutkan?"
            )
            layar_log.value = "Status: Menunggu konfirmasi Anda..."
            layar_log.color = ft.Colors.YELLOW
            page.update()

            setuju = await minta_konfirmasi(ringkasan)
            if not setuju:
                layar_log.value = "Status: Dibatalkan oleh pengguna."
                layar_log.color = ft.Colors.YELLOW
                page.update()
                return

            layar_log.value = "Status: Sinyal VALID! Menembak Binance..."
            layar_log.color = ft.Colors.BLUE
            page.update()

            await loop.run_in_executor(None, lambda: binance.set_leverage(sym, lev))

            side_u, side_p = ('BUY', 'SELL') if arah == 'BUY' else ('SELL', 'BUY')
            qty_str = str(size_dec)

            # 1. Order Utama (STOP_MARKET) - via Algo Order API
            await loop.run_in_executor(None, lambda: binance.create_algo_order(
                sym, side_u, 'STOP_MARKET', qty_str, trigger_price_str=str(entry_dec)
            ))
            # 2. Order Take Profit (TAKE_PROFIT_MARKET) - via Algo Order API
            await loop.run_in_executor(None, lambda: binance.create_algo_order(
                sym, side_p, 'TAKE_PROFIT_MARKET', qty_str, trigger_price_str=str(tp_dec), reduce_only=True
            ))
            # 3. Order Stop Loss (STOP_MARKET) - via Algo Order API
            await loop.run_in_executor(None, lambda: binance.create_algo_order(
                sym, side_p, 'STOP_MARKET', qty_str, trigger_price_str=str(sl_dec), reduce_only=True
            ))

            layar_log.value = "Status: TRIPLE SHOT BERHASIL!"
            layar_log.color = ft.Colors.GREEN
            page.update()

        except requests.exceptions.HTTPError as err:
            err_msg = err.response.text if hasattr(err, 'response') and err.response is not None else str(err)
            tampilkan_peringatan("Gagal REST API", f"{err_msg}")
        except Exception as ex:
            tampilkan_peringatan("Terjadi Kesalahan", f"{str(ex)}")
        finally:
            # Fitur #4: buka kunci tombol lagi apapun hasilnya.
            launch_button.disabled = False
            launch_button.text = "LUNCURKAN OTOMATIS"
            page.update()

    def on_luncurkan_click(e):
        asyncio.create_task(luncurkan_execution())

    launch_button.on_click = on_luncurkan_click

    page.add(
        ft.Column([
            ft.Text("PUSAT KOMANDO SNIPER", size=20, weight="bold"),
            api_ai, api_bin, api_sec, input_margin, input_lev, input_symbol, input_max_loss,
            ft.Divider(),
            ft.Button("UPLOAD FOTO", on_click=pick_files_click),
            path_foto,
            launch_button,
            layar_log
        ])
    )

if __name__ == "__main__":
    ft.run(main)