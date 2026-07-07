import os
import time
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper

# 1. Konfigurasi Sistem (Diambil dari Heroku Config Vars)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def kirim_telegram(pesan):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": pesan, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Gagal mengirim pesan ke Telegram: {e}")

def init_db():
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
    
    # =========================================================================
    # DAFTAR KOMIK TARGET (Menggunakan URL Komiku Pilihanmu)
    # =========================================================================
    target_komik = [
        (
            'Became The Patron Of Villains', 
            '0', 
            'https://komiku.org/manga/became-the-patron-of-villains/'
        ),
        (
            'Job Change Log', 
            '0', 
            'https://komiku.org/manga/job-change-log/'
        )
    ]
    
    # Bersihkan komik lama yang tidak ada di list aktif dari database
    titles = [t[0] for t in target_komik]
    if titles:
        cursor.execute("DELETE FROM manga_tracks WHERE NOT (title = ANY(%s));", (titles,))
    
    # Sinkronisasi list komik aktif ke database
    for title, last_chapter, url in target_komik:
        cursor.execute("""
            INSERT INTO manga_tracks (title, last_chapter, url)
            VALUES (%s, %s, %s)
            ON CONFLICT (title) DO UPDATE SET url = EXCLUDED.url;
        """, (title, last_chapter, url))
        
    conn.commit()
    cursor.close()
    conn.close()

def cek_update():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    cursor.execute("SELECT title, last_chapter, url FROM manga_tracks")
    daftar_manga = cursor.fetchall()

    scraper = cloudscraper.create_scraper()

    for title, last_chapter, url in daftar_manga:
        try:
            respon = scraper.get(url, timeout=15)
            
            if respon.status_code == 200:
                soup = BeautifulSoup(respon.text, 'html.parser')
                
                # --- STRATEGI PENCARIAN CHAPTER KHUSUS KOMIKU ---
                chapter_terbaru = None
                
                # Komiku menggunakan kontainer dengan ID 'Daftar_Chapter'
                container = soup.find(id='Daftar_Chapter') or soup.find(id='daftar_chapter')
                if container:
                    # Mencari tag link <a> pertama di dalam daftar chapter (terbaru)
                    first_a = container.find('a') 
                    if first_a:
                        # Mengambil teksnya (Misal: "Chapter 12")
                        chapter_terbaru = first_a.text.strip()
                
                # Jika cara utama gagal, gunakan cadangan pencarian tag span
                if not chapter_terbaru:
                    chapter_element = soup.find('span', class_='chapnum')
                    if chapter_element:
                        chapter_terbaru = chapter_element.text.strip()

                # --- PROSES VALIDASI DATA & NOTIFIKASI ---
                if chapter_terbaru:
                    # Membersihkan teks dari spasi berlebih atau baris baru
                    chapter_terbaru = " ".join(chapter_terbaru.split())
                    
                    print(f"[{title}] DB: {last_chapter} | Web: {chapter_terbaru}")
                    
                    # Jika data awal masih '0', lakukan inisialisasi tanpa kirim notif
                    if last_chapter == '0':
                        cursor.execute(
                            "UPDATE manga_tracks SET last_chapter = %s WHERE title = %s",
                            (chapter_terbaru, title)
                        )
                        conn.commit()
                        print(f"-> Menginisialisasi chapter awal {title} ke {chapter_terbaru}")
                    
                    # Jika terdeteksi ada chapter baru sungguhan di web Komiku
                    elif chapter_terbaru != last_chapter:
                        pesan = (
                            f"🔥 *UPDATE MANGA BARU!* 🔥\n\n"
                            f"📖 *{title}*\n"
                            f"✨ Sekarang sudah rilis *{chapter_terbaru}*\n\n"
                            f"🔗 [Klik untuk Membaca]({url})"
                        )
                        kirim_telegram(pesan)
                        
                        cursor.execute(
                            "UPDATE manga_tracks SET last_chapter = %s WHERE title = %s",
                            (chapter_terbaru, title)
                        )
                        conn.commit()
                        print(f"-> Notifikasi dikirim! {title} diperbarui ke {chapter_terbaru}")
                else:
                    print(f"Gagal menemukan elemen chapter untuk: {title}")
            else:
                print(f"Gagal mengakses halaman {title} (Status Code: {respon.status_code})")
                
        except Exception as e:
            print(f"Error saat mengecek {title}: {e}")

    cursor.close()
    conn.close()

if __name__ == "__main__":
    print("Bot Tracker Mulai Berjalan...")
    init_db()
    
    while True:
        print("--- Memulai Pengecekan Rutin ---")
        cek_update()
        print("--- Pengecekan Selesai, Istirahat 15 Menit ---")
        time.sleep(900)
