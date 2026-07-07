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
# Menggunakan ID kamu yang sudah tersimpan di Heroku sebagai Hak Akses Admin
ADMIN_ID = int(os.environ.get("TELEGRAM_CHAT_ID")) if os.environ.get("TELEGRAM_CHAT_ID") else 0

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
user_states = {}
search_storage = {}  # Menyimpan sementara hasil pencarian user

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
    """Mengekstrak data chapter dan URL gambar cover (Universal Scraper)"""
    soup = BeautifulSoup(html_text, 'html.parser')
    chapter_terbaru = None
    image_url = None
    
    # 1. Ambil Gambar Cover via Open Graph Meta Tag (Sangat Akurat)
    meta_img = soup.find('meta', property='og:image')
    if meta_img and meta_img.get('content'):
        image_url = meta_img['content']
    
    # Backup cari gambar konvensional jika meta tag tidak ada
    if not image_url:
        img_container = soup.find('div', class_='ims') or soup.find(class_='thumb')
        if img_container and img_container.find('img'):
            image_url = img_container.find('img').get('src')

    # 2. Ambil Data Chapter (Komiku / MangaStream Theme)
    container = soup.find(id='Daftar_Chapter') or soup.find(id='daftar_chapter')
    if container and container.find('a'):
        chapter_terbaru = " ".join(container.find('a').text.strip().split())
            
    if not chapter_terbaru:
        container_ms = soup.find(id='chapterlist') or soup.find(class_='cl')
        if container_ms and container_ms.find('a'):
            chapter_terbaru = " ".join(container_ms.find('a').text.strip().split())

    if not chapter_terbaru:
        ch_el = soup.find('span', class_='chapnum') or soup.find(class_='chapnum')
        if ch_el:
            chapter_terbaru = " ".join(ch_el.text.strip().split())
        
    return chapter_terbaru, image_url

def cari_komik_komiku(keyword):
    """Fungsi pencarian otomatis langsung membelah web Komiku"""
    scraper = cloudscraper.create_scraper()
    url = f"https://komiku.org/?s={requests.utils.quote(keyword)}"
    results = []
    try:
        respon = scraper.get(url, timeout=10)
        if respon.status_code == 200:
            soup = BeautifulSoup(respon.text, 'html.parser')
            # Membaca kontainer hasil pencarian standar Komiku
            for div in soup.find_all('div', class_='bdr'):
                a_tag = div.find('a')
                h3_tag = div.find('h3')
                if a_tag and h3_tag:
                    title = h3_tag.text.strip()
                    link = a_tag.get('href')
                    if "/manga/" in link:
                        results.append({'title': title, 'url': link})
            
            # Jalur cadangan jika struktur pencarian berbeda
            if not results:
                for a_tag in soup.find_all('a'):
                    href = a_tag.get('href', '')
                    title = a_tag.text.strip()
                    if "/manga/" in href and title and len(title) > 3 and not any(r['url'] == href for r in results):
                        results.append({'title': title, 'url': href})
    except Exception as e:
        print(f"Gagal melakukan pencarian: {e}")
    return results[:5]

# =========================================================================
# 🎛️ NAVIGATION & UI MARKUPS
# =========================================================================

def menu_utama(user_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("➕ Tambah Tracker", "📋 Daftar Tracker Kamu")
    if user_id == ADMIN_ID:
        markup.row("⚙️ Menu Panel Admin")
    return markup

def menu_tambah():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("🔍 Cari Otomatis", "🔗 Input URL Manual")
    markup.row("🔙 Kembali ke Menu Utama")
    return markup

def menu_admin():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📊 Statistik Bot", "📢 Broadcast Global")
    markup.row("🔙 Kembali ke Menu Utama")
    return markup

# =========================================================================
# 🤖 HANDLERS CHAT INTERAKTIF
# =========================================================================

@bot.message_handler(commands=['start', 'help'])
def command_start(message):
    user_id = message.chat.id
    user_states.pop(user_id, None)
    pesan = (
        f"✨ *WILA SHORT MANGA TRACKER* ✨\n"
        f"───────────────────────\n"
        f"Halo *{message.from_user.first_name}*! 👋\n"
        f"Bot ini siap memantau update Manga/Manhwa kesukaanmu "
        f"secara otomatis 24 jam penuh.\n\n"
        f"💡 *Fitur:* Mendukung sistem pencarian judul langsung atau "
        f"input URL manual (*Komiku, Komikcast, Bacakomik*, dll).\n"
        f"───────────────────────\n"
        f"Silakan pilih menu di bawah ini:"
    )
    bot.send_message(user_id, pesan, parse_mode="Markdown", reply_markup=menu_utama(user_id))

@bot.message_handler(func=lambda m: m.text == "🔙 Kembali ke Menu Utama")
def kembali_utama(message):
    user_id = message.chat.id
    user_states.pop(user_id, None)
    bot.send_message(user_id, "🏠 Kembali ke Menu Utama.", reply_markup=menu_utama(user_id))

@bot.message_handler(func=lambda m: m.text == "➕ Tambah Tracker")
def sub_menu_tambah(message):
    bot.send_message(message.chat.id, "⚡ *Pilih Metode Penambahan:*", parse_mode="Markdown", reply_markup=menu_tambah())

# 🔗 PROSES PENAMBAHAN MANUAL (URL)
@bot.message_handler(func=lambda m: m.text == "🔗 Input URL Manual")
def manual_judul(message):
    user_id = message.chat.id
    user_states[user_id] = {'step': 'manual_menunggu_judul'}
    bot.send_message(user_id, "📝 Ketik *Judul Komik* yang ingin kamu pantau:", parse_mode="Markdown", reply_markup=telebot.types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('step') == 'manual_menunggu_judul')
def manual_proses_judul(message):
    user_id = message.chat.id
    judul = message.text.strip()
    user_states[user_id] = {'step': 'manual_menunggu_url', 'title': judul}
    bot.send_message(user_id, f"📌 Judul: *{judul}*\n\nSekarang kirim *URL/Link utama halaman komiknya*:", parse_mode="Markdown")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('step') == 'manual_menunggu_url')
def manual_proses_url(message):
    user_id = message.chat.id
    url_input = message.text.strip()
    title = user_states[message.chat.id].get('title')
    user_states.pop(user_id, None)

    if not url_input.startswith("http"):
        bot.send_message(user_id, "❌ URL salah/tidak valid!", reply_markup=menu_utama(user_id))
        return

    status_msg = bot.send_message(user_id, "⏳ Memverifikasi tautan web target...")
    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_input, timeout=12)
        try: bot.delete_message(user_id, status_msg.message_id)
        except: pass

        if res.status_code != 200:
            bot.send_message(user_id, f"❌ Gagal koneksi! Status Code: {res.status_code}", reply_markup=menu_utama(user_id))
            return

        chapter, img = ekstrak_data_komik(res.text)
        if not chapter:
            bot.send_message(user_id, "❌ Gagal mendeteksi struktur chapter. Pastikan itu link info komik!", reply_markup=menu_utama(user_id))
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

        pesan = f"✅ *BERHASIL DITAMBAHKAN!*\n\n📖 Judul: *{title}*\n⚡ Chapter: `{chapter}`"
        if img:
            bot.send_photo(user_id, img, caption=pesan, parse_mode="Markdown", reply_markup=menu_utama(user_id))
        else:
            bot.send_message(user_id, pesan, parse_mode="Markdown", reply_markup=menu_utama(user_id))
    except Exception as e:
        bot.send_message(user_id, f"❌ Terjadi gangguan: {e}", reply_markup=menu_utama(user_id))

# 🔍 PROSES PENCARIAN OTOMATIS
@bot.message_handler(func=lambda m: m.text == "🔍 Cari Otomatis")
def search_start(message):
    user_id = message.chat.id
    user_states[user_id] = {'step': 'menunggu_keyword'}
    bot.send_message(user_id, "🔍 Masukkan *Kata Kunci / Judul* komik yang ingin dicari:", parse_mode="Markdown", reply_markup=telebot.types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('step') == 'menunggu_keyword')
def search_execute(message):
    user_id = message.chat.id
    keyword = message.text.strip()
    user_states.pop(user_id, None)

    loading = bot.send_message(user_id, "⚡ Sedang mencari di database Komiku...")
    hasil = cari_komik_komiku(keyword)
    try: bot.delete_message(user_id, loading.message_id)
    except: pass

    if not hasil:
        bot.send_message(user_id, "❌ Komik tidak ditemukan. Sila coba kata kunci lain atau gunakan Input URL Manual.", reply_markup=menu_utama(user_id))
        return

    search_storage[user_id] = hasil
    markup = telebot.types.InlineKeyboardMarkup()
    for idx, item in enumerate(hasil):
        markup.add(telebot.types.InlineKeyboardButton(text=item['title'], callback_data=f"addsearch_{idx}"))
    
    bot.send_message(user_id, "🎯 *Hasil Pencarian Teratas:*\nKlik judul di bawah untuk langsung memantau:", parse_mode="Markdown", reply_markup=markup)
    bot.send_message(user_id, "💡 Gunakan tombol menu utama di bawah jika ingin membatalkan:", reply_markup=menu_utama(user_id))

@bot.callback_query_handler(func=lambda call: call.data.startswith('addsearch_'))
def callback_add_search(call):
    user_id = call.message.chat.id
    idx = int(call.data.split('_')[1])
    
    if user_id not in search_storage or idx >= len(search_storage[user_id]):
        bot.answer_callback_query(call.id, "Sesi kedaluwarsa, silakan cari ulang.")
        return

    target = search_storage[user_id][idx]
    title = target['title']
    url_input = target['url']

    bot.answer_callback_query(call.id, f"Memproses {title}...")
    scraper = cloudscraper.create_scraper()
    try:
        res = scraper.get(url_input, timeout=12)
        chapter, img = ekstrak_data_komik(res.text)
        
        if not chapter:
            bot.send_message(user_id, "❌ Gagal memproses data chapter dari hasil pencarian.")
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

        pesan = f"✅ *SUKSES MENGAKTIFKAN TRACKER!*\n\n📖 Komik: *{title}*\n⚡ Posisi Saat Ini: `{chapter}`"
        if img:
            bot.send_photo(user_id, img, caption=pesan, parse_mode="Markdown")
        else:
            bot.send_message(user_id, pesan, parse_mode="Markdown")
            
    except Exception as e:
        bot.send_message(user_id, f"❌ Eror saat menyimpan: {e}")

# 📋 DAFTAR TRACKER & FITUR HAPUS (CRUD COMPLETION)
@bot.message_handler(func=lambda m: m.text == "📋 Daftar Tracker Kamu")
def list_tracker(message):
    user_id = message.chat.id
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, last_chapter, url FROM user_tracks WHERE user_id = %s ORDER BY id DESC", (user_id,))
    data = cursor.fetchall()
    cursor.close()
    conn.close()

    if not data:
        bot.send_message(user_id, "❌ Kamu belum memantau komik apa pun saat ini.", parse_mode="Markdown")
        return

    bot.send_message(user_id, f"📋 *Daftar Tracker Aktif Kamu ({len(data)} Judul):*\n"
                              f"───────────────────────", parse_mode="Markdown")
    
    for db_id, title, last_chapter, url in data:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton(text="📖 Baca", url=url),
            telebot.types.InlineKeyboardButton(text="❌ Hapus", callback_data=f"del_{db_id}")
        )
        bot.send_message(user_id, f"🔸 *{title}*\n⚡ Chapter Terakhir: `{last_chapter}`", parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def callback_delete_tracker(call):
    db_id = int(call.data.split('_')[1])
    user_id = call.message.chat.id

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    # Pastikan data yang dihapus benar milik user yang menekan tombol
    cursor.execute("DELETE FROM user_tracks WHERE id = %s AND user_id = %s RETURNING title", (db_id, user_id))
    deleted = cursor.fetchone()
    conn.commit()
    cursor.close()
    conn.close()

    if deleted:
        bot.answer_callback_query(call.id, f"Sukses menghapus {deleted[0]}!")
        bot.edit_message_text(chat_id=user_id, message_id=call.message.message_id, text=f"🗑️ *{deleted[0]}* telah dihapus dari tracker kamu.", parse_mode="Markdown")
    else:
        bot.answer_callback_query(call.id, "Data tidak ditemukan atau sudah terhapus.")

# =========================================================================
# ⚙️ PANEL KONTROL ADMIN (STATS & BROADCAST)
# =========================================================================

@bot.message_handler(func=lambda m: m.text == "⚙️ Menu Panel Admin" and m.chat.id == ADMIN_ID)
def panel_admin(message):
    bot.send_message(ADMIN_ID, "🛠️ *Selamat Datang di Panel Owner Bot:*", parse_mode="Markdown", reply_markup=menu_admin())

@bot.message_handler(func=lambda m: m.text == "📊 Statistik Bot" and m.chat.id == ADMIN_ID)
def stats_bot(message):
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
        f"───────────────────────\n"
        f"👥 Total Pengguna Unik: `{users}` Orang\n"
        f"📌 Total Judul Di-track: `{total_tracks}` Item\n\n"
        f"🔥 *Top 3 Komik Terpopuler:*\n{txt_populer}"
        f"───────────────────────"
    )
    bot.send_message(ADMIN_ID, pesan, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast Global" and m.chat.id == ADMIN_ID)
def start_broadcast(message):
    user_states[ADMIN_ID] = {'step': 'menunggu_broadcast'}
    bot.send_message(ADMIN_ID, "📢 Ketik *Pesan Massal* yang ingin kamu kirimkan ke seluruh pengguna bot:", parse_mode="Markdown", reply_markup=telebot.types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get('step') == 'menunggu_broadcast' and m.chat.id == ADMIN_ID)
def execute_broadcast(message):
    pesan_bc = message.text.strip()
    user_states.pop(ADMIN_ID, None)

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM user_tracks")
    users = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()

    bot.send_message(ADMIN_ID, f"⚡ Mengirim pesan ke {len(users)} pengguna...", reply_markup=menu_utama(ADMIN_ID))
    
    sukses = 0
    for u_id in users:
        try:
            bot.send_message(u_id, f"📢 *PEMBERITAHUAN DEVELOPER:*\n\n{pesan_bc}", parse_mode="Markdown")
            sukses += 1
            time.sleep(0.05) # Menghindari rate limit bot API
        except:
            pass
            
    bot.send_message(ADMIN_ID, f"✅ Broadcast selesai! Pesan terkirim ke `{sukses}` pengguna.")


# =========================================================================
# 🕵️ JALUR BACKGROUND REFRESH SCRAPER (THREAD LATAR BELAKANG)
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
                            # Tampilan Notifikasi Update dengan Rich Media (Gambar Cover + Link)
                            pesan_notif = (
                                f"🔥 *UPDATE MANGA HYPE RELEASE!* 🔥\n"
                                f"───────────────────────\n"
                                f"📖 Judul: *{title}*\n"
                                f"✨ Rilis Baru: *{chapter_web}*\n"
                                f"📥 Status DB: (Lama: `{last_chapter}`)\n"
                                f"───────────────────────\n"
                                f"🚀 Klik link di bawah untuk membaca langsung!"
                            )
                            
                            markup = telebot.types.InlineKeyboardMarkup()
                            markup.add(telebot.types.InlineKeyboardButton(text="🚀 Baca Sekarang", url=url))
                            
                            try:
                                if img_web:
                                    bot.send_photo(user_id, img_web, caption=pesan_notif, parse_mode="Markdown", reply_markup=markup)
                                else:
                                    bot.send_message(user_id, pesan_notif, parse_mode="Markdown", reply_markup=markup)
                            except Exception as e:
                                print(f"Gagal notif user {user_id}: {e}")
                                
                            cursor.execute(
                                "UPDATE user_tracks SET last_chapter = %s WHERE user_id = %s AND url = %s",
                                (chapter_web, user_id, url)
                            )
                            conn.commit()
                            print(f"[Notif Sukses] {title} -> {chapter_web} untuk User ID {user_id}")
        except Exception as e:
            print(f"Error background loop pada {url}: {e}")

    cursor.close()
    conn.close()

def loop_background_worker():
    init_db()
    while True:
        print("--- Memulai Pengecekan Rutin (Multi-User Master) ---")
        try:
            refresh_loop_multiuser()
        except Exception as e:
            print(f"Gagal loop worker: {e}")
        print("--- Pengecekan Selesai, Istirahat 15 Menit ---")
        time.sleep(900)

if __name__ == "__main__":
    init_db()
    
    # Menjalankan Thread Latar Belakang untuk Scraper Otomatis
    worker = Thread(target=loop_background_worker)
    worker.daemon = True
    worker.start()
    
    # Jalankan Listener Utama Chat Telegram
    print("Bot Ultimate Terpasang Sempurna. Siap Melayani Pengguna Global...")
    bot.infinity_polling()
