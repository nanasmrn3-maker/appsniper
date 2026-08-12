import flet as ft
import json
import os
import asyncio
import base64
import requests
import hmac
import hashlib
import time
import urllib3
from urllib.parse import urlencode

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- HELPER FUNGSIONAL BINANCE REST API ---
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
        full_query = f"{query_string}&signature={signature}"
        
        url = f"{self.base_url}{endpoint}"
        
        if method.upper() == "GET":
            res = requests.get(f"{url}?{full_query}", headers=self._headers(), timeout=15, verify=False)
        else:
            res = requests.post(url, headers=self._headers(), data=full_query, timeout=15, verify=False)
            
        res.raise_for_status()
        return res.json()

    def set_leverage(self, symbol, leverage):
        params = {
            "symbol": symbol.replace('/', '').upper(),
            "leverage": int(leverage)
        }
        return self._request("POST", "/fapi/v1/leverage", params)

    def get_ticker_price(self, symbol):
        url = f"{self.base_url}/fapi/v1/ticker/price"
        params = {"symbol": symbol.replace('/', '').upper()}
        res = requests.get(url, params=params, timeout=15, verify=False)
        res.raise_for_status()
        return float(res.json()["price"])

    def create_order(self, symbol, side, order_type, quantity, price, stop_price=None, reduce_only=False):
        params = {
            "symbol": symbol.replace('/', '').upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": f"{quantity:.3f}",
        }
        
        if order_type.upper() in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
            params["stopPrice"] = f"{stop_price}"
            
        if reduce_only:
            params["reduceOnly"] = "true"
            
        return self._request("POST", "/fapi/v1/order", params)


# --- APP MAIN FLET ---
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

    async def save_data(e):
        await sp.set("api_ai", api_ai.value or "")
        await sp.set("api_bin", api_bin.value or "")
        await sp.set("api_sec", api_sec.value or "")
        await sp.set("margin", input_margin.value or "")
        await sp.set("leverage", input_lev.value or "")

    api_ai = ft.TextField(label="Gemini API Key", password=True, can_reveal_password=True, value=saved_api_ai, on_blur=save_data)
    api_bin = ft.TextField(label="Binance API Key", password=True, can_reveal_password=True, value=saved_api_bin, on_blur=save_data)
    api_sec = ft.TextField(label="Binance Secret", password=True, can_reveal_password=True, value=saved_api_sec, on_blur=save_data)
    input_margin = ft.TextField(label="Margin (USDT)", value=saved_margin, on_blur=save_data)
    input_lev = ft.TextField(label="Leverage", value=saved_leverage, on_blur=save_data)
    input_symbol = ft.TextField(label="Simbol (Contoh: BTCUSDT atau CAP/USDT)", value="BTCUSDT")
    
    path_foto = ft.Text("Belum ada foto dipilih")
    layar_log = ft.Text("Status: Standby", color=ft.Colors.YELLOW)

    async def pick_files_click(e):
        files = await file_picker.pick_files()
        if files and len(files) > 0:
            path_foto.value = files[0].path
            path_foto.update()

    def tampilkan_peringatan(judul, pesan):
        layar_log.value = f"Status: {judul}\n\nDetail: {pesan}"
        layar_log.color = ft.Colors.RED
        
        dialog = ft.AlertDialog(
            title=ft.Text(judul),
            content=ft.Text(pesan),
            actions=[ft.TextButton("OK", on_click=lambda _: page.close(dialog))]
        )
        page.open(dialog)
        page.update()

    def call_gemini_rest_api(api_key, image_path, prompt):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key.strip()}"
        
        # BACA BYTES GAMBAR ASLI TANPA POTONGAN AGAR HEADER FILE VALID
        with open(image_path, "rb") as image_file:
            raw_bytes = image_file.read()
            encoded_image = base64.b64encode(raw_bytes).decode("utf-8")

        ext = os.path.splitext(image_path)[1].lower()
        mime_type = "image/png" if ext == ".png" else "image/jpeg"

        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": encoded_image
                        }
                    }
                ]
            }]
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        res = requests.post(url, headers=headers, json=payload, timeout=60, verify=False)
        res.raise_for_status()
        data = res.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def luncurkan_execution():
        if not path_foto.value or "Belum ada" in path_foto.value:
            tampilkan_peringatan("Foto Belum Dipilih", "Silakan upload foto chart terlebih dahulu sebelum meluncurkan bot.")
            return

        layar_log.value = "Status: Menghubungi Gemini AI..."
        layar_log.color = ft.Colors.BLUE
        page.update()

        try:
            prompt = """
            Anda adalah sistem penembak jitu trading crypto. Analisis chart/gambar ini dengan sangat teliti.
            Perhatikan struktur harga terbaru yang tertera di chart.
            Tentukan setup Stop Market (Entri, Take Profit, dan Stop Loss) berdasarkan analisis teknikal chart tersebut.
            
            Keluarkan HANYA format JSON murni tanpa kata-kata pembuka/penutup. Format wajib:
            {
                "sinyal": "VALID",
                "arah": "BUY",
                "pemicu_masuk": 0.00000,
                "take_profit": 0.00000,
                "stop_loss": 0.00000
            }
            Jika tidak ada momentum atau chart kurang jelas untuk dianalisis, ubah status "sinyal" menjadi "TIDAK VALID".
            """
            
            loop = asyncio.get_running_loop()
            raw_response = await loop.run_in_executor(
                None, lambda: call_gemini_rest_api(api_ai.value, path_foto.value, prompt)
            )
            
            raw_text = raw_response.strip().replace('```json', '').replace('```', '')
            start = raw_text.find('{')
            end = raw_text.rfind('}') + 1
            setup = json.loads(raw_text[start:end])

            if setup.get('sinyal') == "VALID":
                layar_log.value = "Status: Sinyal VALID! Menembak Binance..."
                page.update()

                binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
                margin = float(input_margin.value)
                lev = int(input_lev.value)
                sym = input_symbol.value.strip().replace('/', '').upper()

                await loop.run_in_executor(None, lambda: binance.set_leverage(sym, lev))
                price = await loop.run_in_executor(None, lambda: binance.get_ticker_price(sym))
                size = (margin * lev) / price
                
                side_u, side_p = ('BUY', 'SELL') if setup['arah'] == 'BUY' else ('SELL', 'BUY')
                
                # 1. Order Utama (STOP_MARKET)
                await loop.run_in_executor(None, lambda: binance.create_order(
                    sym, side_u, 'STOP_MARKET', size, setup['pemicu_masuk'], stop_price=setup['pemicu_masuk']
                ))
                # 2. Order Take Profit (TAKE_PROFIT_MARKET)
                await loop.run_in_executor(None, lambda: binance.create_order(
                    sym, side_p, 'TAKE_PROFIT_MARKET', size, setup['take_profit'], stop_price=setup['take_profit'], reduce_only=True
                ))
                # 3. Order Stop Loss (STOP_MARKET)
                await loop.run_in_executor(None, lambda: binance.create_order(
                    sym, side_p, 'STOP_MARKET', size, setup['stop_loss'], stop_price=setup['stop_loss'], reduce_only=True
                ))
                
                layar_log.value = "Status: TRIPLE SHOT BERHASIL!"
                layar_log.color = ft.Colors.GREEN
                page.update()
            else:
                tampilkan_peringatan("Sinyal Tidak Valid", "AI menilai chart saat ini kurang jelas atau tidak ada momentum yang aman untuk masuk pasar.")
                
        except requests.exceptions.HTTPError as err:
            err_msg = err.response.text if hasattr(err, 'response') and err.response is not None else str(err)
            tampilkan_peringatan("Gagal REST API", f"{err_msg}")
        except Exception as ex:
            tampilkan_peringatan("Terjadi Kesalahan", f"{str(ex)}")

    def on_luncurkan_click(e):
        asyncio.create_task(luncurkan_execution())

    page.add(
        ft.Column([
            ft.Text("PUSAT KOMANDO SNIPER", size=20, weight="bold"),
            api_ai, api_bin, api_sec, input_margin, input_lev, input_symbol,
            ft.Divider(),
            ft.Button("UPLOAD FOTO", on_click=pick_files_click),
            path_foto,
            ft.Button("LUNCURKAN OTOMATIS", bgcolor=ft.Colors.RED_800, color=ft.Colors.WHITE, on_click=on_luncurkan_click),
            layar_log
        ])
    )

if __name__ == "__main__":
    ft.run(main)