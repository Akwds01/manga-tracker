import os
import time
import requests
from bs4 import BeautifulSoup
import psycopg2
import cloudscraper
import urllib3

# Mematikan notifikasi peringatan SSL Insecure di log Heroku
urllib3.disable_warnings()

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
    # DAFTAR KOMIK TARGET (Menggunakan Komikcast yang lebih aman dari Cloudflare)
    # =========================================================================
    target_komik = [
        (
            'Became The Patron Of Villains', 
            '0', 
            'https://komikcast.bz/komik/became-the-patron-of-villains/'
        ),
        (
            'Job Change Log', 
            '0', 
            'https://komikcast.bz/komik/the-job-change-log/'
        ),
        (
            'One Piece',
            '0',
            'https://komikcast.bz/komik/one-piece/'
        )
    ]
    
    for title, last_chapter, url in target_komik:
        cursor.execute("""
            INSERT INTO manga_tracks (title, last_chapter, url)
            VALUES (%s, %s, %s)
            ON CONFLICT (title) DO NOTHING;
        """, (title, last_chapter, url))
        
    conn.commit()
    cursor.close()
    conn.close()

def cek_update():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    cursor.execute("SELECT title, last_chapter, url FROM manga_tracks")
    daftar_manga = cursor.fetchall()

    # Bikin objek scraper penembus proteksi
    scraper = cloudscraper.create_scraper()

    for title, last_chapter, url in daftar_manga:
        try:
            # verify=False digunakan untuk melewati error SSL Certificate
            respon = scraper.get(url, timeout=15, verify=False)
            
            if respon.status_code == 200:
                soup = BeautifulSoup(respon.text, 'html.parser')
                
                # Selektor khusus Komikcast (MangaStream Theme menggunakan tag 'chapnum')
                chapter_element = soup.find('span', class_='chapnum')
                
                if chapter_element:
                    chapter_terbaru = chapter_element.text.strip()
                    print(f"[{title}] DB: {last_chapter} | Web: {chapter_terbaru}")
                    
                    if last_chapter == '0':
                        cursor.execute(
                            "UPDATE manga_tracks SET last_chapter = %s WHERE title = %s",
                            (chapter_terbaru, title)
                        )
                        conn.commit()
                        print(f"-> Menginisialisasi chapter awal {title} ke {chapter_terbaru}")
                    
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
