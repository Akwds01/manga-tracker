import os
import io
import time
import json
import gc
import re
import tempfile
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper
import telebot
from threading import Thread
from PIL import Image
import img2pdf

# =========================================================================
# ⚙️ KONFIGURASI BOT & DATABASE
# =========================================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = int(os.environ.get("TELEGRAM_CHAT_ID")) if os.environ.get("TELEGRAM_CHAT_ID") else 0

BANNER_MENU_URL = "https://images.unsplash.com/photo-1578632767115-351597cf2477?w=1000&q=80"

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

telebot.apihelper.CUSTOM_REQUEST_TIMEOUT = 300
telebot.apihelper.CONNECT_TIMEOUT = 60

bot = telebot.TeleBot(TELEGRAM_TOKEN)
user_main_message = {}
user_quality_pref = {}
user_selected_manga = {}
user_chapter_storage = {}
next_chapter_cache = {}

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
# 🛠️ HELPER SCRAPING, DETAIL PARSER & CONVERTER
# =========================================================================

def bersihkan_markdown(text):
    if not text:
        return ""
    for char in ['*', '_', '`', '[', ']', '(', ')']:
        text = text.replace(char, '')
    return text

def edit_dashboard(chat_id, message_id, text, reply_markup=None):
    if len(text) <= 1000:
        try:
            bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, parse_mode="Markdown", reply_markup=reply_markup)
            return
        except Exception:
            pass

    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)
        return
    except Exception:
        pass

    try:
        if message_id:
            bot.delete_message(chat_id, message_id)
    except Exception:
        pass

    try:
        new_msg = bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
        user_main_message[chat_id] = new_msg.message_id
    except Exception:
        clean_text = text.replace('*', '').replace('_', '').replace('`', '')
        new_msg = bot.send_message(chat_id, clean_text, reply_markup=reply_markup)
        user_main_message[chat_id] = new_msg.message_id

def parse_range_input(text):
    match = re.search(r'(\d+)\s*[-_to sampai]+\s*(\d+)', text, re.IGNORECASE)
    if match:
        start_num = int(match.group(1))
        end_num = int(match.group(2))
        if start_num <= end_num:
            return start_num, end_num
        return end_num, start_num
    return None, None

def extract_latest_ch_number(last_ch_str):
    if not last_ch_str:
        return 10
    match = re.search(r'(\d+)', str(last_ch_str))
    if match:
        return int(match.group(1))
    return 10

def parse_manga_slug_and_ch(url_chapter):
    url_clean = url_chapter.rstrip('/')
    parts = url_clean.split('/')
    ch_slug = parts[-1]
    
    match = re.search(r'^(.*?)[-_]chapter[-_](\d+(\.\d+)?)$', ch_slug, re.IGNORECASE)
    if match:
        slug = match.group(1)
        ch_num = float(match.group(2)) if '.' in match.group(2) else int(match.group(2))
        return slug, ch_num
    
    match_num = re.search(r'(\d+(\.\d+)?)', ch_slug)
    if match_num:
        ch_num = float(match_num.group(1)) if '.' in match_num.group(1) else int(match_num.group(1))
        slug = re.sub(r'[-_]chapter[-_]?\d+(\.\d+)?$', '', ch_slug, flags=re.IGNORECASE)
        return slug, ch_num
        
    return None, None

def ekstrak_nomor_chapter_singkat(title):
    match = re.search(r'(chapter|ch|bab)[-_ ]?(\d+(\.\d+)?)', title, re.IGNORECASE)
    if match:
        return f"Ch {match.group(2)}"
    return title[:8]

def ekstrak_detail_komik(url_manga):
    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_manga, timeout=12)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, 'html.parser')
        
        status = "Ongoing"
        genre = "Tidak Diketahui"
        
        infolist = soup.find(class_='infolist') or soup.find(class_='sd') or soup.find('table')
        if infolist:
            text_all = infolist.text
            if 'Tamat' in text_all or 'Completed' in text_all:
                status = "Tamat (Completed)"
            elif 'Berjalan' in text_all or 'Ongoing' in text_all:
                status = "Ongoing"
            
            genre_tags = soup.find_all('a', href=re.compile(r'/genre/'))
            if genre_tags:
                genres = [g.text.strip() for g in genre_tags[:4] if g.text.strip()]
                if genres:
                    genre = ", ".join(genres)

        sinopsis = "Sinopsis tidak tersedia."
        desc_container = soup.find(id='Judul') or soup.find(class_='desc') or soup.find(class_='sinopsis') or soup.find('p', class_='desc')
        if desc_container:
            p_tag = desc_container.find('p') or desc_container
            clean_p = " ".join(p_tag.text.strip().split())
            if clean_p and len(clean_p) > 10:
                sinopsis = clean_p[:220] + "..." if len(clean_p) > 220 else clean_p

        ch_terbaru, img_url, _ = ekstrak_data_komik(res.text)

        return {
            "status": status,
            "genre": genre,
            "sinopsis": sinopsis,
            "latest_ch": ch_terbaru or "Unknown",
            "img_url": img_url
        }
    except Exception as e:
        print(f"Error detail komik: {e}")
        return None

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

def ekstrak_daftar_chapter(url_manga):
    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_manga, timeout=15)
        if res.status_code != 200:
            return []
            
        soup = BeautifulSoup(res.text, 'html.parser')
        container = (
            soup.find(id='Daftar_Chapter') or 
            soup.find(class_='Daftar_Chapter') or 
            soup.find(id='daftar_chapter') or 
            soup.find(id='chapterlist') or 
            soup.find(class_='cl') or
            soup
        )
        
        chapters = []
        for a in container.find_all('a'):
            href = a.get('href', '')
            if not href:
                continue
                
            if '/ch/' in href or 'chapter' in href.lower():
                if href.startswith('/'):
                    href = f"https://komiku.org{href}"
                
                if any(x in href for x in ['/genre/', '/category/', '/manga/']) and '/ch/' not in href:
                    continue

                title = " ".join(a.text.strip().split())
                if not title or len(title) < 2:
                    title = a.get('title', '') or href.rstrip('/').split('/')[-1].replace('-', ' ').title()

                if not any(c['url'] == href for c in chapters):
                    chapters.append({'title': title, 'url': href})
        
        return chapters[:15]
    except Exception as e:
        print(f"Error ekstrak chapter list: {e}")
        return []

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
    
    temp_dir = tempfile.mkdtemp()
    temp_files = []
    
    total_pages = len(image_urls)
    if total_pages >= 80:
        max_width = 750 if quality == "HD" else 550
        jpg_quality = 60 if quality == "HD" else 45
    elif total_pages >= 50:
        max_width = 850 if quality == "HD" else 600
        jpg_quality = 65 if quality == "HD" else 50
    else:
        max_width = 1000 if quality == "HD" else 700
        jpg_quality = 75 if quality == "HD" else 50

    try:
        for idx, url in enumerate(image_urls):
            try:
                resp = scraper.get(url, headers=headers, timeout=12)
                if resp.status_code == 200:
                    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                    
                    if img.width > max_width:
                        ratio = max_width / float(img.width)
                        new_height = int(float(img.height) * ratio)
                        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                    
                    temp_path = os.path.join(temp_dir, f"page_{idx:03d}.jpg")
                    img.save(temp_path, "JPEG", quality=jpg_quality)
                    temp_files.append(temp_path)
                    
                    img.close()
                    del img
            except Exception as e:
                print(f"Gagal unduh halaman {url}: {e}")
                
        if not temp_files:
            return None

        temp_files.sort()
        pdf_bytes = img2pdf.convert(temp_files)
        pdf_buffer = io.BytesIO(pdf_bytes)
        return pdf_buffer

    except Exception as e:
        print(f"Error menyusun file PDF: {e}")
        return None

    finally:
        for f in temp_files:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass
        if os.path.exists(temp_dir):
            try: os.rmdir(temp_dir)
            except: pass
        gc.collect()

def update_last_read_status(user_id, url_chapter):
    try:
        slug_ch = url_chapter.rstrip('/').split('/')[-1]
        match_ch = re.search(r'(chapter|ch|bab)[-_]?(\d+(\.\d+)?)', slug_ch, re.IGNORECASE)
        if match_ch:
            clean_ch = f"Chapter {match_ch.group(2)}"
        else:
            clean_ch = slug_ch.replace('-', ' ').title()

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT id, url FROM user_tracks WHERE user_id = %s", (user_id,))
        rows = cursor.fetchall()

        for db_id, manga_url in rows:
            manga_slug = manga_url.rstrip('/').split('/')[-1]
            if manga_slug and manga_slug in slug_ch:
                cursor.execute(
                    "UPDATE user_tracks SET last_read = %s WHERE id = %s",
                    (clean_ch, db_id)
                )
                conn.commit()
                break

        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Gagal update last read: {e}")

# Menerima parameter is_batch untuk menentukan apakah Tombol Next Chapter perlu dimunculkan
def eksekusi_unduh_pdf(chat_id, url_chapter, status_msg_id=None, is_batch=False):
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

    update_last_read_status(chat_id, url_chapter)

    manga_info_caption = ""
    slug, current_ch = parse_manga_slug_and_ch(url_chapter)
    
    try:
        if slug:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT title, last_read, last_chapter FROM user_tracks WHERE user_id = %s AND LOWER(url) LIKE %s",
                (chat_id, f"%{slug}%")
            )
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if row:
                m_title, m_last_read, m_last_ch = row
                manga_info_caption = (
                    f"📖 *Komik:* `{bersihkan_markdown(m_title)}`\n"
                    f"📌 *Terakhir Dibaca:* `{bersihkan_markdown(m_last_read)}`\n"
                    f"⚡ *Status Web Saat Ini:* `{bersihkan_markdown(m_last_ch)}`\n"
                )
    except Exception as e:
        print(f"Gagal mengambil info komik untuk caption: {e}")

    clean_name = url_chapter.rstrip('/').split('/')[-1]
    judul_file = f"{clean_name}_{quality}.pdf"
    
    # Logic memunculkan tombol Next Chapter HANYA jika bukan Batch Download
    markup_next = None
    if not is_batch:
        markup_next = telebot.types.InlineKeyboardMarkup()
        if slug and current_ch is not None:
            next_num = int(current_ch + 1) if isinstance(current_ch, int) or current_ch.is_integer() else current_ch + 1
            next_url = f"https://komiku.org/ch/{slug}-chapter-{next_num}/"
            cache_key = f"{chat_id}_{next_num}"
            next_chapter_cache[cache_key] = next_url
            
            markup_next.row(
                telebot.types.InlineKeyboardButton(text=f"➡️ Lanjut Chapter {next_num}", callback_data=f"exec_nxt_{cache_key}")
            )

    caption_text = (
        f"✅ *DOWNLOAD PDF SELESAI!*\n"
        f"───────────────────────────\n"
        f"{manga_info_caption}"
        f"⚙️ *Mode Kualitas:* `{quality}`"
    )

    try:
        bot.send_document(
            chat_id=chat_id,
            document=(judul_file, pdf_file),
            caption=caption_text,
            parse_mode="Markdown",
            reply_markup=markup_next,
            timeout=300
        )

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
        f"📌 *Tracker:* Auto Scan & Multi-Reading Progress\n"
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
        telebot.types.InlineKeyboardButton(text="📚 Daftar Bacaan", callback_data="btn_bacaan"),
        telebot.types.InlineKeyboardButton(text="📥 Download Single PDF", callback_data="btn_download")
    )
    markup.row(
        telebot.types.InlineKeyboardButton(text="📦 Batch Download", callback_data="btn_batch"),
        telebot.types.InlineKeyboardButton(text=f"⚙️ Kualitas PDF: [{q_mode}]", callback_data="toggle_quality")
    )
    markup.row(
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
    eksekusi_unduh_pdf(user_id, text_args[1].strip(), is_batch=False)

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

        elif call.data.startswith("exec_nxt_"):
            cache_key = call.data.replace("exec_nxt_", "")
            url_target = next_chapter_cache.get(cache_key)
            if url_target:
                bot.answer_callback_query(call.id, "⚡ Mengunduh Chapter Selanjutnya...", show_alert=False)
                eksekusi_unduh_pdf(user_id, url_target, is_batch=False)
            else:
                bot.answer_callback_query(call.id, "❌ Sesi kedaluwarsa, silakan buka via menu.", show_alert=True)

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
            markup.row(
                telebot.types.InlineKeyboardButton(text="🎯 Pilih dari Tracker/Bacaan", callback_data="batch_from_tracker"),
                telebot.types.InlineKeyboardButton(text="🔗 Paste URL Multiple", callback_data="batch_from_urls")
            )
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Menu Utama", callback_data="go_home"))
            
            pesan = (
                "📦 *MANAJEMEN BATCH DOWNLOAD*\n"
                "───────────────────────────\n"
                "Pilih metode batch download yang ingin kamu gunakan:\n\n"
                "• **Pilih dari Tracker/Bacaan:** Pilih komikmu lalu unduh dengan Preset Dinamis atau Custom Range.\n"
                "• **Paste URL Multiple:** Mengunduh beberapa link URL sekaligus."
            )
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data == "batch_from_tracker":
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
            data = cursor.fetchall()
            cursor.close()
            conn.close()

            if not data:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="btn_batch"))
                edit_dashboard(user_id, msg_id, "❌ *Daftar komik kamu masih kosong.* Tambahkan komik terlebih dahulu.", markup)
                return

            pesan = "📦 *PILIH KOMIK UNTUK BATCH DOWNLOAD:*\n───────────────────────────\n"
            for idx, (db_id, title) in enumerate(data, 1):
                pesan += f" [{idx}]  *{bersihkan_markdown(title)}*\n"

            markup = telebot.types.InlineKeyboardMarkup()
            row_buttons = []
            for idx, (db_id, title) in enumerate(data, 1):
                row_buttons.append(telebot.types.InlineKeyboardButton(text=f" [{idx}] ", callback_data=f"sel_batch_manga_{db_id}"))
                if len(row_buttons) == 5:
                    markup.row(*row_buttons)
                    row_buttons = []
            if row_buttons:
                markup.row(*row_buttons)

            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali", callback_data="btn_batch"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data.startswith("sel_batch_manga_"):
            db_id = int(call.data.split('_')[3])
            user_selected_manga[user_id] = db_id
            bot.answer_callback_query(call.id)

            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT title, last_chapter FROM user_tracks WHERE id = %s AND user_id = %s", (db_id, user_id))
            res = cursor.fetchone()
            cursor.close()
            conn.close()

            manga_title = res[0] if res else "Komik"
            max_ch = extract_latest_ch_number(res[1]) if res else 10

            pesan = (
                f"📦 *BATCH OPTION: {bersihkan_markdown(manga_title)}*\n"
                f"⚡ *Chapter Terbaru Web:* `Chapter {max_ch}`\n"
                f"───────────────────────────\n"
                f"Pilih **Preset Rentang Chapter** atau gunakan **Input Range Manual**:"
            )

            markup = telebot.types.InlineKeyboardMarkup()
            
            # Dinamis membuat preset per 10 chapter (1-10, 11-20, dst)
            temp_row = []
            for i in range(1, max_ch + 1, 10):
                # Jika chapter komik > 60, sembunyikan bagian tengah agar menu Telegram tidak kepanjangan
                if 50 < i < (max_ch - 9):
                    continue
                    
                end_ch = min(i + 9, max_ch)
                btn_label = f"⚡ Ch {i}-{end_ch}"
                temp_row.append(telebot.types.InlineKeyboardButton(text=btn_label, callback_data=f"exec_preset_batch_{db_id}_{i}_{end_ch}"))
                
                if len(temp_row) == 2:
                    markup.row(*temp_row)
                    temp_row = []
            
            if temp_row:
                markup.row(*temp_row)

            markup.row(
                telebot.types.InlineKeyboardButton(text=f"✏️ Input Range Manual (cth: 1-{max_ch})", callback_data=f"btn_custom_range_{db_id}")
            )
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="batch_from_tracker"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data.startswith("exec_preset_batch_"):
            parts = call.data.split('_')
            db_id = int(parts[3])
            start_num = int(parts[4])
            end_num = int(parts[5])

            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM user_tracks WHERE id = %s AND user_id = %s", (db_id, user_id))
            res = cursor.fetchone()
            cursor.close()
            conn.close()

            if res:
                slug = res[0].rstrip('/').split('/')[-1]
                bot.answer_callback_query(call.id, f"🚀 Memulai Batch Download Ch {start_num}-{end_num}...", show_alert=False)
                
                for ch in range(start_num, end_num + 1):
                    url_target = f"https://komiku.org/ch/{slug}-chapter-{ch}/"
                    bot.send_message(user_id, f"📦 *Mengunduh Chapter {ch} dari {end_num}...*", parse_mode="Markdown")
                    # SET is_batch=True agar tombol Lanjut Next Chapter Tidak Muncul
                    eksekusi_unduh_pdf(user_id, url_target, is_batch=True)
                    time.sleep(1)

        elif call.data.startswith("btn_custom_range_"):
            db_id = int(call.data.split('_')[3])
            user_selected_manga[user_id] = db_id
            bot.answer_callback_query(call.id)
            
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="batch_from_tracker"))
            edit_dashboard(user_id, msg_id, "✏️ Kirim **RENTANG CHAPTER** yang ingin kamu unduh (Contoh: `1-5` atau `10-25`):", markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_custom_range_batch)

        elif call.data == "batch_from_urls":
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="btn_batch"))
            pesan = (
                "📦 *PASTE MULTIPLE URL CHAPTER*\n"
                "───────────────────────────\n"
                "Kirimkan **beberapa URL Chapter** sekaligus (satu URL per baris / dipisah spasi):\n\n"
                "*Contoh:*\n"
                "`https://komiku.org/ch/one-piece-chapter-100/`\n"
                "`https://komiku.org/ch/one-piece-chapter-101/`"
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

        elif call.data == "btn_daftar":
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title, last_chapter, url, updated_at FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
            data = cursor.fetchall()
            cursor.close()
            conn.close()

            if not data:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.row(telebot.types.InlineKeyboardButton(text="🔙 Menu Utama", callback_data="go_home"))
                edit_dashboard(user_id, msg_id, "❌ *Kamu belum memantau komik apa pun.*", markup)
                return

            pesan = f"📋 *DAFTAR TRACKER AKTIF ({len(data)} Judul):*\n───────────────────────────\n"
            for idx, (db_id, title, last_ch, url, updated) in enumerate(data, 1):
                clean_t = bersihkan_markdown(title)
                clean_last_ch = bersihkan_markdown(last_ch)
                tgl_update = updated.strftime("%d/%m/%Y") if updated else "-"
                
                pesan += (
                    f"[{idx}] 📖 *{clean_t}*\n"
                    f"     ⚡ Chapter Terbaru Web: `{clean_last_ch}`\n"
                    f"     🗓️ Auto Scan: `{tgl_update}`\n\n"
                )
                
            pesan += f"───────────────────────────\n💡 Pilih nomor komik untuk melihat detail & sinopsis atau aksi lainnya."
            markup = telebot.types.InlineKeyboardMarkup()
            row_buttons = []
            
            # 🔧 FIX DI SINI: Unpack 5 data (db_id, title, last_ch, url, updated)
            for idx, (db_id, title, last_ch, url, updated) in enumerate(data, 1):
                row_buttons.append(telebot.types.InlineKeyboardButton(text=f" [{idx}] ", callback_data=f"sel_detail_manga_{db_id}"))
                if len(row_buttons) == 5:
                    markup.row(*row_buttons)
                    row_buttons = []
            if row_buttons:
                markup.row(*row_buttons)

            markup.row(
                telebot.types.InlineKeyboardButton(text="⚡ Download Chapter Terbaru", callback_data="manage_dl_latest"),
                telebot.types.InlineKeyboardButton(text="🗑️ Hapus Tracker", callback_data="manage_del")
            )
            markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data.startswith("sel_detail_manga_"):
            db_id = int(call.data.split('_')[3])
            bot.answer_callback_query(call.id, "⏳ Memuat ringkasan & sinopsis komik...", show_alert=False)

            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT title, url, last_read, last_chapter FROM user_tracks WHERE id = %s AND user_id = %s", (db_id, user_id))
            res = cursor.fetchone()
            cursor.close()
            conn.close()

            if not res:
                bot.send_message(user_id, "❌ Komik tidak ditemukan.")
                return

            m_title, m_url, m_last_read, m_last_ch = res
            detail = ekstrak_detail_komik(m_url)

            status = detail['status'] if detail else "Ongoing"
            genre = detail['genre'] if detail else "Tidak Diketahui"
            sinopsis = detail['sinopsis'] if detail else "Sinopsis tidak tersedia."
            web_latest = detail['latest_ch'] if detail else m_last_ch

            pesan = (
                f"ℹ️ *RINGKASAN INFO & SINOPSIS KOMIK*\n"
                f"───────────────────────────\n"
                f"📖 *Judul:* *{bersihkan_markdown(m_title)}*\n"
                f"📌 *Status Web:* `{status}`\n"
                f"🏷️ *Genre:* `{genre}`\n"
                f"⚡ *Chapter Terbaru Web:* `{bersihkan_markdown(web_latest)}`\n"
                f"🔖 *Terakhir Dibaca:* `{bersihkan_markdown(m_last_read)}`\n"
                f"───────────────────────────\n"
                f"📝 *Sinopsis:* \n_{bersihkan_markdown(sinopsis)}_\n"
                f"───────────────────────────"
            )

            user_selected_manga[user_id] = db_id
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton(text="📥 Pilih Chapter", callback_data=f"sel_manga_ch_{db_id}"),
                telebot.types.InlineKeyboardButton(text="📦 Batch Download", callback_data=f"sel_batch_manga_{db_id}")
            )
            markup.row(
                telebot.types.InlineKeyboardButton(text="⚡ Download Terbaru", callback_data=f"exec_dl_lat_{db_id}"),
                telebot.types.InlineKeyboardButton(text="✏️ Set Progress", callback_data=f"set_rd_{db_id}")
            )
            markup.row(
                telebot.types.InlineKeyboardButton(text="🗑️ Hapus Tracker", callback_data=f"exec_del_{db_id}"),
                telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Daftar", callback_data="btn_daftar")
            )
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data == "btn_bacaan":
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title, last_read, last_chapter FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
            data = cursor.fetchall()
            cursor.close()
            conn.close()

            if not data:
                markup = telebot.types.InlineKeyboardMarkup()
                markup.row(telebot.types.InlineKeyboardButton(text="🔙 Menu Utama", callback_data="go_home"))
                edit_dashboard(user_id, msg_id, "❌ *Daftar bacaan kamu masih kosong.*", markup)
                return

            pesan = f"📚 *DAFTAR BACAAN & PROGRESS KAMU ({len(data)} Judul):*\n───────────────────────────\n"
            for idx, (db_id, title, last_rd, last_ch) in enumerate(data, 1):
                clean_t = bersihkan_markdown(title)
                clean_last_rd = bersihkan_markdown(last_rd)
                clean_last_ch = bersihkan_markdown(last_ch)
                
                pesan += (
                    f"{idx}. 📖 *{clean_t}*\n"
                    f"     📌 Terakhir Dibaca: `{clean_last_rd}`\n"
                    f"     ⚡ Status Web Saat Ini: `{clean_last_ch}`\n\n"
                )
                
            pesan += f"───────────────────────────\n💡 Pilih menu di bawah untuk unduh chapter, batch, atau set progress."
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton(text="📥 Download Chapter", callback_data="manage_dl_chapter"),
                telebot.types.InlineKeyboardButton(text="📦 Batch Download", callback_data="batch_from_tracker")
            )
            markup.row(
                telebot.types.InlineKeyboardButton(text="✏️ Set Manual Progress", callback_data="manage_read_progress"),
                telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home")
            )
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data == "manage_dl_chapter":
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
            data = cursor.fetchall()
            cursor.close()
            conn.close()

            pesan = "📥 *PILIH KOMIK YANG INGIN DIUNDUH CHAPTER-NYA:*\n───────────────────────────\n"
            for idx, (db_id, title) in enumerate(data, 1):
                pesan += f" [{idx}]  *{bersihkan_markdown(title)}*\n"

            markup = telebot.types.InlineKeyboardMarkup()
            row_buttons = []
            for idx, (db_id, title) in enumerate(data, 1):
                row_buttons.append(telebot.types.InlineKeyboardButton(text=f" [{idx}] ", callback_data=f"sel_manga_ch_{db_id}"))
                if len(row_buttons) == 5:
                    markup.row(*row_buttons)
                    row_buttons = []
            if row_buttons:
                markup.row(*row_buttons)

            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Daftar Bacaan", callback_data="btn_bacaan"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data.startswith("sel_manga_ch_"):
            db_id = int(call.data.split('_')[3])
            bot.answer_callback_query(call.id, "⏳ Mengambil daftar chapter dari web...", show_alert=False)

            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT title, url FROM user_tracks WHERE id = %s AND user_id = %s", (db_id, user_id))
            res = cursor.fetchone()
            cursor.close()
            conn.close()

            if not res:
                bot.send_message(user_id, "❌ Data komik tidak ditemukan.")
                return

            manga_title, manga_url = res
            chapters = ekstrak_daftar_chapter(manga_url)
            if not chapters:
                bot.send_message(user_id, "❌ Gagal memuat daftar chapter komik ini.")
                return

            user_chapter_storage[user_id] = chapters
            user_selected_manga[user_id] = db_id

            pesan = (
                f"📖 *PILIH CHAPTER KOMIK:*\n"
                f"📌 *{bersihkan_markdown(manga_title)}*\n"
                f"───────────────────────────\n"
                f"Klik tombol chapter di bawah ini atau gunakan tombol **Input Nomor Chapter**:"
            )

            markup = telebot.types.InlineKeyboardMarkup()
            row_buttons = []
            for idx, ch in enumerate(chapters):
                label_ch = ekstrak_nomor_chapter_singkat(ch['title'])
                row_buttons.append(telebot.types.InlineKeyboardButton(text=label_ch, callback_data=f"exec_dl_ch_{idx}"))
                if len(row_buttons) == 3:
                    markup.row(*row_buttons)
                    row_buttons = []
            if row_buttons:
                markup.row(*row_buttons)

            markup.row(
                telebot.types.InlineKeyboardButton(text="✏️ Input Nomor Chapter", callback_data="btn_input_custom_ch"),
                telebot.types.InlineKeyboardButton(text="⏮️ Chapter 1", callback_data=f"exec_dl_ch1_{db_id}")
            )
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Pilih Komik", callback_data="manage_dl_chapter"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data == "btn_input_custom_ch":
            bot.answer_callback_query(call.id)
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="manage_dl_chapter"))
            edit_dashboard(user_id, msg_id, "✏️ **Ketik NOMOR CHAPTER** yang ingin kamu unduh:\n\n*Contoh:* `1`, `12`, `45`, atau `10.5`", markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_input_nomor_chapter)

        elif call.data.startswith("exec_dl_ch1_"):
            db_id = int(call.data.split('_')[3])
            bot.answer_callback_query(call.id, "⚡ Mengunduh Chapter 1...", show_alert=False)
            
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM user_tracks WHERE id = %s AND user_id = %s", (db_id, user_id))
            res = cursor.fetchone()
            cursor.close()
            conn.close()

            if res:
                slug = res[0].rstrip('/').split('/')[-1]
                url_ch1 = f"https://komiku.org/ch/{slug}-chapter-1/"
                eksekusi_unduh_pdf(user_id, url_ch1, status_msg_id=msg_id, is_batch=False)

        elif call.data.startswith("exec_dl_ch_"):
            ch_idx = int(call.data.split('_')[3])
            bot.answer_callback_query(call.id, "⚡ Memproses pengunduhan PDF...", show_alert=False)

            if user_id in user_chapter_storage and ch_idx < len(user_chapter_storage[user_id]):
                target_ch = user_chapter_storage[user_id][ch_idx]
                eksekusi_unduh_pdf(user_id, target_ch['url'], status_msg_id=msg_id, is_batch=False)
            else:
                bot.send_message(user_id, "❌ Sesi pilihan chapter kedaluwarsa, silakan pilih ulang.")

        elif call.data == "manage_read_progress":
            bot.answer_callback_query(call.id)
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("SELECT id, title FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
            data = cursor.fetchall()
            cursor.close()
            conn.close()

            pesan = "✏️ *PILIH KOMIK UNTUK MEMPERBARUI PROGRESS BACA:*\n───────────────────────────\n"
            for idx, (db_id, title) in enumerate(data, 1):
                pesan += f" [{idx}]  *{bersihkan_markdown(title)}*\n"

            markup = telebot.types.InlineKeyboardMarkup()
            row_buttons = []
            for idx, (db_id, title) in enumerate(data, 1):
                row_buttons.append(telebot.types.InlineKeyboardButton(text=f" [{idx}] ", callback_data=f"set_rd_{db_id}"))
                if len(row_buttons) == 5:
                    markup.row(*row_buttons)
                    row_buttons = []
            if row_buttons:
                markup.row(*row_buttons)

            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Daftar Bacaan", callback_data="btn_bacaan"))
            edit_dashboard(user_id, msg_id, pesan, markup)

        elif call.data.startswith("set_rd_"):
            db_id = int(call.data.split('_')[2])
            user_selected_manga[user_id] = db_id
            bot.answer_callback_query(call.id)
            
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batal", callback_data="btn_bacaan"))
            edit_dashboard(user_id, msg_id, "✏️ Silakan **ketik chapter terakhir yang kamu baca** (Contoh: `Chapter 150` atau `Bab 12`):", markup)
            bot.register_next_step_handler_by_chat_id(user_id, tangkap_manual_progress_input)

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

# =========================================================================
# 📥 NEXT STEP HANDLERS
# =========================================================================

def tangkap_custom_range_batch(message):
    user_id = message.chat.id
    input_text = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)
    db_id = user_selected_manga.get(user_id)
    
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not db_id:
        bot.send_message(user_id, "❌ Sesi telah kedaluwarsa.")
        return

    start_num, end_num = parse_range_input(input_text)
    if not start_num or not end_num:
        bot.send_message(user_id, "❌ Format rentang salah. Gunakan format seperti `1-5` atau `10-44`.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM user_tracks WHERE id = %s AND user_id = %s", (db_id, user_id))
    res = cursor.fetchone()
    cursor.close()
    conn.close()

    if not res:
        bot.send_message(user_id, "❌ Komik tidak ditemukan.")
        return

    slug = res[0].rstrip('/').split('/')[-1]
    bot.send_message(user_id, f"🚀 *Memulai Batch Download Chapter {start_num} sampai {end_num}...*", parse_mode="Markdown")

    for ch in range(start_num, end_num + 1):
        url_target = f"https://komiku.org/ch/{slug}-chapter-{ch}/"
        bot.send_message(user_id, f"📦 *Processing Chapter ({ch}/{end_num})...*", parse_mode="Markdown")
        # SET is_batch=True agar tombol Lanjut Next Chapter Tidak Muncul
        eksekusi_unduh_pdf(user_id, url_target, is_batch=True)
        time.sleep(1)

def tangkap_input_nomor_chapter(message):
    user_id = message.chat.id
    ch_num = message.text.strip().lower().replace('chapter', '').replace('ch', '').strip()
    msg_dashboard_id = user_main_message.get(user_id)
    db_id = user_selected_manga.get(user_id)
    
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not db_id:
        bot.send_message(user_id, "❌ Sesi telah kedaluwarsa.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT title, url FROM user_tracks WHERE id = %s AND user_id = %s", (db_id, user_id))
    res = cursor.fetchone()
    cursor.close()
    conn.close()

    if not res:
        bot.send_message(user_id, "❌ Komik tidak ditemukan di database.")
        return

    manga_title, manga_url = res
    slug = manga_url.rstrip('/').split('/')[-1]
    url_target = f"https://komiku.org/ch/{slug}-chapter-{ch_num}/"
    eksekusi_unduh_pdf(user_id, url_target, status_msg_id=msg_dashboard_id, is_batch=False)

def tangkap_manual_progress_input(message):
    user_id = message.chat.id
    input_text = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)
    db_id = user_selected_manga.get(user_id)
    
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not db_id:
        bot.send_message(user_id, "❌ Sesi telah kedaluwarsa.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("UPDATE user_tracks SET last_read = %s WHERE id = %s AND user_id = %s RETURNING title", (input_text, db_id, user_id))
    res = cursor.fetchone()
    conn.commit()
    cursor.close()
    conn.close()

    manga_title = res[0] if res else "Komik"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(telebot.types.InlineKeyboardButton(text="📚 Lihat Daftar Bacaan", callback_data="btn_bacaan"))
    markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))
    
    edit_dashboard(user_id, msg_dashboard_id, f"✅ *PROGRESS BACA DISIMPANKAN!*\n\n📖 Komik: *{bersihkan_markdown(manga_title)}*\n📌 Terakhir Dibaca: `{bersihkan_markdown(input_text)}`", markup)

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
        # SET is_batch=True agar tombol Lanjut Next Chapter Tidak Muncul
        eksekusi_unduh_pdf(user_id, url, is_batch=True)
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

    eksekusi_unduh_pdf(user_id, url_input, status_msg_id=msg_dashboard_id, is_batch=False)

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
        markup.row(telebot.types.InlineKeyboardButton(text="📋 Lihat Daftar Tracker", callback_data="btn_daftar"))
        markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))
        edit_dashboard(user_id, msg_dashboard_id, f"✅ *TRACKER AKTIF!*\n\n📖 Komik: *{bersihkan_markdown(title_slug)}*\n⚡ Posisi Web: `{bersihkan_markdown(chapter)}`", markup)
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
                            
                            # 💡 SIKLUS PENGIRIMAN DENGAN FALLBACK
                            terkirim = False
                            if img_web:
                                try:
                                    # Unduh gambar via cloudscraper (Bypass Hotlink Protection)
                                    img_resp = scraper.get(img_web, timeout=10)
                                    if img_resp.status_code == 200:
                                        bot.send_photo(user_id, img_resp.content, caption=pesan_notif, parse_mode="Markdown", reply_markup=markup)
                                        terkirim = True
                                except Exception as img_err:
                                    print(f"Gambar gagal dimuat, beralih ke pesan teks: {img_err}")
                            
                            # Jika gambar gagal/tidak ada, kirim pesan teks biasa agar notifikasi tetap sampai
                            if not terkirim:
                                try:
                                    bot.send_message(user_id, pesan_notif, parse_mode="Markdown", reply_markup=markup)
                                except Exception as msg_err:
                                    print(f"Gagal kirim pesan notif: {msg_err}")
                                
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
