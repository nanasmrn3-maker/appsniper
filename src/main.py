import flet as ft
import json
import os
import asyncio
import requests
import hmac
import hashlib
import time
import urllib3
from PIL import Image
import google.generativeai as genai

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- HELPER FUNGSIONAL BINANCE REST API ---
class BinanceFuturesAPI:
    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = "https://fapi.binance.com"

    def _generate_signature(self, params):
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def _headers(self):
        return {"X-MBX-APIKEY": self.api_key}

    def set_leverage(self, symbol, leverage):
        url = f"{self.base_url}/fapi/v1/leverage"
        params = {
            "symbol": symbol.replace('/', '').upper(),
            "leverage": int(leverage),
            "timestamp": int(time.time() * 1000)
        }
        params["signature"] = self._generate_signature(params)
        res = requests.post(url, headers=self._headers(), params=params, timeout=10, verify=False)
        res.raise_for_status()
        return res.json()

    def get_ticker_price(self, symbol):
        url = f"{self.base_url}/fapi/v1/ticker/price"
        params = {"symbol": symbol.replace('/', '').upper()}
        res = requests.get(url, params=params, timeout=10, verify=False)
        res.raise_for_status()
        return float(res.json()["price"])

    def create_stop_market_order(self, symbol, side, stop_price, quantity):
        url = f"{self.base_url}/fapi/v1/order"
        params = {
            "symbol": symbol.replace('/', '').upper(),
            "side": side.upper(),
            "type": "STOP_MARKET",
            "quantity": f"{quantity:.3f}",
            "stopPrice": f"{stop_price}",
            "timestamp": int(time.time() * 1000)
        }
        params["signature"] = self._generate_signature(params)
        res = requests.post(url, headers=self._headers(), params=params, timeout=10, verify=False)
        res.raise_for_status()
        return res.json()

    def create_close_order(self, symbol, side, order_type, stop_price):
        url = f"{self.base_url}/fapi/v1/order"
        params = {
            "symbol": symbol.replace('/', '').upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "stopPrice": f"{stop_price}",
            "closePosition": "true",
            "timestamp": int(time.time() * 1000)
        }
        params["signature"] = self._generate_signature(params)
        res = requests.post(url, headers=self._headers(), params=params, timeout=10, verify=False)
        res.raise_for_status()
        return res.json()


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
    input_symbol = ft.TextField(label="Simbol (Contoh: BTCUSDT)", value="BTCUSDT")
    
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

    # PANGGILAN SDK RESMI GEMINI VIA PIL IMAGE
    def call_gemini_sdk(api_key, image_path, prompt):
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        img = Image.open(image_path)
        response = model.generate_content([prompt, img])
        return response.text

    async def luncurkan_execution():
        if not path_foto.value or "Belum ada" in path_foto.value:
            tampilkan_peringatan("Foto Belum Dipilih", "Silakan upload foto chart terlebih dahulu sebelum meluncurkan bot.")
            return

        layar_log.value = "Status: Menjalankan Misi..."
        layar_log.color = ft.Colors.BLUE
        page.update()

        try:
            binance = BinanceFuturesAPI(api_bin.value, api_sec.value)
            prompt = 'Berikan HANYA JSON murni (tanpa penjelasan, tanpa markdown) dengan format: {"sinyal": "VALID", "arah": "BUY", "pemicu_masuk": 0.0, "take_profit": 0.0, "stop_loss": 0.0}'
            
            loop = asyncio.get_running_loop()
            raw_response = await loop.run_in_executor(
                None, lambda: call_gemini_sdk(api_ai.value, path_foto.value, prompt)
            )
            
            raw_text = raw_response.strip().replace('```json', '').replace('```', '')
            start = raw_text.find('{')
            end = raw_text.rfind('}') + 1
            setup = json.loads(raw_text[start:end])

            if setup['sinyal'] == "VALID":
                margin = float(input_margin.value)
                lev = int(input_lev.value)
                sym = input_symbol.value.strip()

                await loop.run_in_executor(None, lambda: binance.set_leverage(sym, lev))
                price = await loop.run_in_executor(None, lambda: binance.get_ticker_price(sym))
                size = (margin * lev) / price
                
                side_u, side_p = ('BUY', 'SELL') if setup['arah'] == 'BUY' else ('SELL', 'BUY')
                
                # 1. Entry Order
                await loop.run_in_executor(None, lambda: binance.create_stop_market_order(
                    sym, side_u, setup['pemicu_masuk'], size
                ))
                # 2. Take Profit Order
                await loop.run_in_executor(None, lambda: binance.create_close_order(
                    sym, side_p, 'TAKE_PROFIT_MARKET', setup['take_profit']
                ))
                # 3. Stop Loss Order
                await loop.run_in_executor(None, lambda: binance.create_close_order(
                    sym, side_p, 'STOP_MARKET', setup['stop_loss']
                ))
                
                layar_log.value = "Status: TRIPLE SHOT BERHASIL!"
                layar_log.color = ft.Colors.GREEN
                page.update()
            else:
                tampilkan_peringatan("Sinyal Tidak Valid", "AI menilai chart saat ini kurang jelas atau tidak ada momentum yang aman untuk masuk pasar.")
                
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