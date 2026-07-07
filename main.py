import os
import time
import requests
from bs4 import BeautifulSoup
import psycopg2

# 1. Konfigurasi Sistem (Diambil dari Heroku Config Vars)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Perbaikan otomatis format URL database bawaan Heroku
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def kirim_telegram(pesan):
    """Fungsi untuk mengirim notifikasi teks ke Telegram"""
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
    """Fungsi untuk membuat tabel dan memasukkan komik target pertama kali"""
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    # Membuat tabel jika belum ada
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS manga_tracks (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) UNIQUE,
            last_chapter VARCHAR(50),
            url TEXT
        )
    """)
    
    # =========================================================================
    # SILAKAN GANTI TARGET KOMIK DI SINI (Contoh: Jujutsu Kaisen & One Piece)
    # =========================================================================
    target_komik = [
        (
            'Became The Patron Of Villains', 
            '0', 
            'https://g.shinigami.asia/series/84561956-c987-491d-a189-ba1af3c22810'
        ),
        (
            'Job Change Log', 
            '0', 
            'https://g.shinigami.asia/series/977280a5-eb42-474f-86f3-e63e07e468f6'
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
    """Fungsi inti untuk memantau update chapter komik"""
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    cursor.execute("SELECT title, last_chapter, url FROM manga_tracks")
    daftar_manga = cursor.fetchall()

    # Header wajib agar Heroku tidak diblokir/403 Forbidden oleh website komik
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    for title, last_chapter, url in daftar_manga:
        try:
            # Ambil data HTML dari web komik
            respon = requests.get(url, headers=headers, timeout=15)
            
            if respon.status_code == 200:
                soup = BeautifulSoup(respon.text, 'html.parser')
                
                # Selektor khusus bertema MangaStream (Bacakomik, Komikcast, Komikindo)
                # Mengambil tag <span class="chapnum"> yang muncul pertama kali di halaman (terbaru)
                chapter_element = soup.find('span', class_='chapnum')
                
                if chapter_element:
                    # Hasil pembersihan teks (Contoh: "Chapter 1110" atau "Ch. 370")
                    chapter_terbaru = chapter_element.text.strip()
                    
                    # Log internal untuk memantau status di Heroku Logs
                    print(f"[{title}] DB: {last_chapter} | Web: {chapter_terbaru}")
                    
                    # Jika data di database masih '0', ini adalah running pertama kali (simpan tanpa kirim notif)
                    if last_chapter == '0':
                        cursor.execute(
                            "UPDATE manga_tracks SET last_chapter = %s WHERE title = %s",
                            (chapter_terbaru, title)
                        )
                        conn.commit()
                        print(f"-> Menginisialisasi chapter awal {title} ke {chapter_terbaru}")
                    
                    # JIKA ADA CHAPTER BARU NYATA (Tidak sama dengan data di database)
                    elif chapter_terbaru != last_chapter:
                        # Susun pesan notifikasi estetis dengan format Markdown
                        pesan = (
                            f"🔥 *UPDATE MANGA BARU!* 🔥\n\n"
                            f"📖 *{title}*\n"
                            f"✨ Sekarang sudah rilis *{chapter_terbaru}*\n\n"
                            f"🔗 [Klik untuk Membaca]({url})"
                        )
                        
                        # Kirim ke Telegram
                        kirim_telegram(pesan)
                        
                        # Perbarui data chapter terbaru di database
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
        time.sleep(900)  # Menunggu 900 detik (15 Menit) sebelum looping kembali
