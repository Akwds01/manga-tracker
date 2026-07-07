import os
import time
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper
import telebot

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
    scraper = cloudscraper.create_scraper()
    url = f"https://komiku.org/?s={requests.utils.quote(keyword)}"
    results = []
    try:
        respon = scraper.get(url, timeout=10)
        if respon.status_code == 200:
            soup = BeautifulSoup(respon.text, 'html.parser')
            
            # Cari seluruh elemen anchor link manga asli
            for a_tag in soup.find_all('a'):
                href = a_tag.get('href', '')
                # Ambil teks h3 di dalam a jika ada, jika tidak pakai teks a langsung
                h3 = a_tag.find('h3')
                title = h3.text.strip() if h3 else a_tag.text.strip()
                
                if "/manga/" in href and title and len(title) > 3:
                    # Filter mutlak membuang link genre/kategori sampah bawaan Komiku
                    if any(x in href for x in ["/genre/", "/category/", "/page/", "?s=", "/ch/"]):
                        continue
                    if any(y in title.lower() for y in ["manga", "manhwa", "manhua", "home", "daftar", "next", "prev"]):
                        continue
                    if href.startswith("/"):
                        href = f"https://komiku.org{href}"
                    if not any(r['url'] == href for r in results):
                        results.append({'title': title, 'url': href})
    except Exception as e:
        print(f"Gagal melakukan pencarian: {e}")
    return results[:5]

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
    
    # Hapus keyboard reply lama jika masih tertinggal di layar bawaan user
    hapus_reply = telebot.types.ReplyKeyboardRemove()
    msg_info = bot.send_message(user_id, "⚡ Menginisialisasi Dashboard...", reply_markup=hapus_reply)
    bot.delete_message(user_id, msg_info.message_id)

    # Kirim Banner Utama Terpusat
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
    user_main_message[user_id] = msg_id # Kunci ID pesan agar tidak berubah

    # 🔙 KEMBALI KE MENU UTAMA
    if call.data == "go_home":
        bot.answer_callback_query(call.id, "Kembali ke Menu Utama")
        pesan = dapatkan_text_utama(call.from_user.first_name)
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup_utama(user_id))

    # ➕ MENU TAMBAH TRACKER
    elif call.data == "btn_tambah":
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton(text="🔍 Cari Otomatis", callback_data="add_auto"),
            telebot.types.InlineKeyboardButton(text="🔗 Input URL Manual", callback_data="add_manual")
        )
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Menu Utama", callback_data="go_home"))
        
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="⚡ *Silakan Pilih Metode Penambahan Tracker:*", parse_mode="Markdown", reply_markup=markup)

    # 📋 MENU DAFTAR TRACKER (POINT 2 RESOLVED)
    elif call.data == "btn_daftar":
        bot.answer_callback_query(call.id)
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, last_chapter FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
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
        
        markup = telebot.types.InlineKeyboardMarkup()
        for db_id, title, last_chapter in data:
            pesan += f"🔸 *{title}* (Posisi: `{last_chapter}`)\n"
            # Sediakan tombol hapus langsung di bawah daftar
            markup.add(telebot.types.InlineKeyboardButton(text=f"❌ Hapus {title[:20]}", callback_data=f"del_{db_id}"))
            
        pesan += f"───────────────────────────\n💡 Klik hapus untuk mengeluarkan judul dari radar."
        markup.add(telebot.types.InlineKeyboardButton(text="🔙 Kembali ke Menu Utama", callback_data="go_home"))
        
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption=pesan, parse_mode="Markdown", reply_markup=markup)

    # ❌ EKSEKUSI PROSES HAPUS TRACKER
    elif call.data.startswith("del_"):
        db_id = int(call.data.split('_')[1])
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_tracks WHERE id = %s AND user_id = %s RETURNING title", (db_id, user_id))
        deleted = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        
        nama_del = deleted[0] if deleted else "Komik"
        bot.answer_callback_query(call.id, f"Sukses menghapus {nama_del}!")
        
        # Alihkan kembali memuat daftar terbaru setelah menghapus
        bot.execute_handler(bot.callback_query_handlers[0], call) 
        # Update call data simulasi klik daftar ulang
        call.data = "btn_daftar"
        callback_router(call)

    # 🔍 METODE CARI OTOMATIS SELECTED
    elif call.data == "add_auto":
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan & Kembali", callback_data="btn_tambah"))
        
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="🔍 Silakan **ketik kata kunci / judul komik** yang ingin kamu cari langsung di room chat ini:", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(user_id, tangkap_keyword_pencarian)

    # 🔗 METODE INPUT MANUAL SELECTED
    elif call.data == "add_manual":
        bot.answer_callback_query(call.id)
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Batalkan & Kembali", callback_data="btn_tambah"))
        
        bot.edit_message_caption(chat_id=user_id, message_id=msg_id, caption="🔗 Silakan **kirim URL/Link utama komik** secara langsung dari web target:", parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(user_id, tangkap_url_manual)

# =========================================================================
# 📥 PROGRAM TANGKAP INPUT USER & PENGHAPUSAN CHAT OTOMATIS (POINT 4)
# =========================================================================

def tangkap_keyword_pencarian(message):
    user_id = message.chat.id
    keyword = message.text.strip()
    msg_dashboard_id = user_main_message.get(user_id)

    # Hapus pesan yang diketik user biar room chat bersih tanpa sampah chat baru
    try: bot.delete_message(user_id, message.message_id)
    except: pass

    if not msg_dashboard_id:
        bot.send_message(user_id, "Sesi terputus, ketik /start kembali.")
        return

    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"⏳ Sedang mencari hasil kata kunci *'{keyword}'* di Komiku...", parse_mode="Markdown", reply_markup=None)
    
    hasil = cari_komik_komiku(keyword)
    markup = telebot.types.InlineKeyboardMarkup()
    
    if not hasil:
        markup.row(telebot.types.InlineKeyboardButton(text="🔄 Coba Cari Lagi", callback_data="add_auto"))
        markup.row(telebot.types.InlineKeyboardButton(text="🔙 Kembali", callback_data="btn_tambah"))
        bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"❌ *Komik tidak ditemukan!* Pencarian kata kunci *'{keyword}'* nihil hasil. Gunakan judul spesifik lain.", parse_mode="Markdown", reply_markup=markup)
        return

    search_storage[user_id] = hasil
    for idx, item in enumerate(hasil):
        markup.add(telebot.types.InlineKeyboardButton(text=item['title'], callback_data=f"save_search_{idx}"))
    markup.add(telebot.types.InlineKeyboardButton(text="🔙 Batalkan", callback_data="btn_tambah"))

    bot.edit_message_caption(chat_id=user_id, message_id=msg_dashboard_id, caption=f"🎯 *Hasil Pencarian Teratas untuk '{keyword}':*\nKlik salah satu judul tombol di bawah untuk langsung mengaktifkan pemantauan:", parse_mode="Markdown", reply_markup=markup)

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

        # Ambil nama judul kasar dari potongan slug URL aman
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
        bot.send_message(user_id, f"❌ Kendala koneksi manual: {e}")

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
