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

# Inisialisasi Bot Telegram
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Kamus sementara untuk menyimpan status input user (State Management)
user_states = {}

def init_db():
    """Membuat tabel database multi-user jika belum ada"""
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

# =========================================================================
# 🎛️ BAGIAN TOMBOL DAN MENU TELEGRAM (MENU BUTTONS)
# =========================================================================

def tombol_utama():
    """Membuat menu tombol di bawah keyboard Telegram"""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("➕ Tambah Komik", "📋 Daftar Komik")
    return markup

@bot.message_handler(commands=['start', 'help'])
def kirim_welcome(message):
    """Menangani perintah /start"""
    nama_user = message.from_user.first_name
    pesan = (
        f"Halo *{nama_user}*! 👋\n\n"
        "Selamat datang di Bot Tracker Komik otomatis. "
        "Di sini kamu bisa mendaftarkan komik favoritmu dari web *Komiku* dan bot akan memberikan notifikasi otomatis tiap kali ada chapter baru rilis!\n\n"
        "Silakan gunakan tombol di bawah untuk memulai."
    )
    bot.send_message(message.chat.id, pesan, parse_mode="Markdown", reply_markup=tombol_utama())

@bot.message_handler(func=lambda message: message.text == "📋 Daftar Komik")
def lihat_daftar_komik(message):
    """Menampilkan daftar komik yang di-add oleh user tersebut"""
    user_id = message.chat.id
    
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT title, last_chapter, url FROM user_tracks WHERE user_id = %s", (user_id,))
    daftar = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not daftar:
        bot.send_message(user_id, "❌ Kamu belum menambahkan komik apa pun. Klik tombol *➕ Tambah Komik* untuk memulai!", parse_mode="Markdown")
        return
        
    pesan = "📋 *Daftar Komik Tracker Kamu:*\n\n"
    for i, (title, last_chapter, url) in enumerate(daftar, 1):
        pesan += f"{i}. *{title}*\n✨ Posisi: `{last_chapter}`\n🔗 [Link Baca]({url})\n\n"
        
    bot.send_message(user_id, pesan, parse_mode="Markdown", disable_web_page_preview=True)

@bot.message_handler(func=lambda message: message.text == "➕ Tambah Komik")
def mulai_tambah_komik(message):
    """Langkah 1 Tambah Komik: Meminta Judul"""
    user_id = message.chat.id
    user_states[user_id] = {'step': 'menunggu_judul'}
    
    # Tombol batal jika user ingin membatalkan input
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("❌ Batalkan")
    
    bot.send_message(user_id, "Silakan ketik atau kirim *Judul Komik* yang ingin kamu pantau:", parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('step') == 'menunggu_judul')
def proses_judul_komik(message):
    """Langkah 2 Tambah Komik: Menyimpan Judul & Meminta URL"""
    user_id = message.chat.id
    teks = message.text.strip()
    
    if teks == "❌ Batalkan":
        user_states.pop(user_id, None)
        bot.send_message(user_id, "Proses penambahan dibatalkan.", reply_markup=tombol_utama())
        return
        
    user_states[user_id] = {'step': 'menunggu_url', 'title': teks}
    bot.send_message(user_id, f"Judul diterima: *{teks}*\n\nSekarang, kirim *URL/Link halaman utama komik tersebut* dari website Komiku (Contoh: https://komiku.org/manga/judul-komik/):", parse_mode="Markdown")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get('step') == 'menunggu_url')
def proses_url_komik(message):
    """Langkah 3 Tambah Komik: Menyimpan ke Database"""
    user_id = message.chat.id
    url_input = message.text.strip()
    
    if url_input == "❌ Batalkan":
        user_states.pop(user_id, None)
        bot.send_message(user_id, "Proses penambahan dibatalkan.", reply_markup=tombol_utama())
        return
        
    if not url_input.startswith("http"):
        bot.send_message(user_id, "❌ URL tidak valid! Pastikan diawali dengan `http://` atau `https://`. Silakan kirim ulang URL-nya:")
        return
        
    data_user = user_states.get(user_id, {})
    title = data_user.get('title')
    
    # Simpan ke Database
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_tracks (user_id, title, last_chapter, url)
            VALUES (%s, %s, '0', %s)
            ON CONFLICT (user_id, url) DO NOTHING;
        """, (user_id, title, url_input))
        conn.commit()
        cursor.close()
        conn.close()
        
        bot.send_message(user_id, f"✅ Sukses menambahkan tracker untuk komik: *{title}*!\nBot akan mendeteksi data awal dalam beberapa saat.", parse_mode="Markdown", reply_markup=tombol_utama())
    except Exception as e:
        bot.send_message(user_id, f"❌ Terjadi kesalahan saat menyimpan ke database: {e}", reply_markup=tombol_utama())
        
    # Hapus state jika sudah selesai
    user_states.pop(user_id, None)


# =========================================================================
# 🕵️ REFAKTORISASI JALUR SCRAPER LATAR BELAKANG (BACKGROUND THREAD)
# =========================================================================

def cek_update_multiuser():
    """Mengecek seluruh URL unik di DB agar hemat bandwidth server"""
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    # Ambil semua URL unik yang didaftarkan oleh siapapun
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
                soup = BeautifulSoup(respon.text, 'html.parser')
                chapter_terbaru = None
                
                # Selektor khusus web Komiku
                container = soup.find(id='Daftar_Chapter') or soup.find(id='daftar_chapter')
                if container:
                    first_a = container.find('a')
                    if first_a:
                        chapter_terbaru = " ".join(first_a.text.strip().split())
                
                if not chapter_terbaru:
                    chapter_element = soup.find('span', class_='chapnum')
                    if chapter_element:
                        chapter_terbaru = " ".join(chapter_element.text.strip().split())

                if chapter_terbaru:
                    # Ambil semua user yang memantau URL ini
                    cursor.execute("SELECT user_id, title, last_chapter FROM user_tracks WHERE url = %s", (url,))
                    users = cursor.fetchall()
                    
                    for user_id, title, last_chapter in users:
                        # Jika baru inisialisasi awal (DB masih '0')
                        if last_chapter == '0':
                            cursor.execute(
                                "UPDATE user_tracks SET last_chapter = %s WHERE user_id = %s AND url = %s",
                                (chapter_terbaru, user_id, url)
                            )
                            conn.commit()
                            print(f"[Init] {title} user {user_id} -> {chapter_terbaru}")
                        
                        # Jika ada rilis chapter baru sungguhan
                        elif chapter_terbaru != last_chapter:
                            pesan = (
                                f"🔥 *UPDATE MANGA BARU!* 🔥\n\n"
                                f"📖 *{title}*\n"
                                f"✨ Sekarang sudah rilis *{chapter_terbaru}*\n\n"
                                f"🔗 [Klik untuk Membaca]({url})"
                            )
                            # Kirim langsung ke user_id yang bersangkutan
                            try:
                                bot.send_message(user_id, pesan, parse_mode="Markdown")
                            except Exception as e:
                                print(f"Gagal mengirim notif ke {user_id}: {e}")
                                
                            cursor.execute(
                                "UPDATE user_tracks SET last_chapter = %s WHERE user_id = %s AND url = %s",
                                (chapter_terbaru, user_id, url)
                            )
                            conn.commit()
                            print(f"[Notif Sent] {title} to user {user_id}")
        except Exception as e:
            print(f"Error scraping {url}: {e}")

    cursor.close()
    conn.close()

def loop_scraper():
    """Fungsi loop 15 menit yang akan berjalan di thread terpisah"""
    init_db()
    while True:
        print("--- Memulai Pengecekan Rutin (Multi-User) ---")
        try:
            cek_update_multiuser()
        except Exception as e:
            print(f"Error pada loop scraper: {e}")
        print("--- Pengecekan Selesai, Istirahat 15 Menit ---")
        time.sleep(900)

# =========================================================================
# 🚀 MENYALAKAN KEDUA SISTEM SECARA BERSAMAAN
# =========================================================================
if __name__ == "__main__":
    print("Mengaktifkan database...")
    init_db()
    
    # Jalankan loop scraper di latar belakang (Thread 1)
    thread_Scrap = Thread(target=loop_scraper)
    thread_Scrap.daemon = True
    thread_Scrap.start()
    
    # Jalankan bot listener untuk dengerin tombol chat di thread utama (Thread 2)
    print("Bot Telegram Interaktif Siap Melayani Pengguna...")
    bot.infinity_polling()
