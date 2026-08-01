import flet as ft
from google import genai
import ccxt
import json
import os
import asyncio
from PIL import Image

async def main(page: ft.Page):
    page.title = "Sniper Bot Mobile"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO

    # 1. Initialize Services
    sp = ft.SharedPreferences()
    file_picker = ft.FilePicker()  # Initiated directly, NO overlay append required

    # Load saved preferences
    saved_api_ai = await sp.get("api_ai") or ""
    saved_api_bin = await sp.get("api_bin") or ""
    saved_api_sec = await sp.get("api_sec") or ""
    saved_margin = await sp.get("margin") or "2"
    saved_leverage = await sp.get("leverage") or "20"

    # Async function to save local data
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
    input_symbol = ft.TextField(label="Simbol (Contoh: BTC/USDT)", value="BTC/USDT")
    
    path_foto = ft.Text("Belum ada foto dipilih")
    layar_log = ft.Text("Status: Standby", color=ft.Colors.YELLOW)

    # Clean Async FilePicker Handler
    async def pick_files_click(e):
        files = await file_picker.pick_files()
        if files and len(files) > 0:
            path_foto.value = files[0].path
            path_foto.update()

    # Pop-up Warning Function
    def tampilkan_peringatan(judul, pesan):
        layar_log.value = f"Status: {judul}"
        layar_log.color = ft.Colors.RED
        
        dialog = ft.AlertDialog(
            title=ft.Text(judul),
            content=ft.Text(pesan),
            actions=[ft.TextButton("OK", on_click=lambda _: page.close(dialog))]
        )
        page.open(dialog)
        page.update()

    # 2. Main Execution Handler
    async def luncurkan_execution():
        if not path_foto.value or "Belum ada" in path_foto.value:
            tampilkan_peringatan("Foto Belum Dipilih", "Silakan upload foto chart terlebih dahulu sebelum meluncurkan bot.")
            return

        layar_log.value = "Status: Menjalankan Misi..."
        layar_log.color = ft.Colors.BLUE
        page.update()

        try:
            client = genai.Client(api_key=api_ai.value)
            exchange = ccxt.binance({
                'apiKey': api_bin.value, 
                'secret': api_sec.value, 
                'enableRateLimit': True, 
                'options': {'defaultType': 'future'}
            })
            
            img = Image.open(path_foto.value)
            prompt = "Berikan HANYA JSON murni (tanpa penjelasan, tanpa markdown) dengan format: {'sinyal': 'VALID', 'arah': 'BUY', 'pemicu_masuk': 0.0, 'take_profit': 0.0, 'stop_loss': 0.0}"
            
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, 
                lambda: client.models.generate_content(model='gemini-2.0-flash', contents=[prompt, img])
            )
            
            raw_text = response.text.strip().replace('```json', '').replace('```', '')
            start = raw_text.find('{')
            end = raw_text.rfind('}') + 1
            setup = json.loads(raw_text[start:end])

            if setup['sinyal'] == "VALID":
                margin = float(input_margin.value)
                lev = int(input_lev.value)
                
                await loop.run_in_executor(None, lambda: exchange.set_leverage(lev, input_symbol.value))
                ticker = await loop.run_in_executor(None, lambda: exchange.fetch_ticker(input_symbol.value))
                price = ticker['last']
                size = (margin * lev) / price
                
                side_u, side_p = ('buy', 'sell') if setup['arah'] == 'BUY' else ('sell', 'buy')
                
                await loop.run_in_executor(None, lambda: exchange.create_order(
                    input_symbol.value, 'STOP_MARKET', side_u, size, setup['pemicu_masuk'], {'stopPrice': setup['pemicu_masuk']}
                ))
                await loop.run_in_executor(None, lambda: exchange.create_order(
                    input_symbol.value, 'TAKE_PROFIT_MARKET', side_p, size, setup['take_profit'], {'stopPrice': setup['take_profit'], 'reduceOnly': True}
                ))
                await loop.run_in_executor(None, lambda: exchange.create_order(
                    input_symbol.value, 'STOP_MARKET', side_p, size, setup['stop_loss'], {'stopPrice': setup['stop_loss'], 'reduceOnly': True}
                ))
                
                layar_log.value = "Status: TRIPLE SHOT BERHASIL!"
                layar_log.color = ft.Colors.GREEN
                page.update()
            else:
                tampilkan_peringatan("Sinyal Tidak Valid", "AI menilai chart saat ini kurang jelas atau tidak ada momentum yang aman untuk masuk pasar.")
                
        except ccxt.AuthenticationError:
            tampilkan_peringatan("Gagal Otentikasi", "API Key atau Secret Binance Anda salah, atau izin Futures belum diaktifkan.")
        except ccxt.InsufficientFunds:
            tampilkan_peringatan("Saldo Tidak Cukup", "Saldo USDT Futures Anda tidak mencukupi untuk membuka posisi dengan margin tersebut.")
        except ccxt.NetworkError:
            tampilkan_peringatan("Gangguan Jaringan", "Gagal terhubung ke server Binance. Periksa kembali koneksi internet Anda.")
        except Exception as ex:
            tampilkan_peringatan("Terjadi Kesalahan", f"Pesan sistem: {str(ex)}")

    def on_luncurkan_click(e):
        asyncio.create_task(luncurkan_execution())

    # 3. Layout UI
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