import os
import time
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper
import telebot
from threading import Thread

# 1. Konfigurasi Token & Admin dari Heroku Config Vars
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = int(os.environ.get("TELEGRAM_CHAT_ID")) if os.environ.get("TELEGRAM_CHAT_ID") else 0

# URL Banner Premium Terpusat
BANNER_MENU_URL = "https://images.unsplash.com/photo-1578632767115-351597cf2477?w=1000&q=80"

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
user_main_message = {} # Menyimpan ID pesan banner utama tiap user
search_storage = {} 

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
    soup = BeautifulSoup(html_text, 'html.parser')
    chapter_terbaru = None
    image_url = None
    
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        image_url = meta_img['content']

    container = soup.find(id='Daftar_Chapter') or soup.find(id='daftar_chapter')
    if container and container.find('a'):
        chapter_terbaru = " ".join(container.find('a').text.strip().split())
            
    if not chapter_terbaru:
        container_ms = soup.find(id='chapterlist') or soup.find(class_='cl')
        if container_ms and container_ms.find('a'):
            chapter_terbaru = " ".join(container_ms.find('a').text.strip().split())

    return chapter_terbaru, image_url

def cari_komik_komiku(keyword):
    """Fungsi pencarian multi-layer menggunakan parameter sakti post_type=manga"""
    scraper = cloudscraper.create_scraper()
    url = f"https://komiku.org/?post_type=manga&s={requests.utils.quote(keyword)}"
    results = []
    status_debug = "SUCCESS"
    
    try:
        respon = scraper.get(url, timeout=10)
        if respon.status_code != 200:
            return [], f"WEB_ERROR_{respon.status_code}"
            
        soup = BeautifulSoup(respon.text, 'html.parser')
        
        for a_tag in soup.find_all('a'):
            href = a_tag.get('href', '')
            if "/manga/" in href:
                if any(x in href for x in ["/genre/", "/category/", "/page/", "/ch/", "/chapter/", "?s="]): 
                    continue
                
                title = a_tag.text.strip()
                
                if not title:
                    parent = a_tag.find_parent(['div', 'article', 'li', 'td'])
                    if parent:
                        h_tag = parent.find(['h3', 'h4', 'h2', 'b', 'span'])
                        if h_tag:
                            title = h_tag.text.strip()
                
                title = " ".join(title.split())
                
                if not title or title.lower() in ["manga", "manhwa", "manhua", "home", "next", "prev", "daftar komik", "kembali", "baca"]:
                    continue
                    
                if href.startswith("/"):
                    href = f"https://komiku.org{href}"
                    
                if not any(r['url'] == href for r in results):
                    results.append({'title': title, 'url': href})
                    
        if not results:
            status_debug = "HTML_EMPTY"
            
    except Exception as e:
        status_debug = f"EXCEPTION_{str(e)}"
        
    return results[:5], status_debug

# =========================================================================
# 🎛️ CODES INTERFACE TAMPILAN INLINE (DASHBOARD APP STYLE)
# =========================================================================

def dapatkan_text_utama(nama_user):
    return (
        f"👑 *WILA STORE | MANGA TRACKER PREMIUM* 👑\n"
        f"───────────────────────────\n"
        f"Halo *{nama_user}*! 👋\n\n"
        f"Selamat datang di sistem manajemen tracker otomatis. Bot ini akan "
        f"berdiri siaga memantau update komik favoritmu 24/7 tanpa henti.\n\n"
        f"⚡ *Status Layanan:* `ONLINE (Lancar) ✅`\n"
        f"💡 *Fitur:* Pencarian Judul Otomatis & Input URL Manual\n"
        f"───────────────────────────\n"
        f"Silakan gunakan menu interaktif di bawah ini:"
    )

def markup_utama(user_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(text="➕ Tambah Tracker", callback_data="btn_tambah"),
        telebot.types.InlineKeyboardButton(text="📋 Daftar Tracker", callback_data="btn_daftar")
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
        markup.row(
            telebot.types.InlineKeyboardButton(text="🔍 Cari Otomatis", callback_data="add_auto"),
            telebot.types.InlineKeyboardButton(text="🔗 Input URL Manual", callback_data="add_manual")
        )
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Menu Utama", callback_data="go_home"))
        
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="⚡ *Silakan Pilih Metode Penambahan Tracker:*", parse_mode="Markdown", reply_markup=markup)

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
        pesan += f"───────────────────────────\n🎯 Silakan klik **Angka Nomor Urut** komik di bawah ini untuk menghapusnya dari radar pemantauan:"

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

    elif call.data == "add_auto":
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan & Kembali", callback_data="btn_tambah"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="🔍 Silakan **ketik kata kunci / judul komik** yang ingin kamu cari langsung di room chat ini:", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(user_id, tangkap_keyword_pencarian)

    elif call.data == "add_manual":
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan & Kembali", callback_data="btn_tambah"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="🔗 Silakan **kirim URL/Link utama komik** secara langsung dari web target:", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(user_id, tangkap_url_manual)

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
# 📥 PROGRAM TANGKAP INPUT USER & PENGHAPUSAN CHAT OTOMATIS
# =========================================================================

def tangkap_keyword_pencarian(message):
    user_id = message.chat.id
    keyword = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)

    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not msg_dashboard_id:
        return

    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"⏳ Sedang memproses kata kunci *'{keyword}'* via database post_type=manga...", parse_mode="Markdown", reply_markup=None)
    
    hasil, debug_status = cari_komik_komiku(keyword)
    markup = telebot.types.InlineKeyboardMarkup()
    
    if not hasil:
        markup.row(telebot.types.InlineKeyboardButton(text="🔄 Coba Cari Lagi", callback_data="add_auto"))
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali", callback_data="btn_tambah"))
        
        detail_eror = "Komik tidak ditemukan." if debug_status == "HTML_EMPTY" else f"Kendala Jaringan ({debug_status})"
        bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"❌ *Pencarian Gagal!*\n\nKata kunci *'{keyword}'* nihil hasil.\nStatus: `{detail_eror}`\n\nSilakan coba judul lain atau gunakan Input URL Manual.", parse_mode="Markdown", reply_markup=markup)
        return

    search_storage[user_id] = hasil
    for idx, item in enumerate(hasil):
        markup.add(telebot.types.InlineKeyboardButton(text=item['title'], callback_data=f"save_search_{idx}"))
    markup.add(telebot.types.InlineKeyboardButton(text="🔙 Batalkan", callback_data="btn_tambah"))

    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"🎯 *Hasil Pencarian Teratas untuk '{keyword}':*\n\nKlik salah satu tombol judul di bawah untuk langsung mengunci radar pemantauan otomatis:", parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("save_search_"))
def simpan_pencarian_otomatis(call):
    user_id = call.message.chat.id
    msg_id = call.message.message_id
    idx = int(call.data.split('_')[2])

    if user_id not in search_storage or idx >= len(search_storage[user_id]):
        bot.answer_callback_query(call.id, "Sesi kedaluwarsa, silakan cari ulang.")
        return

    target = search_storage[user_id][idx]
    title = target['title']
    url_input = target['url']

    bot.answer_callback_query(call.id, "Sedang mengunci tracker...")
    scraper = cloudscraper.create_scraper()
    
    try:
        res = scraper.get(url_input, timeout=12)
        chapter, img = ekstrak_data_komik(res.text)
        
        if not chapter:
            bot.send_message(user_id, "❌ Gagal memproses data struktur web tersebut.")
            return

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_tracks (user_id, title, last_chapter, url)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, url) DO UPDATE SET last_chapter = EXCLUDED.last_chapter;
        """, (user_id, title, chapter, url_input))
        conn.commit()
        cursor.close()
        conn.close()

        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="📋 Lihat Daftar Tracker", callback_data="btn_daftar"))
        markup.row(telebot.types.InlineKeyboardButton(text="🏠 Menu Utama", callback_data="go_home"))

        pesan = f"✅ *MANGA TRACKER PREMIUM AKTIF!*\n\n📖 Judul: *{title}*\n⚡ Chapter Saat Ini: `{chapter}`\n\nSistem berhasil mengunci data awal komik."
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup)
            
    except Exception as e:
        bot.send_message(user_id, f"❌ Gagal memproses penyimpanan: {e}")

def tangkap_url_manual(message):
    user_id = message.chat.id
    url_input = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)

    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not url_input.startswith("http"):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔄 Input Ulang URL", callback_data="add_manual"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption="❌ *Format link salah!* URL harus diawali dengan http:// atau https://", parse_mode="Markdown", reply_markup=markup)
        return

    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption="⏳ Sedang menembak dan memverifikasi tautan link manual...", parse_mode="Markdown", reply_markup=None)
    
    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_input, timeout=12)
        if res.status_code != 200:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔄 Coba Lagi", callback_data="add_manual"))
            bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"❌ Gagal koneksi! Server mengembalikan Status: {res.status_code}", parse_mode="Markdown", reply_markup=markup)
            return

        chapter, img = ekstrak_data_komik(res.text)
        if not chapter:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali", callback_data="btn_tambah"))
            bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption="❌ Gagal mengekstrak nomor bab. Pastikan itu adalah Link Profil Utama komik.", parse_mode="Markdown", reply_markup=markup)
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
        print(f"Eror koneksi manual: {e}")

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
    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"✅ *Broadcast Berhasil!* Pesan sukses disiarkan ke `{sukses}` pengguna aktif.", parse_mode="Markdown", reply_markup=markup)

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
                chapter_web, img_web = ekstrak_data_komik(res.text)

                if chapter_web:
                    cursor.execute("SELECT user_id, title, last_chapter FROM user_tracks WHERE url = %s", (url,))
                    registered_users = cursor.fetchall()
                    
                    for user_id, title, last_chapter in registered_users:
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
                                f"🚀 Klik tombol di bawah untuk membaca langsung!"
                            )
                            
                            markup = telebot.types.InlineKeyboardMarkup()
                            markup.add(telebot.types.InlineKeyboardButton(text="🚀 Baca Sekarang", url=url))
                            
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

if __name__ == "__main__":
    init_db()
    
    worker = Thread(target=loop_background_worker)
    worker.daemon = True
    worker.start()
    
    print("Bot Premium Single Message Dashboard Aktif Sempurna...")
    bot.infinity_polling()
