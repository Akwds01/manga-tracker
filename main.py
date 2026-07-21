import os
import io
import time
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper
import telebot
from threading import Thread
from PIL import Image

# 1. Konfigurasi Token & Admin dari Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = int(os.environ.get("TELEGRAM_CHAT_ID")) if os.environ.get("TELEGRAM_CHAT_ID") else 0

# URL Banner Dashboard Terpusat
BANNER_MENU_URL = "https://images.unsplash.com/photo-1578632767115-351597cf2477?w=1000&q=80"

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
user_main_message = {} # Menyimpan ID pesan banner utama tiap user

# =========================================================================
# 🗄️ DATABASE & HELPER SCRAPING / PDF CONVERTER
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
            url TEXT,
            UNIQUE(user_id, url)
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()

def ekstrak_data_komik(html_text):
    """Mengekstrak chapter terbaru, thumbnail, dan link chapter terbaru dari halaman utama komik"""
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
    """Mengambil semua URL gambar halaman dari satu halaman chapter komik"""
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

def buat_pdf_dari_gambar(image_urls, referer_url=None):
    """Mengunduh gambar-gambar halaman dengan Header Referer & konversi aman ke PDF"""
    scraper = cloudscraper.create_scraper()
    
    # Headers khusus untuk menembus Anti-Hotlink CDN Komiku
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer_url if referer_url else "https://komiku.org/"
    }
    
    pil_images = []
    
    for url in image_urls:
        try:
            resp = scraper.get(url, headers=headers, timeout=12)
            if resp.status_code == 200:
                img = Image.open(io.BytesIO(resp.content))
                # Konversi RGBA/PNG ke RGB standar agar aman saat disimpan ke PDF
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                else:
                    img = img.convert("RGB")
                pil_images.append(img)
            else:
                print(f"Gagal akses gambar ({resp.status_code}): {url}")
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

def eksekusi_unduh_pdf(chat_id, url_chapter, status_msg_id=None):
    """Fungsi pembantu untuk memproses pengunduhan dan pengiriman PDF"""
    if status_msg_id:
        bot.edit_message_caption(
            chat_id=chat_id, 
            message_id=status_msg_id, 
            caption="⏳ *Sedang mengekstrak gambar halaman komik...*", 
            parse_mode="Markdown", 
            reply_markup=None
        )
    else:
        status_msg = bot.send_message(chat_id, "⏳ *Sedang mengekstrak gambar halaman komik...*", parse_mode="Markdown")
        status_msg_id = status_msg.message_id

    # 1. Ambil list gambar
    image_urls = ekstrak_gambar_chapter(url_chapter)
    if not image_urls:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))
        bot.send_message(chat_id, "❌ Gagal menemukan gambar di link tersebut. Pastikan tautan adalah halaman baca chapter.", reply_markup=markup)
        return

    if status_msg_id and chat_id in user_main_message and user_main_message[chat_id] == status_msg_id:
        bot.edit_message_caption(
            chat_id=chat_id, 
            message_id=status_msg_id, 
            caption=f"📥 *Mengunduh {len(image_urls)} halaman & menyusun PDF...*", 
            parse_mode="Markdown"
        )
    else:
        bot.edit_message_text(f"📥 *Mengunduh {len(image_urls)} halaman & menyusun PDF...*", chat_id=chat_id, message_id=status_msg_id, parse_mode="Markdown")

    # 2. Konversi ke PDF (Menggunakan Referer Header)
    pdf_file = buat_pdf_dari_gambar(image_urls, referer_url=url_chapter)
    if not pdf_file:
        bot.send_message(chat_id, "❌ Gagal mengonversi gambar halaman menjadi PDF. Silakan coba link chapter lain.")
        return

    # 3. Kirim File PDF
    clean_name = url_chapter.rstrip('/').split('/')[-1]
    judul_file = f"{clean_name}.pdf" if clean_name else "komik_chapter.pdf"
    
    try:
        bot.send_document(
            chat_id=chat_id,
            document=(judul_file, pdf_file),
            caption=f"✅ *Download PDF Selesai!*\n📖 `{judul_file}`",
            parse_mode="Markdown"
        )
        # Kembalikan tampilan dashboard jika tadi diedit
        if chat_id in user_main_message and user_main_message[chat_id] == status_msg_id:
            pesan = dapatkan_text_utama(bot.get_chat(chat_id).first_name or "User")
            bot.edit_message_caption(chat_id=chat_id, message_id=status_msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup_utama(chat_id))
        else:
            bot.delete_message(chat_id, status_msg_id)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Gagal mengirim file PDF: {e}")

# =========================================================================
# 🎛️ TAMPILAN DASHBOARD INLINE
# =========================================================================

def dapatkan_text_utama(nama_user):
    return (
        f"👑 *WILA STORE | MANGA TRACKER & DOWNLOADER* 👑\n"
        f"───────────────────────────\n"
        f"Halo *{nama_user}*! 👋\n\n"
        f"Selamat datang di sistem manajemen tracker & downloader otomatis.\n"
        f"Bot ini siaga memantau update komik favoritmu 24/7.\n\n"
        f"⚡ *Status Layanan:* `ONLINE (Lancar) ✅`\n"
        f"📌 *Tracker:* Input URL Manual Komik\n"
        f"📥 *Download PDF:* Via tombol menu / Notifikasi / Perintah `/dl`\n"
        f"───────────────────────────\n"
        f"Silakan gunakan menu interaktif di bawah ini:"
    )

def markup_utama(user_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(text="➕ Tambah Tracker", callback_data="btn_tambah"),
        telebot.types.InlineKeyboardButton(text="📋 Daftar Tracker", callback_data="btn_daftar")
    )
    markup.row(
        telebot.types.InlineKeyboardButton(text="📥 Download PDF Chapter", callback_data="btn_download")
    )
    if user_id == ADMIN_ID:
        markup.row(telebot.types.InlineKeyboardButton(text="⚙️ Menu Panel Admin", callback_data="btn_admin"))
    return markup

# =========================================================================
# 🤖 HANDLERS ROUTING UTAMA
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
        bot.send_message(
            user_id, 
            "⚠️ *Format Perintah Salah!*\n\n"
            "Gunakan format:\n`/dl <URL_CHAPTER_KOMIK>`\n\n"
            "*Contoh:*\n`/dl https://komiku.org/ch/one-piece-chapter-1100/`", 
            parse_mode="Markdown"
        )
        return

    url_chapter = text_args[1].strip()
    eksekusi_unduh_pdf(user_id, url_chapter)

@bot.callback_query_handler(func=lambda call: True)
def callback_router(call):
    user_id = call.message.chat.id
    msg_id = call.message.message_id
    user_main_message[user_id] = msg_id

    if call.data == "go_home":
        bot.answer_callback_query(call.id, "Kembali")
        pesan = dapatkan_text_utama(call.from_user.first_name)
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup_utama(user_id))

    elif call.data == "btn_tambah":
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan & Kembali", callback_data="go_home"))
        bot.edit_message_caption(
            chat_id=user_id, 
            message_id=msg_id, 
            caption="🔗 Silakan **kirim URL/Link profil utama komik** secara langsung dari web Komiku:", 
            parse_mode="Markdown", 
            reply_markup=markup
        )
        bot.register_next_step_handler_by_chat_id(user_id, tangkap_url_manual)

    elif call.data == "btn_download":
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan & Kembali", callback_data="go_home"))
        bot.edit_message_caption(
            chat_id=user_id, 
            message_id=msg_id, 
            caption="📥 Silakan **kirim URL/Link Chapter Komik** yang ingin diunduh jadi PDF:\n\n*Contoh:*\n`https://komiku.org/ch/one-piece-chapter-1100/`", 
            parse_mode="Markdown", 
            reply_markup=markup
        )
        bot.register_next_step_handler_by_chat_id(user_id, tangkap_url_download_menu)

    elif call.data.startswith("dln_"):
        # Download PDF langsung dari Tombol Notifikasi
        db_id = int(call.data.split('_')[1])
        bot.answer_callback_query(call.id, "⚡ Memulai pengunduhan PDF chapter terbaru...", show_alert=False)
        
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
                bot.send_message(user_id, f"❌ Error saat memuat data: {e}")

    elif call.data == "btn_daftar":
        bot.answer_callback_query(call.id)
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, last_chapter, url FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
        data = cursor.fetchall()
        cursor.close()
        conn.close()

        if not data:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Menu Utama", callback_data="go_home"))
            bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="❌ *Kamu belum memantau komik apa pun saat ini.*", parse_mode="Markdown", reply_markup=markup)
            return

        pesan = f"📋 *Daftar Tracker Aktif Kamu ({len(data)} Judul):*\n" \
                f"───────────────────────────\n"
        
        for idx, (db_id, title, last_chapter, url) in enumerate(data, 1):
            pesan += f"{idx}. 📖 [{title}]({url})\n     ✨ Posisi: `{last_chapter}`\n\n"
            
        pesan += f"───────────────────────────\n💡 Klik nama judul untuk membaca. Gunakan menu di bawah untuk mengelola database."
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton(text="🗑️ Manajemen Hapus", callback_data="manage_del"),
            telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home")
        )
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup)

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

        pesan = "🗑️ *MANAJEMEN PENGHAPUSAN TRACKER*\n" \
                f"───────────────────────────\n"
        for idx, (db_id, title) in enumerate(data, 1):
            pesan += f" [{idx}]  *{title}*\n"
        pesan += f"───────────────────────────\n🎯 Silakan klik **Angka Nomor Urut** komik di bawah ini untuk menghapusnya dari pemantauan:"

        markup = telebot.types.InlineKeyboardMarkup()
        
        row_buttons = []
        for idx, (db_id, title) in enumerate(data, 1):
            row_buttons.append(telebot.types.InlineKeyboardButton(text=f" [{idx}] ", callback_data=f"exec_del_{db_id}"))
            if len(row_buttons) == 5:
                markup.row(*row_buttons)
                row_buttons = []
        if row_buttons:
            markup.row(*row_buttons)

        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Daftar", callback_data="btn_daftar"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup)

    elif call.data.startswith("exec_del_"):
        db_id = int(call.data.split('_')[2])
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_tracks WHERE id = %s AND user_id = %s RETURNING title", (db_id, user_id))
        deleted = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        
        nama_del = deleted[0] if deleted else "Komik"
        bot.answer_callback_query(call.id, f"Sukses Menghapus {nama_del}!", show_alert=False)
        
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
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="🛠️ *Selamat Datang di Panel Owner Bot WILA STORE:*", parse_mode="Markdown", reply_markup=markup)

    elif call.data == "admin_stats" and user_id == ADMIN_ID:
        bot.answer_callback_query(call.id)
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT user_id), COUNT(*) FROM user_tracks")
        users, total_tracks = cursor.fetchone()
        cursor.execute("SELECT title, COUNT(*) as c FROM user_tracks GROUP BY title ORDER BY c DESC LIMIT 3")
        populer = cursor.fetchall()
        cursor.close()
        conn.close()

        txt_populer = ""
        for i, (t, c) in enumerate(populer, 1):
            txt_populer += f"   {i}. {t} ({c} User)\n"

        pesan = (
            f"📊 *STATISTIK BOT REAL-TIME*\n"
            f"───────────────────────────\n"
            f"👥 Total Pengguna Unik: `{users}` Orang\n"
            f"📌 Total Judul Di-track: `{total_tracks}` Item\n\n"
            f"🔥 *Top 3 Komik Terpopuler:*\n{txt_populer}"
            f"───────────────────────────"
        )
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Panel", callback_data="btn_admin"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup)

    elif call.data == "admin_bc" and user_id == ADMIN_ID:
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan", callback_data="btn_admin"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="📢 Silakan **ketik dan kirim pesan massal** yang ingin disiarkan ke seluruh user:", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(user_id, tangkap_pesan_broadcast)

# =========================================================================
# 📥 PROGRAM TANGKAP INPUT USER
# =========================================================================

def tangkap_url_download_menu(message):
    user_id = message.chat.id
    url_input = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)

    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not url_input.startswith("http"):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔄 Input Ulang URL", callback_data="btn_download"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption="❌ *Format link salah!* URL harus diawali dengan http:// atau https://", parse_mode="Markdown", reply_markup=markup)
        return

    eksekusi_unduh_pdf(user_id, url_input, status_msg_id=msg_dashboard_id)

def tangkap_url_manual(message):
    user_id = message.chat.id
    url_input = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)

    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not url_input.startswith("http"):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔄 Input Ulang URL", callback_data="btn_tambah"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption="❌ *Format link salah!* URL harus diawali dengan http:// atau https://", parse_mode="Markdown", reply_markup=markup)
        return

    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption="⏳ Sedang memverifikasi link komik...", parse_mode="Markdown", reply_markup=None)
    
    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_input, timeout=12)
        if res.status_code != 200:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔄 Coba Lagi", callback_data="btn_tambah"))
            bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"❌ Gagal koneksi! Server status: {res.status_code}", parse_mode="Markdown", reply_markup=markup)
            return

        chapter, img, _ = ekstrak_data_komik(res.text)
        if not chapter:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali", callback_data="go_home"))
            bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption="❌ Gagal mengekstrak chapter. Pastikan itu adalah Link Profil Utama komik.", parse_mode="Markdown", reply_markup=markup)
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
        markup.row(telebot.types.InlineKeyboardButton(text="📋 Tampilkan Daftar Tracker", callback_data="btn_daftar"))
        markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))

        pesan = f"✅ *PROSES INPUT MANUAL SUKSES!*\n\n📖 Komik: *{title_slug}*\n⚡ Posisi: `{chapter}`"
        bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=pesan, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        print(f"Error koneksi manual: {e}")

def tangkap_pesan_broadcast(message):
    user_id = message.chat.id
    pesan_bc = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)

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
            bot.send_message(u_id, f"📢 *PEMBERITAHUAN DEVELOPER WILA STORE:*\n\n{pesan_bc}", parse_mode="Markdown")
            sukses += 1
            time.sleep(0.05)
        except:
            pass
            
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))
    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"✅ *Broadcast Berhasil!* Pesan sukses disiarkan ke `{sukses}` pengguna.", parse_mode="Markdown", reply_markup=markup)

# =========================================================================
# 🕵️ WORKER SCRAPER LATAR BELAKANG
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
                            cursor.execute(
                                "UPDATE user_tracks SET last_chapter = %s WHERE user_id = %s AND url = %s",
                                (chapter_web, user_id, url)
                            )
                            conn.commit()
                        
                        elif chapter_web != last_chapter:
                            pesan_notif = (
                                f"🔥 *UPDATE MANGA HYPE RELEASE!* 🔥\n"
                                f"───────────────────────────\n"
                                f"📖 Judul: *{title}*\n"
                                f"✨ Rilis Baru: *{chapter_web}*\n"
                                f"📥 Status DB: (Lama: `{last_chapter}`)\n"
                                f"───────────────────────────\n"
                                f"🚀 Silakan pilih opsi di bawah ini:"
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
                                
                            cursor.execute(
                                "UPDATE user_tracks SET last_chapter = %s WHERE user_id = %s AND url = %s",
                                (chapter_web, user_id, url)
                            )
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
    
    print("Bot Premium Single Message Dashboard Aktif Sempurna...")
    bot.infinity_polling()
