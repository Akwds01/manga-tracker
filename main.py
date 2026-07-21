import os
import io
import time
import json
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper
import telebot
from threading import Thread
from PIL import Image

# =========================================================================
# ⚙️ KONFIGURASI BOT & DATABASE
# =========================================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = int(os.environ.get("TELEGRAM_CHAT_ID")) if os.environ.get("TELEGRAM_CHAT_ID") else 0

BANNER_MENU_URL = "https://images.unsplash.com/photo-1578632767115-351597cf2477?w=1000&q=80"

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
user_main_message = {}
user_quality_pref = {}

# =========================================================================
# 🗄️ DATABASE INITIALIZATION & MIGRATION
# =========================================================================

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_tracks (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            title VARCHAR(255),
            last_chapter VARCHAR(50) DEFAULT '0',
            last_read VARCHAR(50) DEFAULT 'Belum Dibaca',
            url TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, url)
        )
    """)
    cursor.execute("ALTER TABLE user_tracks ADD COLUMN IF NOT EXISTS last_read VARCHAR(50) DEFAULT 'Belum Dibaca';")
    cursor.execute("ALTER TABLE user_tracks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
    conn.commit()
    cursor.close()
    conn.close()

# =========================================================================
# 🛠️ HELPER SAFE EDIT DASHBOARD & SCRAPING
# =========================================================================

def bersihkan_markdown(text):
    """Menghapus karakter yang merusak formatting Markdown Telegram"""
    if not text:
        return ""
    for char in ['*', '_', '`', '[', ']', '(', ')']:
        text = text.replace(char, '')
    return text

def edit_dashboard(chat_id, message_id, text, reply_markup=None):
    """Fungsi pintar update dashboard: Menangani limit caption 1024 char & error media"""
    # 1. Coba edit caption jika teks muat di caption foto (<= 1000 char)
    if len(text) <= 1000:
        try:
            bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, parse_mode="Markdown", reply_markup=reply_markup)
            return
        except Exception:
            pass

    # 2. Coba edit sebagai text biasa (jika pesan lama adalah pesan teks)
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)
        return
    except Exception:
        pass

    # 3. Jika gagal (misal teks > 1000 char di foto), Hapus pesan lama & kirim pesan baru!
    try:
        if message_id:
            bot.delete_message(chat_id, message_id)
    except Exception:
        pass

    try:
        new_msg = bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
        user_main_message[chat_id] = new_msg.message_id
    except Exception:
        # Fallback tanpa markdown jika ada sintaks bermasalah
        clean_text = text.replace('*', '').replace('_', '').replace('`', '')
        new_msg = bot.send_message(chat_id, clean_text, reply_markup=reply_markup)
        user_main_message[chat_id] = new_msg.message_id

def ekstrak_data_komik(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    chapter_terbaru = None
    image_url = None
    url_chapter_terbaru = None
    
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        image_url = meta_img['content']

    container = soup.find(id='Daftar_Chapter') or soup.find(id='daftar_chapter')
    if container and container.find('a'):
        a_tag = container.find('a')
        chapter_terbaru = " ".join(a_tag.text.strip().split())
        url_chapter_terbaru = a_tag.get('href')
            
    if not chapter_terbaru:
        container_ms = soup.find(id='chapterlist') or soup.find(class_='cl')
        if container_ms and container_ms.find('a'):
            a_tag = container_ms.find('a')
            chapter_terbaru = " ".join(a_tag.text.strip().split())
            url_chapter_terbaru = a_tag.get('href')

    if url_chapter_terbaru and url_chapter_terbaru.startswith('/'):
        url_chapter_terbaru = f"https://komiku.org{url_chapter_terbaru}"

    return chapter_terbaru, image_url, url_chapter_terbaru

def ekstrak_gambar_chapter(url_chapter):
    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_chapter, timeout=15)
        if res.status_code != 200:
            return []
            
        soup = BeautifulSoup(res.text, 'html.parser')
        container = soup.find(id='Baca_Komik') or soup.find(class_='baca-komik') or soup.find(id='chimg-container') or soup.find(id='Baca_Komik_2')
        
        images = []
        if container:
            for img in container.find_all('img'):
                src = img.get('src') or img.get('data-src')
                if src:
                    src = src.strip()
                    if src.startswith('//'):
                        src = 'https:' + src
                    if src.startswith('http'):
                        images.append(src)
        return images
    except Exception as e:
        print(f"Error scraping gambar chapter: {e}")
        return []

def buat_pdf_dari_gambar(image_urls, referer_url=None, quality="HD"):
    scraper = cloudscraper.create_scraper()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer_url if referer_url else "https://komiku.org/"
    }
    
    pil_images = []
    for url in image_urls:
        try:
            resp = scraper.get(url, headers=headers, timeout=12)
            if resp.status_code == 200:
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                if quality == "SD":
                    new_size = (max(1, img.width // 2), max(1, img.height // 2))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                pil_images.append(img)
        except Exception as e:
            print(f"Gagal unduh halaman {url}: {e}")
            
    if not pil_images:
        return None

    try:
        pdf_buffer = io.BytesIO()
        pil_images[0].save(pdf_buffer, format='PDF', save_all=True, append_images=pil_images[1:])
        pdf_buffer.seek(0)
        return pdf_buffer
    except Exception as e:
        print(f"Error menyusun file PDF: {e}")
        return None

def update_last_read_status(user_id, url_chapter):
    try:
        clean_ch = url_chapter.rstrip('/').split('/')[-1].replace('-', ' ').title()
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_tracks 
            SET last_read = %s 
            WHERE user_id = %s AND %s LIKE '%' || LOWER(REPLACE(title, ' ', '-')) || '%';
        """, (clean_ch, user_id, url_chapter.lower()))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Gagal update last read: {e}")

def eksekusi_unduh_pdf(chat_id, url_chapter, status_msg_id=None):
    quality = user_quality_pref.get(chat_id, "HD")
    
    if status_msg_id:
        edit_dashboard(chat_id, status_msg_id, f"⏳ *Mengekstrak halaman komik...* (Mode: `{quality}`)")
    else:
        status_msg = bot.send_message(chat_id, f"⏳ *Mengekstrak halaman komik...* (Mode: `{quality}`)", parse_mode="Markdown")
        status_msg_id = status_msg.message_id

    image_urls = ekstrak_gambar_chapter(url_chapter)
    if not image_urls:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))
        bot.send_message(chat_id, "❌ Gagal menemukan gambar di link tersebut.", reply_markup=markup)
        return

    edit_dashboard(chat_id, status_msg_id, f"📥 *Mengunduh {len(image_urls)} halaman & menyusun PDF (`{quality}`)...*")

    pdf_file = buat_pdf_dari_gambar(image_urls, referer_url=url_chapter, quality=quality)
    if not pdf_file:
        bot.send_message(chat_id, "❌ Gagal mengonversi gambar ke PDF.")
        return

    clean_name = url_chapter.rstrip('/').split('/')[-1]
    judul_file = f"{clean_name}_{quality}.pdf"
    
    try:
        bot.send_document(
            chat_id=chat_id,
            document=(judul_file, pdf_file),
            caption=f"✅ *Download PDF Selesai!*\n📖 `{judul_file}`\n⚡ Mode Kualitas: `{quality}`",
            parse_mode="Markdown"
        )
        update_last_read_status(chat_id, url_chapter)

        if chat_id in user_main_message and user_main_message[chat_id] == status_msg_id:
            pesan = dapatkan_text_utama(bot.get_chat(chat_id).first_name or "User")
            edit_dashboard(chat_id, status_msg_id, pesan, markup_utama(chat_id))
        else:
            bot.delete_message(chat_id, status_msg_id)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Gagal mengirim file PDF: {e}")

# =========================================================================
# 🎛️ DASHBOARD UI
# =========================================================================

def dapatkan_text_utama(nama_user):
    return (
        f"👑 *WILA STORE | MANGA TRACKER & DOWNLOADER* 👑\n"
        f"───────────────────────────\n"
        f"Halo *{nama_user}*! 👋\n\n"
        f"Selamat datang di sistem manajemen tracker & downloader otomatis.\n\n"
        f"⚡ *Status Layanan:* `ONLINE (Lancar) ✅`\n"
        f"📌 *Tracker:* Auto Scan & Last Read Marker\n"
        f"⚙️ *Mode PDF saat ini:* `{user_quality_pref.get(ADMIN_ID, 'HD')}`\n"
        f"───────────────────────────\n"
        f"Silakan gunakan menu interaktif di bawah ini:"
    )

def markup_utama(user_id):
    q_mode = user_quality_pref.get(user_id, "HD")
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(text="➕ Tambah Tracker", callback_data="btn_tambah"),
        telebot.types.InlineKeyboardButton(text="📋 Daftar Tracker", callback_data="btn_daftar")
    )
    markup.row(
        telebot.types.InlineKeyboardButton(text="📥 Download Single PDF", callback_data="btn_download"),
        telebot.types.InlineKeyboardButton(text="📦 Batch Download", callback_data="btn_batch")
    )
    markup.row(
        telebot.types.InlineKeyboardButton(text=f"⚙️ Kualitas PDF: [{q_mode}]", callback_data="toggle_quality"),
        telebot.types.InlineKeyboardButton(text="💾 Backup / Restore", callback_data="btn_backup_menu")
    )
    if user_id == ADMIN_ID:
        markup.row(telebot.types.InlineKeyboardButton(text="⚙️ Menu Panel Admin", callback_data="btn_admin"))
    return markup

# =========================================================================
# 🤖 ROUTING HANDLERS
# =========================================================================

@bot.message_handler(commands=['start', 'help'])
def command_start(message):
    user_id = message.chat.id
    hapus_reply = telebot.types.ReplyKeyboardRemove()
    msg_info = bot.send_message(user_id, "⚡ Menginisialisasi Dashboard...", reply_markup=hapus_reply)
    bot.delete_message(user_id, msg_info.message_id)

    pesan = dapatkan_text_utama(message.from_user.first_name)
    try:
        main_msg = bot.send_photo(user_id, BANNER_MENU_URL, caption=pesan, parse_mode="Markdown", reply_markup=markup_utama(user_id))
        user_main_message[user_id] = main_msg.message_id
    except Exception as e:
        main_msg = bot.send_message(user_id, pesan, parse_mode="Markdown", reply_markup=markup_utama(user_id))
        user_main_message[user_id] = main_msg.message_id

@bot.message_handler(commands=['dl', 'download'])
def handle_download_pdf(message):
    user_id = message.chat.id
    text_args = message.text.split()
    if len(text_args) < 2:
        bot.send_message(user_id, "⚠️ *Format Perintah Salah!*\n\nGunakan: `/dl <URL_CHAPTER>`", parse_mode="Markdown")
        return
    eksekusi_unduh_pdf(user_id, text_args[1].strip())

@bot.callback_query_handler(func=lambda call: True)
def callback_router(call):
    user_id = call.message.chat.id
    msg_id = call.message.message_id
    user_main_message[user_id] = msg_id

    try:
        if call.data == "go_home":
            bot.answer_callback_query(call.id, "Kembali")
            pesan = dapatkan_text_utama(call.from_user.first_name)
            edit_dashboard(user_id, msg_id, pesan, markup_utama(user_id))

        elif call.data == "toggle_quality":
            curr = user_quality_pref.get(user_id, "HD")
            new_q = "SD" if curr == "HD" else "HD"
            user_quality_pref[user_id] = new_q
            bot.answer_callback_query(call.id, f"Kualitas PDF diubah ke {new_q}!")
            pesan = dapatkan_text_utama(call.from_user.first_name)
            edit_dashboard(user_id, msg_id, pesan, markup_utama(user_id))

        elif call.data == "btn_tambah":
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan", callback_data="go_home"))
            edit_dashboard(user_id, msg_id, "🔗 Kirim **URL Utama Komik** dari Komiku:", markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_url_manual)

        elif call.data == "btn_download":
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan", callback_data="go_home"))
            edit_dashboard(user_id, msg_id, "📥 Kirim **URL Chapter Komik** yang ingin diunduh:", markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_url_download_menu)

        elif call.data == "btn_batch":
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan", callback_data="go_home"))
            pesan = (
                "📦 *BATCH DOWNLOAD PDF CHAPTER*\n"
                "───────────────────────────\n"
                "Silakan kirimkan **beberapa URL Chapter** sekaligus (satu URL per baris / pisah spasi):\n\n"
                "*Contoh:*\n"
                "`https://komiku.org/ch/chapter-100/`\n"
                "`https://komiku.org/ch/chapter-101/`"
            )
            edit_dashboard(user_id, msg_id, pesan, markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_batch_download)

        elif call.data == "btn_backup_menu":
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton(text="📤 Export Backup (JSON)", callback_data="exec_export_backup"),
                telebot.types.InlineKeyboardButton(text="📥 Import Backup (JSON)", callback_data="exec_import_backup")
            )
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Menu Utama", callback_data="go_home"))
            edit_dashboard(user_id, msg_id, "💾 *MANAJEMEN BACKUP & RESTORE DATABASE TRACKER*", markup)

        elif call.data == "exec_export_backup":
            bot.answer_callback_query(call.id, "Mengeksport data...")
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT title, url, last_chapter, last_read FROM user_tracks WHERE user_id = %s", (user_id,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()

            data_export = [{"title": r[0], "url": r[1], "last_chapter": r[2], "last_read": r[3]} for r in rows]
            json_bytes = io.BytesIO(json.dumps(data_export, indent=2).encode('utf-8'))
            
            bot.send_document(
                chat_id=user_id,
                document=("wila_manga_backup.json", json_bytes),
                caption=f"✅ *Export Berhasil!* Menyimpan `{len(data_export)}` daftar komik.",
                parse_mode="Markdown"
            )

        elif call.data == "exec_import_backup":
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="btn_backup_menu"))
            edit_dashboard(user_id, msg_id, "📥 Silakan **upload file `.json`** hasil backup kamu:", markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_file_import)

        elif call.data.startswith("dln_"):
            db_id = int(call.data.split('_')[1])
            bot.answer_callback_query(call.id, "⚡ Memulai pengunduhan PDF...", show_alert=False)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM user_tracks WHERE id = %s", (db_id,))
            res = cursor.fetchone()
            cursor.close()
            conn.close()

            if res:
                manga_url = res[0]
                scraper = cloudscraper.create_scraper()
                try:
                    resp = scraper.get(manga_url, timeout=12)
                    _, _, latest_ch_url = ekstrak_data_komik(resp.text)
                    if latest_ch_url:
                        eksekusi_unduh_pdf(user_id, latest_ch_url)
                    else:
                        bot.send_message(user_id, "❌ Gagal mendapatkan link chapter terbaru.")
                except Exception as e:
                    bot.send_message(user_id, f"❌ Error: {e}")

        elif call.data == "btn_daftar":
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title, last_chapter, last_read, url, updated_at FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
            data = cursor.fetchall()
            cursor.close()
            conn.close()

            if not data:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.row(telebot.types.InlineKeyboardButton(text="🔙 Menu Utama", callback_data="go_home"))
                edit_dashboard(user_id, msg_id, "❌ *Kamu belum memantau komik apa pun.*", markup)
                return

            pesan = f"📋 *Daftar Tracker Aktif Kamu ({len(data)} Judul):*\n───────────────────────────\n"
            for idx, (db_id, title, last_ch, last_rd, url, updated) in enumerate(data, 1):
                clean_t = bersihkan_markdown(title)
                clean_last_ch = bersihkan_markdown(last_ch)
                clean_last_rd = bersihkan_markdown(last_rd)
                tgl_update = updated.strftime("%d/%m/%Y") if updated else "-"
                
                pesan += (
                    f"{idx}. 📖 [{clean_t}]({url})\n"
                    f"     ✨ Posisi Web: `{clean_last_ch}`\n"
                    f"     📌 Terakhir Dibaca: `{clean_last_rd}`\n"
                    f"     🗓️ Last Scan: `{tgl_update}`\n\n"
                )
                
            pesan += f"───────────────────────────\n💡 Klik nama judul untuk membaca langsung di web."
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton(text="🗑️ Hapus Tracker", callback_data="manage_del"),
                telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home")
            )
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data == "manage_del":
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
            data = cursor.fetchall()
            cursor.close()
            conn.close()

            if not data:
                call.data = "btn_daftar"
                callback_router(call)
                return

            pesan = "🗑️ *PILIH NOMOR UNTUK MENGHAPUS TRACKER:*\n───────────────────────────\n"
            for idx, (db_id, title) in enumerate(data, 1):
                pesan += f" [{idx}]  *{bersihkan_markdown(title)}*\n"

            markup = telebot.types.InlineKeyboardMarkup()
            row_buttons = []
            for idx, (db_id, title) in enumerate(data, 1):
                row_buttons.append(telebot.types.InlineKeyboardButton(text=f" [{idx}] ", callback_data=f"exec_del_{db_id}"))
                if len(row_buttons) == 5:
                    markup.row(*row_buttons)
                    row_buttons = []
            if row_buttons:
                markup.row(*row_buttons)

            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali", callback_data="btn_daftar"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data.startswith("exec_del_"):
            db_id = int(call.data.split('_')[2])
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_tracks WHERE id = %s AND user_id = %s RETURNING title", (db_id, user_id))
            deleted = cursor.fetchone()
            conn.commit()
            cursor.close()
            conn.close()
            
            bot.answer_callback_query(call.id, f"Sukses Menghapus!", show_alert=False)
            call.data = "manage_del"
            callback_router(call)

        elif call.data == "btn_admin" and user_id == ADMIN_ID:
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton(text="📊 Statistik Bot", callback_data="admin_stats"),
                telebot.types.InlineKeyboardButton(text="📢 Broadcast Global", callback_data="admin_bc")
            )
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Menu Utama", callback_data="go_home"))
            edit_dashboard(user_id, msg_id, "🛠️ *Panel Owner WILA STORE:*", markup)

        elif call.data == "admin_stats" and user_id == ADMIN_ID:
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(DISTINCT user_id), COUNT(*) FROM user_tracks")
            users, total_tracks = cursor.fetchone()
            cursor.close()
            conn.close()

            pesan = f"📊 *STATISTIK BOT REAL-TIME*\n───────────────────────────\n👥 User Unik: `{users}` Orang\n📌 Total Tracker: `{total_tracks}` Item"
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Panel Admin", callback_data="btn_admin"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data == "admin_bc" and user_id == ADMIN_ID:
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="btn_admin"))
            edit_dashboard(user_id, msg_id, "📢 Kirim pesan broadcast massal:", markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_pesan_broadcast)

    except Exception as e:
        print(f"Error callback handler: {e}")
        bot.answer_callback_query(call.id, "Terjadi kesalahan sistem.", show_alert=False)

# =========================================================================
# 📥 NEXT STEP HANDLERS
# =========================================================================

def tangkap_batch_download(message):
    user_id = message.chat.id
    raw_text = message.text.strip()
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    urls = [u.strip() for u in raw_text.replace('\n', ' ').split() if u.strip().startswith("http")]
    if not urls:
        bot.send_message(user_id, "❌ Tidak ditemukan URL valid. Pastikan diawali `http://` atau `https://`.")
        return

    bot.send_message(user_id, f"🚀 *Memproses Batch Download untuk {len(urls)} Chapter...*", parse_mode="Markdown")
    for idx, url in enumerate(urls, 1):
        bot.send_message(user_id, f"📦 *Processing Chapter ({idx}/{len(urls)})...*", parse_mode="Markdown")
        eksekusi_unduh_pdf(user_id, url)
        time.sleep(1)

def tangkap_file_import(message):
    user_id = message.chat.id
    if not message.document or not message.document.file_name.endswith('.json'):
        bot.send_message(user_id, "❌ Harap kirimkan file berformat `.json`!")
        return

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        items = json.loads(downloaded.decode('utf-8'))

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        inserted = 0
        for item in items:
            cursor.execute("""
                INSERT INTO user_tracks (user_id, title, last_chapter, last_read, url)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, url) DO NOTHING;
            """, (user_id, item['title'], item.get('last_chapter', '0'), item.get('last_read', 'Belum Dibaca'), item['url']))
            inserted += cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        bot.send_message(user_id, f"✅ *Import Selesai!* Berhasil menambahkan `{inserted}` komik baru.", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(user_id, f"❌ Gagal memproses file import: {e}")

def tangkap_url_download_menu(message):
    user_id = message.chat.id
    url_input = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not url_input.startswith("http"):
        bot.send_message(user_id, "❌ Format link salah!")
        return

    eksekusi_unduh_pdf(user_id, url_input, status_msg_id=msg_dashboard_id)

def tangkap_url_manual(message):
    user_id = message.chat.id
    url_input = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not url_input.startswith("http"):
        bot.send_message(user_id, "❌ Format link salah!")
        return

    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_input, timeout=12)
        if res.status_code != 200:
            bot.send_message(user_id, "❌ Gagal koneksi ke web komik.")
            return

        chapter, img, _ = ekstrak_data_komik(res.text)
        if not chapter:
            bot.send_message(user_id, "❌ Gagal mengekstrak chapter komik.")
            return

        title_slug = url_input.split('/manga/')[-1].replace('/', '').replace('-', ' ').title()
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_tracks (user_id, title, last_chapter, url)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, url) DO UPDATE SET last_chapter = EXCLUDED.last_chapter;
        """, (user_id, title_slug, chapter, url_input))
        conn.commit()
        cursor.close()
        conn.close()

        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="📋 Lihat Daftar", callback_data="btn_daftar"))
        markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))
        edit_dashboard(user_id, msg_dashboard_id, f"✅ *TRACKER AKTIF!*\n\n📖 Komik: *{bersihkan_markdown(title_slug)}*\n⚡ Posisi: `{bersihkan_markdown(chapter)}`", markup)
    except Exception as e:
        print(f"Error manual: {e}")

def tangkap_pesan_broadcast(message):
    user_id = message.chat.id
    pesan_bc = message.text.strip()
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM user_tracks")
    users = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()

    sukses = 0
    for u_id in users:
        try:
            bot.send_message(u_id, f"📢 *PEMBERITAHUAN WILA STORE:*\n\n{pesan_bc}", parse_mode="Markdown")
            sukses += 1
            time.sleep(0.05)
        except:
            pass

    bot.send_message(user_id, f"✅ Broadcast dikirim ke `{sukses}` user.", parse_mode="Markdown")

# =========================================================================
# 🕵️ WORKER BACKGROUND SCRAPER
# =========================================================================

def refresh_loop_multiuser():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT url FROM user_tracks")
    urls = [r[0] for r in cursor.fetchall()]
    
    if not urls:
        cursor.close()
        conn.close()
        return

    scraper = cloudscraper.create_scraper()
    for url in urls:
        try:
            res = scraper.get(url, timeout=15)
            if res.status_code == 200:
                chapter_web, img_web, url_chapter_terbaru = ekstrak_data_komik(res.text)

                if chapter_web:
                    cursor.execute("SELECT id, user_id, title, last_chapter FROM user_tracks WHERE url = %s", (url,))
                    registered_users = cursor.fetchall()
                    
                    for track_id, user_id, title, last_chapter in registered_users:
                        if last_chapter == '0':
                            cursor.execute("UPDATE user_tracks SET last_chapter = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s AND url = %s", (chapter_web, user_id, url))
                            conn.commit()
                        elif chapter_web != last_chapter:
                            pesan_notif = (
                                f"🔥 *UPDATE MANGA HYPE RELEASE!* 🔥\n"
                                f"───────────────────────────\n"
                                f"📖 Judul: *{bersihkan_markdown(title)}*\n"
                                f"✨ Rilis Baru: *{bersihkan_markdown(chapter_web)}*\n"
                                f"📥 Status DB: (Lama: `{bersihkan_markdown(last_chapter)}`)\n"
                                f"───────────────────────────"
                            )
                            markup = telebot.types.InlineKeyboardMarkup()
                            web_link = url_chapter_terbaru if url_chapter_terbaru else url
                            markup.row(
                                telebot.types.InlineKeyboardButton(text="🚀 Baca di Web", url=web_link),
                                telebot.types.InlineKeyboardButton(text="📥 Download PDF", callback_data=f"dln_{track_id}")
                            )
                            try:
                                if img_web:
                                    bot.send_photo(user_id, img_web, caption=pesan_notif, parse_mode="Markdown", reply_markup=markup)
                                else:
                                    bot.send_message(user_id, pesan_notif, parse_mode="Markdown", reply_markup=markup)
                            except Exception as e:
                                print(f"Gagal kirim update: {e}")
                                
                            cursor.execute("UPDATE user_tracks SET last_chapter = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s AND url = %s", (chapter_web, user_id, url))
                            conn.commit()
        except Exception as e:
            print(f"Error background loop: {e}")

    cursor.close()
    conn.close()

def loop_background_worker():
    init_db()
    while True:
        try:
            refresh_loop_multiuser()
        except Exception as e:
            print(f"Gagal loop worker: {e}")
        time.sleep(900)

# =========================================================================
# 🚀 MAIN RUNNER
# =========================================================================

if __name__ == "__main__":
    init_db()
    
    worker = Thread(target=loop_background_worker)
    worker.daemon = True
    worker.start()
    
    print("Bot Premium WILA STORE All-In-One Features Aktif...")
    bot.infinity_polling()
