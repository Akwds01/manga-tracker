import os
import time
import requests
from bs4 import BeautifulSoup
import psycopg2

# Ambil konfigurasi dari Heroku Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Perbaikan minor untuk library psycopg2 jika URL dari Heroku pakai 'postgres://'
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def kirim_telegram(pesan):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": pesan, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def init_db():
    # Membuat tabel di database Heroku jika belum ada
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS manga_tracks (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) UNIQUE,
            last_chapter VARCHAR(50),
            url TEXT
        )
    """)
    # Contoh data awal (Dummy): Ganti URL & Judul sesuai situs yang mau kamu scrape
    cursor.execute("""
        INSERT INTO manga_tracks (title, last_chapter, url)
        VALUES ('Solo Leveling Sequel', '0', 'https://example-manga-site.com/solo-leveling')
        ON CONFLICT (title) DO NOTHING;
    """)
    conn.commit()
    cursor.close()
    conn.close()

def cek_update():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT title, last_chapter, url FROM manga_tracks")
    daftar_manga = cursor.fetchall()

    for title, last_chapter, url in daftar_manga:
        try:
            # Proses Scraping (Ini contoh basic, selector disesuaikan dengan situs target)
            respon = requests.get(url)
            soup = BeautifulSoup(respon.text, 'html.parser')
            
            # MISALNYA: Elemen chapter terbaru ada di tag <span class="chap-num">Chapter 15</span>
            # Kamu perlu sesuaikan bagian soup.find() ini nanti dengan struktur website target
            chapter_terbaru_element = soup.find('span', class_='chap-num') 
            
            if chapter_terbaru_element:
                chapter_terbaru = chapter_terbaru_element.text.strip() # Hasil: "Chapter 15"
                
                # Jika ada chapter baru yang tidak sama dengan di database
                if chapter_terbaru != last_chapter:
                    pesan = f"🔥 *UPDATE BARU!* 🔥\n\n📖 *{title}*\n✨ Sekarang sudah rilis *{chapter_terbaru}*!\n🔗 [Baca Sekarang]({url})"
                    kirim_telegram(pesan)
                    
                    # Update data chapter terbaru di database
                    cursor.execute(
                        "UPDATE manga_tracks SET last_chapter = %s WHERE title = %s",
                        (chapter_terbaru, title)
                    )
                    conn.commit()
        except Exception as e:
            print(f"Gagal mengecek {title}: {e}")

    cursor.close()
    conn.close()

if __name__ == "__main__":
    print("Bot Tracker Mulai Berjalan...")
    init_db()
    while True:
        print("Mengecek update...")
        cek_update()
        time.sleep(900) # Jeda waktu 900 detik (15 Menit) sebelum cek lagi
