import os
import time
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper
import telebot
from threading import Thread

# 1. Konfigurasi Token & DB dari Heroku Config Vars
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
user_states = {}

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

def ekstrak_chapter(html_text):
    """Fungsi pembantu untuk mendeteksi chapter dari berbagai jenis struktur web"""
    soup = BeautifulSoup(html_text, 'html.parser')
    
    # Kategori 1: Struktur Web Komiku (id='Daftar_Chapter')
    container = soup.find(id='Daftar_Chapter') or soup.find(id='daftar_chapter')
    if container:
        first_a = container.find('a')
        if first_a:
            return " ".join(first_a.text.strip().split())
            
    # Kategori 2: Halaman detail bertema MangaStream/Madara (Komikcast, Bacakomik, Komikindo)
    container_ms = soup.find(id='chapterlist') or soup.find(class_='cl') or soup.find(class_='clstyle')
    if container_ms:
        first_a = container_ms.find('a')
        if first_a:
            return " ".join(first_a.text.strip().split())

    # Kategori 3: Pencarian langsung lewat class chapnum (Backup global)
    chapter_element = soup.find('span', class_='chapnum') or soup.find(class_='chapnum')
    if chapter_element:
        return " ".join(chapter_element.text.strip().split())
        
    return None

# =========================================================================
# 🎛️ USER INTERFACE (TELEGRAM BUTTONS & COMMANDS)
# =========================================================================

def tombol_utama():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("➕ Tambah Komik", "📋 Daftar Komik")
    return markup

@bot.message_handler(commands=['start', 'help'])
def kirim_welcome(message):
    nama_user = message.from_user.first_name
    pesan = (
        f"Halo *{nama_user}*! 👋\n\n"
        "Selamat datang di Bot Tracker Komik otomatis.\n"
        "Bot mendukung link dari *Komiku, Komikcast, Bacakomik, Komikindo*, dll.\n\n"
        "Silakan gunakan tombol di bawah untuk memulai."
    )
    bot.send_message(message.chat.id, pesan, parse_mode="Markdown", reply_markup=tombol_utama())

@bot.message_handler(func=lambda message: message.text == "📋 Daftar Komik")
def lihat_daftar_komik(message):
    user_id = message.chat.id
    
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT title, last_chapter, url FROM user_tracks WHERE user_id = %s", (user_id,))
    daftar = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not daftar:
        bot.send_message(user_id, "❌ Kamu belum menambahkan komik apa pun. Klik *➕ Tambah Komik* untuk memulai!", parse_mode="Markdown")
        return
        
    pesan = "📋 *Daftar Komik Tracker Kamu:*\n\n"
    for i, (title, last_chapter, url) in enumerate(daftar, 1):
        pesan += f"{i}. *{title}*\n✨ Posisi: `{last_chapter}`\n🔗 [Link Baca]({url})\n\n"
        
    bot.send_message(user_id, pesan, parse_mode="Markdown", disable_web_page_preview=True)

@bot.message_handler(func=lambda message: message.text == "➕ Tambah Komik")
def mulai_tambah_komik(message):
    user_id = message.chat.id
    user_states[user_id] = {'step': 'menunggu_judul'}
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("❌ Batalkan")
    
    bot.send_message(user_id, "Silakan ketik atau kirim *Judul Komik* yang ingin kamu pantau:", parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('step') == 'menunggu_judul')
def proses_judul_komik(message):
    user_id = message.chat.id
    teks = message.text.strip()
    
    if teks == "❌ Batalkan":
        user_states.pop(user_id, None)
        bot.send_message(user_id, "Proses penambahan dibatalkan.", reply_markup=tombol_utama())
        return
        
    user_states[user_id] = {'step': 'menunggu_url', 'title': teks}
    bot.send_message(user_id, f"Judul diterima: *{teks}*\n\nSekarang, kirim *URL/Link utama komik tersebut* (Contoh: https://komiku.org/manga/judul-komik/):", parse_mode="Markdown")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('step') == 'menunggu_url')
def proses_url_komik(message):
    user_id = message.chat.id
    url_input = message.text.strip()
    
    if url_input == "❌ Batalkan":
        user_states.pop(user_id, None)
        bot.send_message(user_id, "Proses penambahan dibatalkan.", reply_markup=tombol_utama())
        return
        
    if not url_input.startswith("http"):
        bot.send_message(user_id, "❌ URL tidak valid! Harus diawali dengan `http://` atau `https://`. Kirim ulang URL-nya:")
        return
        
    data_user = user_states.get(user_id, {})
    title = data_user.get('title')
    
    # Send loading status ke user
    status_msg = bot.send_message(user_id, "⏳ Sedang memverifikasi web target, mohon tunggu...")
    
    # =========================================================================
    # 🛡️ PROSES VALIDASI INSTAN (REAL-TIME CHECK)
    # =========================================================================
    scraper = cloudscraper.create_scraper()
    try:
        respon = scraper.get(url_input, timeout=12)
        
        if respon.status_code == 403:
            bot.delete_message(user_id, status_msg.message_id)
            bot.send_message(user_id, "❌ *Gagal menambahkan!* Website memblokir server Heroku (Eror 403 / Cloudflare). Silakan gunakan link dari web alternatif lain.", parse_mode="Markdown", reply_markup=tombol_utama())
            user_states.pop(user_id, None)
            return
            
        if respon.status_code != 200:
            bot.delete_message(user_id, status_msg.message_id)
            bot.send_message(user_id, f"❌ *Gagal mengakses web!* Server mengembalikan Status Code: {respon.status_code}.", parse_mode="Markdown", reply_markup=tombol_utama())
            user_states.pop(user_id, None)
            return
            
        # Jika berhasil tembus (Status 200), tes ekstraksi data chapter
        chapter_terbaru = ekstrak_chapter(respon.text)
        
        if not chapter_terbaru:
            bot.delete_message(user_id, status_msg.message_id)
            bot.send_message(user_id, "❌ *Gagal mendeteksi data!* Link berhasil dibuka, namun bot tidak menemukan struktur daftar chapter. Pastikan yang kamu kirim adalah link *Halaman Utama* komiknya, bukan halaman sewaktu membaca chapternya.", parse_mode="Markdown", reply_markup=tombol_utama())
            user_states.pop(user_id, None)
            return
            
        # Lolos semua validasi -> Simpan ke Database bersama Nilai Chapter Asli (Bukan 0 lagi)
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_tracks (user_id, title, last_chapter, url)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, url) DO UPDATE SET last_chapter = EXCLUDED.last_chapter;
        """, (user_id, title, chapter_terbaru, url_input))
        conn.commit()
        cursor.close()
        conn.close()
        
        bot.delete_message(user_id, status_msg.message_id)
        bot.send_message(user_id, f"✅ *Sukses Menambahkan!*\n\n📖 Komik: *{title}*\n✨ Chapter Saat Ini: `{chapter_terbaru}`\n\nBot akan otomatis mengabari kamu jika ada update baru!", parse_mode="Markdown", reply_markup=tombol_utama())
        
    except Exception as e:
        bot.delete_message(user_id, status_msg.message_id)
        bot.send_message(user_id, f"❌ Terjadi gangguan jaringan saat memverifikasi link: {e}", reply_markup=tombol_utama())
        
    user_states.pop(user_id, None)


# =========================================================================
# 🕵️ REFAKTORISASI JALUR SCRAPER LATAR BELAKANG (BACKGROUND THREAD)
# =========================================================================

def cek_update_multiuser():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT url FROM user_tracks")
    daftar_url = [r[0] for r in cursor.fetchall()]
    
    if not daftar_url:
        cursor.close()
        conn.close()
        return

    scraper = cloudscraper.create_scraper()

    for url in daftar_url:
        try:
            respon = scraper.get(url, timeout=15)
            if respon.status_code == 200:
                chapter_terbaru = ekstrak_chapter(respon.text)

                if chapter_terbaru:
                    cursor.execute("SELECT user_id, title, last_chapter FROM user_tracks WHERE url = %s", (url,))
                    users = cursor.fetchall()
                    
                    for user_id, title, last_chapter in users:
                        if last_chapter == '0':
                            cursor.execute(
                                "UPDATE user_tracks SET last_chapter = %s WHERE user_id = %s AND url = %s",
                                (chapter_terbaru, user_id, url)
                            )
                            conn.commit()
                        
                        elif chapter_terbaru != last_chapter:
                            pesan = (
                                f"🔥 *UPDATE MANGA BARU!* 🔥\n\n"
                                f"📖 *{title}*\n"
                                f"✨ Sekarang sudah rilis *{chapter_terbaru}*\n\n"
                                f"🔗 [Klik untuk Membaca]({url})"
                            )
                            try:
                                bot.send_message(user_id, pesan, parse_mode="Markdown")
                            except Exception as e:
                                print(f"Gagal kirim pesan ke {user_id}: {e}")
                                
                            cursor.execute(
                                "UPDATE user_tracks SET last_chapter = %s WHERE user_id = %s AND url = %s",
                                (chapter_terbaru, user_id, url)
                            )
                            conn.commit()
                            print(f"[Notif] {title} -> {chapter_terbaru} (User: {user_id})")
        except Exception as e:
            print(f"Error background scraping {url}: {e}")

    cursor.close()
    conn.close()

def loop_scraper():
    init_db()
    while True:
        print("--- Memulai Pengecekan Rutin (Multi-User) ---")
        try:
            cek_update_multiuser()
        except Exception as e:
            print(f"Error pada loop scraper: {e}")
        print("--- Pengecekan Selesai, Istirahat 15 Menit ---")
        time.sleep(900)

if __name__ == "__main__":
    init_db()
    
    thread_Scrap = Thread(target=loop_scraper)
    thread_Scrap.daemon = True
    thread_Scrap.start()
    
    print("Bot Telegram Interaktif Siap Melayani Pengguna...")
    bot.infinity_polling()
