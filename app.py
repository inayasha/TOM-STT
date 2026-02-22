import uuid
import streamlit as st
import speech_recognition as sr
import os
import subprocess
import math
import tempfile
import io
import requests
from shutil import which

# Import Library AI, DOCX, & Firebase
import google.generativeai as genai
from groq import Groq
from docx import Document
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from datetime import datetime

# ==========================================
# 1. SETUP & CONFIG
# ==========================================
st.set_page_config(page_title="TOM'STT", page_icon="🎙️", layout="centered", initial_sidebar_state="expanded")

# --- FIREBASE INITIALIZATION ---
if "firebase" not in st.secrets:
    st.error("⚠️ Kredensial Firebase belum di-set di Streamlit Secrets. Ikuti panduan untuk memasukkan JSON Firebase.")
    st.stop()

if not firebase_admin._apps:
    cred = credentials.Certificate(dict(st.secrets["firebase"]))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# --- FUNGSI DATABASE FIREBASE (USER) ---
def get_user(username):
    if not username: return None
    doc = db.collection('users').document(username).get()
    return doc.to_dict() if doc.exists else None

def save_user(username, password, role):
    """Menyimpan user baru beserta dompetnya, atau mengupdate user lama"""
    user_ref = db.collection('users').document(username)
    existing_user = user_ref.get()
    
    if existing_user.exists:
        # JIKA USER SUDAH ADA: Update password & role saja, JANGAN sentuh saldo/kuota!
        user_ref.update({"password": password, "role": role})
    else:
        # JIKA USER BARU: Buatkan akun dan berikan modal awal (Paket Freemium)
        user_ref.set({
            "password": password,
            "role": role,
            "paket_aktif": "Freemium",
            "kuota": 2,                # Jatah awal Freemium
            "saldo": 0,                # Saldo awal Rp 0
            "batas_durasi": 10,        # Maksimal audio 10 Menit
            "masa_aktif": "Selamanya",
            "created_at": datetime.now()
        })

def delete_user(username):
    db.collection('users').document(username).delete()
    
def check_expired(username, user_data):
    """SATPAM: Mengecek apakah paket user sudah kedaluwarsa. Jika ya, RESET semua."""
    if not user_data or user_data.get("role") == "admin": 
        return user_data # Lewati jika tidak ada data atau jika dia Admin
    
    exp_val = user_data.get("tanggal_expired")
    
    # Jika ada tanggal expired dan bukan 'Selamanya'
    if exp_val and exp_val != "Selamanya":
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Konversi jika format string (untuk berjaga-jaga), jika sudah datetime biarkan
        try:
            if isinstance(exp_val, str):
                exp_date = datetime.datetime.fromisoformat(exp_val.replace("Z", "+00:00"))
            else:
                exp_date = exp_val # Format bawaan Firebase Admin
                
            # EKSEKUSI HUKUMAN JIKA LEWAT WAKTU
            if now > exp_date:
                st.toast("⚠️ Masa aktif paket habis. Kuota dan Saldo di-reset.", icon="🚨")
                user_ref = db.collection('users').document(username)
                user_ref.update({
                    "paket_aktif": "Freemium",
                    "kuota": 0,
                    "saldo": 0,
                    "batas_durasi": 10,
                    "tanggal_expired": firestore.DELETE_FIELD
                })
                # Update data sementara agar UI langsung berubah di layar user
                user_data["paket_aktif"] = "Freemium"
                user_data["kuota"] = 0
                user_data["saldo"] = 0
                user_data["batas_durasi"] = 10
                user_data.pop("tanggal_expired", None)
        except Exception as e:
            pass # Abaikan jika gagal parsing agar tidak error di layar
            
    return user_data
    
# --- FUNGSI KASIR & SUBSIDI SILANG ---
def hitung_estimasi_menit(teks):
    """Estimasi durasi berdasarkan jumlah kata (Rata-rata bicara: 130 kata/menit)"""
    if not teks: return 0
    jumlah_kata = len(teks.split())
    durasi = math.ceil(jumlah_kata / 130)
    return durasi if durasi > 0 else 1 # Minimal terhitung 1 menit

def cek_pembayaran(username, durasi_menit):
    """Mengecek apakah user sanggup membayar. Return: (BisaBayar, Pesan, PotongKuota, PotongSaldo)"""
    user_ref = db.collection('users').document(username)
    user_data = user_ref.get().to_dict()
    
    if user_data.get("role") == "admin":
        return True, "Akses Admin (Gratis)", 0, 0

    kuota = user_data.get("kuota", 0)
    saldo = user_data.get("saldo", 0)
    batas_durasi = user_data.get("batas_durasi", 10)
    TARIF = 350 # Tarif Rp 350/Menit

    # Skenario 1: Durasi Aman (Sesuai Batas)
    if durasi_menit <= batas_durasi:
        if kuota > 0:
            return True, "1 Kuota Terpakai.", 1, 0
        else:
            biaya = durasi_menit * TARIF
            if saldo >= biaya: return True, f"Saldo terpotong Rp {biaya:,}", 0, biaya
            else: return False, f"Saldo kurang. Butuh Rp {biaya:,} untuk {durasi_menit} Menit.", 0, 0

    # Skenario 2: Subsidi Silang (Kelebihan Durasi)
    else:
        kelebihan = durasi_menit - batas_durasi
        biaya_tambahan = kelebihan * TARIF
        
        if kuota > 0:
            if saldo >= biaya_tambahan:
                return True, f"1 Kuota + Saldo Rp {biaya_tambahan:,} terpakai (Kelebihan waktu).", 1, biaya_tambahan
            else: return False, f"Kelebihan durasi! Saldo Anda kurang untuk menutupi biaya tambahan Rp {biaya_tambahan:,}", 0, 0
        else:
            biaya_total = durasi_menit * TARIF
            if saldo >= biaya_total: return True, f"Saldo terpotong Rp {biaya_total:,}", 0, biaya_total
            else: return False, f"Saldo kurang. Butuh Rp {biaya_total:,} untuk total {durasi_menit} Menit.", 0, 0

def eksekusi_pembayaran(username, potong_kuota, potong_saldo):
    """Memotong dompet di Firebase secara NYATA (Dipanggil hanya jika AI berhasil)"""
    if potong_kuota == 0 and potong_saldo == 0: return # Admin tidak dipotong
    user_ref = db.collection('users').document(username)
    user_ref.update({
        "kuota": firestore.Increment(-potong_kuota),
        "saldo": firestore.Increment(-potong_saldo)
    })

# --- FUNGSI DATABASE FIREBASE (API KEYS & LOAD BALANCER) ---
def add_api_key(name, provider, key_string, limit):
    db.collection('api_keys').add({
        "name": name,
        "provider": provider,
        "key": key_string,
        "limit": int(limit),
        "used": 0,
        "is_active": True
    })

def delete_api_key(doc_id):
    db.collection('api_keys').document(doc_id).delete()

def toggle_api_key(doc_id, current_status):
    db.collection('api_keys').document(doc_id).update({"is_active": not current_status})

def increment_api_usage(doc_id, current_used):
    db.collection('api_keys').document(doc_id).update({"used": current_used + 1})

def get_active_keys(provider):
    keys_ref = db.collection('api_keys').where("provider", "==", provider).where("is_active", "==", True).stream()
    valid_keys = []
    for doc in keys_ref:
        data = doc.to_dict()
        data['id'] = doc.id
        if data['used'] < data['limit']:
            valid_keys.append(data)
    return valid_keys

# Inisialisasi Memori (Session State)
if 'transcript' not in st.session_state: st.session_state.transcript = ""
if 'filename' not in st.session_state: st.session_state.filename = "Hasil_STT"
if 'logged_in' not in st.session_state: st.session_state.logged_in = False
if 'current_user' not in st.session_state: st.session_state.current_user = ""
if 'user_role' not in st.session_state: st.session_state.user_role = ""
if 'ai_result' not in st.session_state: st.session_state.ai_result = "" 
if 'ai_prefix' not in st.session_state: st.session_state.ai_prefix = "" 

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .stApp { background-color: #FFFFFF !important; }
    .main-header { font-family: -apple-system, sans-serif; font-weight: 800; color: #111111 !important; text-align: center; margin-top: 20px; font-size: 2.4rem; letter-spacing: -1.5px; }
    .sub-header { font-family: -apple-system, sans-serif; color: #666666 !important; text-align: center; font-size: 1rem; margin-bottom: 30px; font-weight: 500; }
    .stFileUploader label, div[data-testid="stSelectbox"] label, .stAudioInput label { width: 100% !important; text-align: center !important; display: block !important; color: #000000 !important; font-size: 1rem !important; font-weight: 700 !important; margin-bottom: 8px !important; }
    [data-testid="stFileUploaderDropzone"] { background-color: #F0F2F6 !important; border: 1px dashed #444 !important; border-radius: 12px; }
    [data-testid="stFileUploaderDropzone"] div, [data-testid="stFileUploaderDropzone"] span, [data-testid="stFileUploaderDropzone"] small { color: #000000 !important; }
    [data-testid="stFileUploaderDropzone"] button { background-color: #000000 !important; color: #FFFFFF !important; border: none !important; }
    .stFileUploader > div > small { display: none !important; }
    div[data-testid="stFileUploaderFileName"] { color: #000000 !important; font-weight: 600 !important; }
    
    /* FIX: Desain Universal Tombol agar semua senada dan seimbang */
    div.stButton > button, div.stDownloadButton > button, div[data-testid="stFormSubmitButton"] > button { 
        width: 100%; background-color: #000000 !important; color: #FFFFFF !important; border: 1px solid #000000; padding: 14px 20px; font-size: 16px; font-weight: 700; border-radius: 10px; transition: all 0.2s; box-shadow: 0 4px 6px rgba(0,0,0,0.1); 
    }
    div.stButton > button p, div.stDownloadButton > button p, div[data-testid="stFormSubmitButton"] > button p { color: #FFFFFF !important; }
    div.stButton > button:hover, div.stDownloadButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover { background-color: #333333 !important; color: #FFFFFF !important; transform: translateY(-2px); }
    
    .stCaption, p { color: #444444 !important; }
    textarea { color: #000000 !important; background-color: #F8F9FA !important; font-weight: 500 !important; }
    textarea:disabled { color: #000000 !important; -webkit-text-fill-color: #000000 !important; opacity: 1 !important; }
    
    [data-testid="collapsedControl"] svg, [data-testid="collapsedControl"] svg path,
    [data-testid="stSidebarCollapseButton"] svg, [data-testid="stSidebarCollapseButton"] svg path,
    button[kind="header"] svg, button[kind="header"] svg path { fill: #111111 !important; stroke: #111111 !important; color: #111111 !important; }

    /* FIX EXPANDER */
    [data-testid="stExpander"] details summary p, 
    [data-testid="stExpander"] details summary span { color: #111111 !important; font-weight: 700 !important; }
    [data-testid="stExpander"] details summary svg { fill: #111111 !important; color: #111111 !important; }

    div[data-testid="stMarkdownContainer"] p, div[data-testid="stMarkdownContainer"] h1, div[data-testid="stMarkdownContainer"] h2, div[data-testid="stMarkdownContainer"] h3, div[data-testid="stMarkdownContainer"] li, div[data-testid="stMarkdownContainer"] strong, div[data-testid="stMarkdownContainer"] span { color: #111111 !important; }
    [data-testid="stSidebar"] { background-color: #F4F6F9 !important; }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] p, [data-testid="stSidebar"] label { color: #111111 !important; font-weight: 600 !important; }
    [data-testid="stSidebar"] input { background-color: #FFFFFF !important; color: #000000 !important; border: 1px solid #CCCCCC !important; }
    .mobile-tips { background-color: #FFF3CD; color: #856404; padding: 12px; border-radius: 10px; font-size: 0.9rem; text-align: center; margin-bottom: 25px; border: 1px solid #FFEEBA; }
    .custom-info-box { background-color: #e6f3ff; color: #0068c9; padding: 15px; border-radius: 10px; text-align: center; font-weight: 600; border: 1px solid #cce5ff; margin-bottom: 20px; }
    .login-box { background-color: #F8F9FA; padding: 25px; border-radius: 12px; border: 1px solid #E0E0E0; margin-bottom: 20px; }
    .footer-link { text-decoration: none; font-weight: 700; color: #e74c3c !important; }
    
    /* Box Data API Key */
    .api-card { background-color: #f8f9fa; border: 1px solid #ddd; padding: 15px; border-radius: 8px; margin-bottom: 15px; color: #111111 !important; }
	
	/* FIX MODAL & DIALOG (POP-UP) STYLING */
    div[data-testid="stModal"] > div[role="dialog"], div[role="dialog"] { background-color: #FFFFFF !important; }
    div[role="dialog"] h1, div[role="dialog"] h2, div[role="dialog"] h3, div[role="dialog"] p, div[role="dialog"] li, div[role="dialog"] span { color: #111111 !important; }
    div[role="dialog"] div.stButton > button p { color: #FFFFFF !important; }
    div[role="dialog"] hr { border-color: #EEEEEE !important; }
	
	/* FIX TOMBOL BAYAR MIDTRANS (st.link_button) */
    div[data-testid="stLinkButton"] > a {
        width: 100% !important; 
        background-color: #000000 !important; 
        border: 1px solid #000000 !important; 
        border-radius: 10px !important; 
        padding: 14px 20px !important; 
        text-decoration: none !important; 
        display: flex !important; 
        justify-content: center !important; 
        align-items: center !important;
        transition: all 0.2s !important;
    }
    div[data-testid="stLinkButton"] > a p, 
    div[data-testid="stLinkButton"] > a span,
    div[role="dialog"] div[data-testid="stLinkButton"] > a p,
    div[role="dialog"] div[data-testid="stLinkButton"] > a span {
        color: #FFFFFF !important; 
        font-weight: 700 !important; 
        font-size: 16px !important;
    }
    div[data-testid="stLinkButton"] > a:hover {
        background-color: #333333 !important; 
        transform: translateY(-2px) !important;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. FUNGSI PENDUKUNG (DOCX, FFMPEG)
# ==========================================
project_folder = os.getcwd()
local_ffmpeg, local_ffprobe = os.path.join(project_folder, "ffmpeg.exe"), os.path.join(project_folder, "ffprobe.exe")
if os.path.exists(local_ffmpeg) and os.path.exists(local_ffprobe):
    ffmpeg_cmd, ffprobe_cmd = local_ffmpeg, local_ffprobe
    os.environ["PATH"] += os.pathsep + project_folder
else:
    if which("ffmpeg") and which("ffprobe"): ffmpeg_cmd, ffprobe_cmd = "ffmpeg", "ffprobe"
    else: st.error("❌ FFmpeg not found."); st.stop()

def get_duration(file_path):
    try: return float(subprocess.check_output([ffprobe_cmd, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path], stderr=subprocess.STDOUT))
    except: return 0.0

def create_docx(text, title):
    doc = Document()
    doc.add_heading(title, level=1)
    for line in text.split('\n'):
        if line.strip() != "": doc.add_paragraph(line)
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()

PROMPT_NOTULEN = """Kamu adalah Sekretaris Profesional. Tugasmu membuat Notulen Rapat dari transkrip yang diberikan.
INSTRUKSI MUTLAK:
- TULIS SANGAT PANJANG, MENDETAIL, DAN KOMPREHENSIF. 
- JANGAN MERINGKAS TERLALU PENDEK. Jabarkan seluruh diskusi, nama (jika ada), argumen pro/kontra, data, dan fakta yang dibahas.
- Ekstrak SEMUA informasi tanpa ada yang terlewat.
Format:
1. Agenda Utama: (Latar belakang komprehensif).
2. Uraian Detail Pembahasan: (Jabarkan paragraf demi paragraf, poin per poin dengan sangat lengkap).
3. Keputusan: (Keputusan akhir dan alasannya).
4. Tindak Lanjut: (Langkah teknis dan penanggung jawab)."""

PROMPT_LAPORAN = """Kamu adalah ASN tingkat manajerial. Tugasmu menyusun ISI LAPORAN dari transkrip.
INSTRUKSI MUTLAK:
- TULIS SANGAT PANJANG, MENDETAIL, DAN KOMPREHENSIF.
- JANGAN MERINGKAS. Jabarkan setiap topik yang dibahas, masalah yang ditemukan, dan solusi secara ekstensif.
- Abaikan kop surat (Yth, Hal, dll). Langsung ke isi.
Format:
1. Pendahuluan: (Penjelasan acara/rapat secara lengkap).
2. Uraian Hasil Pelaksanaan: (Penjabaran ekstensif seluruh dinamika, fakta, dan informasi dari transkrip).
3. Kesimpulan & Analisis: (Analisis mendalam atas hasil pembahasan).
4. Rekomendasi/Tindak Lanjut: (Saran konkret ke depan).
5. Penutup: ('Demikian kami laporkan, mohon arahan Bapak/Ibu Pimpinan lebih lanjut. Terima kasih.')."""

# ==========================================
# 3. SIDEBAR & ETALASE HARGA (MIDTRANS SNAP)
# ==========================================
def buat_tagihan_midtrans(nama_paket, harga, user_email):
    """Menghubungi server Midtrans untuk meminta Link Pembayaran (QRIS/VA)"""
    # URL Sandbox (Ubah ke https://app.midtrans.com/snap/v1/transactions jika sudah rilis resmi/Production)
    url = "https://app.sandbox.midtrans.com/snap/v1/transactions" 
    server_key = st.secrets["midtrans_server_key"]
    
    # Membuat Order ID unik (Contoh: TOM-STARTER-A1B2C3)
    order_id = f"TOM-{nama_paket.split()[0].upper()}-{uuid.uuid4().hex[:6].upper()}"
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    payload = {
        "transaction_details": {
            "order_id": order_id,
            "gross_amount": harga
        },
        "customer_details": {
            "email": user_email
        },
	    "custom_field1": user_email,  # <--- TAMBAHKAN BARIS INI (Sangat Krusial)
        "item_details": [{
            "id": nama_paket.replace(" ", "_"),
            "price": harga,
            "quantity": 1,
            "name": f"Paket {nama_paket} TOM'STT"
        }]
    }
    
    # Mengirim permintaan ke Midtrans
    response = requests.post(url, auth=(server_key, ''), json=payload, headers=headers)
    
    if response.status_code == 201:
        return response.json().get("redirect_url")
    else:
        st.error(f"Gagal menghubungi gateway pembayaran. Pesan Error: {response.text}")
        return None

@st.dialog("🛒 Beli Paket & Top-Up Saldo", width="large")
def show_pricing_dialog():
    user_email = st.session_state.current_user
    
    st.markdown("""
    > ⚡ **Investasi Waktu Terbaik Anda**
    > Dapatkan kembali waktu istirahat Anda. Biarkan AI kami yang bekerja keras menyusun laporan rumit hanya dengan biaya setara segelas kopi per dokumen!
    """)
    
    st.info("""
    💡 **Informasi Sistem & Ketentuan:**
    * 🎟️ **Sistem Tiket:** 1 Kuota = 1x Pembuatan Dokumen (Laporan/Notulen).
    * ⚖️ **Tagihan Adil (Deteksi Jeda):** Durasi dihitung berdasarkan **jumlah kata aktual** yang diucapkan, BUKAN total waktu rekaman mentah. Waktu hening & jeda tidak memotong kuota Anda.
    * 📅 **Akumulasi Masa Aktif:** Pembelian paket baru akan otomatis menambah sisa masa aktif Anda sebelumnya *(Maksimal akumulasi 365 Hari / 1 Tahun)*.
    * 💳 **Saldo Tambahan:** Jika rekaman melebihi batas maksimal, sistem menggunakan Saldo Utama dengan tarif **Rp 350 / Menit**.
    """)
    
    tab_paket, tab_saldo = st.tabs(["📦 BELI PAKET KUOTA", "💳 TOP-UP SALDO"])
    
    with tab_paket:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("""
            **1. Paket Starter**
            *Cocok untuk kebutuhan personal & tugas ringan.*
            * 📄 **5x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 1 Jam / File
            * 📅 **Masa Aktif:** 14 Hari
            * 🎁 **Bonus Saldo:** Rp 3.000
            * &nbsp;
            """)
            if st.button("🛒 Beli Starter - Rp 51.000", use_container_width=True, key="buy_starter"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Starter", 51000, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar (Termasuk 2% Biaya Layanan)", link_bayar, use_container_width=True)
            
            st.markdown("---")
            
            st.markdown("""
            **2. Paket Pro Notulis**
            *Standar profesional untuk notulis & sekretaris.*
            * 📄 **15x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 1,5 Jam / File
            * 📅 **Masa Aktif:** 30 Hari
            * 🎁 **Bonus Saldo:** Rp 10.000
            * &nbsp;
            """)
            if st.button("🛒 Beli Pro - Rp 102.000", use_container_width=True, key="buy_pro"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Pro", 102000, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar (Termasuk 2% Biaya Layanan)", link_bayar, use_container_width=True)

        with col2:
            st.markdown("""
            **3. Paket Eksekutif**
            *Pilihan tepat untuk intensitas rapat tinggi.*
            * 📄 **50x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 2 Jam / File
            * 📅 **Masa Aktif:** 45 Hari
            * 🎁 **Bonus Saldo:** Rp 20.000
            * 🛡️ **Jaminan Akses:** Multi-Server API (Anti-Limit)
            """)
            if st.button("🛒 Beli Eksekutif - Rp 306.000", use_container_width=True, key="buy_exec"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Eksekutif", 306000, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar (Termasuk 2% Biaya Layanan)", link_bayar, use_container_width=True)
            
            st.markdown("---")
            
            st.markdown("""
            **4. Paket VIP Instansi**
            *Akses maksimal untuk kementerian/instansi.*
            * 📄 **100x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 3 Jam / File
            * 📅 **Masa Aktif:** 60 Hari
            * 🎁 **Bonus Saldo:** Rp 35.000
            * 🛡️ **Jaminan Akses:** Multi-Server API (Anti-Limit)
            """)
            if st.button("🛒 Beli VIP - Rp 510.000", use_container_width=True, key="buy_vip"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("VIP", 510000, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar (Termasuk 2% Biaya Layanan)", link_bayar, use_container_width=True)

    with tab_saldo:
        st.warning("ℹ️ **Catatan:** Saldo yang Anda beli masuk utuh 100% ke dompet Anda. Kami hanya menambahkan 2% pada tombol bayar sebagai Biaya Layanan (Payment Gateway).")
        
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            st.markdown("""
            **Saldo Rp 10.000**
            * ⏱️ Melindungi **± 28 Menit**
            """)
            if st.button("💳 Bayar Rp 10.200", use_container_width=True, key="topup_10"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Topup10k", 10200, user_email) 
                    if link_bayar: st.link_button("💳 Lanjut Bayar", link_bayar, use_container_width=True)
            
            st.markdown("---")
            
            st.markdown("""
            **Saldo Rp 20.000**
            * ⏱️ Melindungi **± 57 Menit**
            """)
            if st.button("💳 Bayar Rp 20.400", use_container_width=True, key="topup_20"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Topup20k", 20400, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar", link_bayar, use_container_width=True)

        with col_s2:
            st.markdown("""
            **Saldo Rp 30.000**
            * ⏱️ Melindungi **± 85 Menit**
            """)
            if st.button("💳 Bayar Rp 30.600", use_container_width=True, key="topup_30"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Topup30k", 30600, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar", link_bayar, use_container_width=True)
            
            st.markdown("---")
            
            st.markdown("""
            **Saldo Rp 40.000**
            * ⏱️ Melindungi **± 114 Menit**
            """)
            if st.button("💳 Bayar Rp 40.800", use_container_width=True, key="topup_40"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Topup40k", 40800, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar", link_bayar, use_container_width=True)
                    
with st.sidebar:
    st.header("⚙️ Status Sistem")
    
    if st.session_state.logged_in:
        st.success(f"👤 Login as: {st.session_state.current_user}")
        
        # --- MENARIK DATA DOMPET DARI FIREBASE ---
        user_data = get_user(st.session_state.current_user)
        
        if user_data:
            st.markdown("---")
            st.markdown("### 💼 Dompet Anda")
            
            # 🚨 PANGGIL SATPAM: Cek expired sebelum dirender ke layar
            user_data = check_expired(st.session_state.current_user, user_data)
            
            if user_data.get("role") == "admin":
                st.info("👑 Paket: **Super Admin (VIP)**")
                col_k, col_b = st.columns(2)
                col_k.metric("Sisa Kuota", "∞")
                col_b.metric("Batas Paket", "∞")
                st.metric("💳 Saldo Darurat", "∞")
            else:
                paket = user_data.get("paket_aktif", "Freemium")
                kuota = user_data.get("kuota", 0)
                saldo = user_data.get("saldo", 0)
                batas = user_data.get("batas_durasi", 10)
                exp_val = user_data.get("tanggal_expired")
                
                # Format Tanggal untuk Tampilan
                status_waktu = "⏳ **Berlaku hingga:** Selamanya" # Default murni untuk Freemium
                if paket != "Freemium" and exp_val and exp_val != "Selamanya":
                    import datetime
                    try:
                        if isinstance(exp_val, str):
                            exp_date = datetime.datetime.fromisoformat(exp_val.replace("Z", "+00:00"))
                        else:
                            exp_date = exp_val
                        status_waktu = f"⏳ **Berlaku hingga:** {exp_date.strftime('%d %b %Y')}"
                    except:
                        pass
                elif paket != "Freemium":
                    status_waktu = "⏳ **Berlaku hingga:** Selamanya"
                
                estimasi_menit = math.floor(saldo / 350)
                saldo_rp = f"Rp {saldo:,}".replace(",", ".")
                
                # UI Dashboard Mini Baru + TANGGAL
                st.markdown(f"📦 **Paket Aktif:** {paket}")
                st.markdown(f"📄 **Sisa Kuota:** {kuota}x")
                st.markdown(f"⏱️ **Kapasitas:** Maks. {batas} Menit per Kuota")
                st.markdown(status_waktu) # <--- Tanggal Expired Muncul di Sini
                st.markdown("---")
                
                st.metric("💳 Saldo Utama", saldo_rp)
                st.caption(f"*(Melindungi ± {estimasi_menit} Menit kelebihan durasi)*")
                
                st.write("")
                if st.button("🔄 Segarkan Dompet", use_container_width=True):
                    st.rerun()
                if st.button("🛒 Beli Paket / Top-Up", use_container_width=True):
                    show_pricing_dialog()  
                
            st.markdown("---")
            
        if st.session_state.user_role == "admin": 
            st.info("👑 Anda Administrator.")
            
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state.logged_in, st.session_state.current_user, st.session_state.user_role = False, "", ""
            st.session_state.ai_result = ""
            st.rerun()
    else:
        st.caption("Silakan login di Tab '🔐 Akun'.")

# ==========================================
# 4. MAIN LAYOUT & TABS
# ==========================================
st.markdown('<div class="main-header">🎙️ TOM\'<span style="color: #e74c3c !important;">STT</span></div>', unsafe_allow_html=True)

# KOTAK SELAMAT DATANG (COPYWRITING BARU)
st.info("""
🚀 **Otomatisasi Notulen & Laporan dalam Hitungan Menit** Mengubah rekaman rapat berjam-jam menjadi teks manual bisa menyita 1-2 hari kerja Anda. Dengan mesin AI TOM'STT, semuanya selesai secara instan!

🧠 **Bukan Sekadar Transkrip Biasa:** AI kami telah diprogram khusus untuk langsung mengekstrak **Notulen Rapat** atau **Laporan Memorandum** siap cetak—lengkap dengan latar belakang, analisis, dan tindak lanjut berstandar profesional.
""")

tab_titles = ["📂 Upload File", "🎙️ Rekam Suara", "✨ Ekstrak AI", "🔐 Akun"]
if st.session_state.user_role == "admin": tab_titles.append("⚙️ Panel Admin")
tabs = st.tabs(tab_titles)
tab_upload, tab_rekam, tab_ai, tab_auth = tabs[0], tabs[1], tabs[2], tabs[3]

audio_to_process, source_name = None, "audio"
submit_btn = False
lang_code = "id-ID"

# TAB 1: UPLOAD FILE (Bebas Akses)
with tab_upload:
    uploaded_file = st.file_uploader("Pilih File Audio", type=["aac", "mp3", "wav", "m4a", "opus", "mp4", "3gp", "amr", "ogg", "flac", "wma"])
    if uploaded_file: audio_to_process, source_name = uploaded_file, uploaded_file.name
    
    st.write("") 
    c1, c2, c3 = st.columns([1, 4, 1]) 
    with c2:
        lang_choice_upload = st.selectbox("Pilih Bahasa Audio", ("Indonesia", "Inggris"), key="lang_up")
        st.write("") 
        if uploaded_file:
            if st.button("🚀 Mulai Transkrip", use_container_width=True, key="btn_up"):
                submit_btn = True
                lang_code = "id-ID" if lang_choice_upload == "Indonesia" else "en-US"
        else:
            st.markdown('<div class="custom-info-box">👆 Silakan Upload terlebih dahulu.</div>', unsafe_allow_html=True)

# TAB 2: REKAM SUARA (Terkunci)
with tab_rekam:
    if not st.session_state.logged_in:
        st.markdown('<div style="text-align: center; padding: 20px; background-color: #fdeced; border-radius: 10px; border: 1px solid #f5c6cb; margin-bottom: 20px;"><h3 style="color: #e74c3c; margin-top: 0;">🔒 Akses Terkunci!</h3><p style="color: #e74c3c; font-weight: 500;">Silakan masuk (login) atau daftar terlebih dahulu di tab <b>🔐 Akun</b> untuk menggunakan fitur rekam suara langsung.</p></div>', unsafe_allow_html=True)
    else:
        audio_mic = st.audio_input("Klik ikon mic untuk mulai merekam")
        if audio_mic: audio_to_process, source_name = audio_mic, "rekaman_mic.wav"
        
        st.write("") 
        c1, c2, c3 = st.columns([1, 4, 1]) 
        with c2:
            lang_choice_mic = st.selectbox("Pilih Bahasa Audio", ("Indonesia", "Inggris"), key="lang_mic")
            st.write("") 
            if audio_mic:
                if st.button("🚀 Mulai Transkrip", use_container_width=True, key="btn_mic"):
                    submit_btn = True
                    lang_code = "id-ID" if lang_choice_mic == "Indonesia" else "en-US"
            else:
                st.markdown('<div class="custom-info-box">👆 Silakan Rekam terlebih dahulu.</div>', unsafe_allow_html=True)

if submit_btn and audio_to_process:
    st.markdown("---")
    status_box, progress_bar, result_area = st.empty(), st.progress(0), st.empty()
    full_transcript = []
    
    file_ext = ".wav" if source_name == "rekaman_mic.wav" else (os.path.splitext(source_name)[1] or ".wav")
    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
        tmp_file.write(audio_to_process.getvalue())
        input_path = tmp_file.name

    try:
        duration_sec = get_duration(input_path)
        if duration_sec == 0: st.error("Gagal membaca audio."); st.stop()
        
        chunk_len = 59 
        total_chunks = math.ceil(duration_sec / chunk_len)
        status_box.info(f"⏱️ Durasi: {duration_sec:.2f}s")
        
        recognizer = sr.Recognizer()
        recognizer.energy_threshold, recognizer.dynamic_energy_threshold = 300, True 

        for i in range(total_chunks):
            start_time = i * chunk_len
            chunk_filename = f"temp_slice_{i}.wav"
            cmd = [ffmpeg_cmd, "-y", "-i", input_path, "-ss", str(start_time), "-t", str(chunk_len), "-filter:a", "volume=3.0", "-ar", "16000", "-ac", "1", chunk_filename]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            try:
                with sr.AudioFile(chunk_filename) as source:
                    audio_data = recognizer.record(source)
                    text = recognizer.recognize_google(audio_data, language=lang_code)
                    full_transcript.append(text)
                    result_area.text_area("📝 Live Preview:", " ".join(full_transcript), height=250)
            except: full_transcript.append("") 
            finally:
                if os.path.exists(chunk_filename): os.remove(chunk_filename)
            
            progress_bar.progress(int(((i + 1) / total_chunks) * 100))
            status_box.caption(f"Sedang memproses... ({int(((i + 1) / total_chunks) * 100)}%)")

        status_box.success("✅ Selesai! Transkrip tersimpan. Silakan klik Tab '✨ Ekstrak AI'.")
        final_text = " ".join(full_transcript)
        st.session_state.transcript, st.session_state.filename = final_text, os.path.splitext(source_name)[0]
        st.session_state.ai_result = "" 
        st.download_button("💾 Download (.TXT)", final_text, f"{st.session_state.filename}.txt", "text/plain", use_container_width=True)

    except Exception as e: st.error(f"Error: {e}")
    finally:
        if os.path.exists(input_path): os.remove(input_path)

# ==========================================
# 5. TAB 3 (AKSES AKUN) & TAB 4 (EKSTRAK AI)
# ==========================================
with tab_auth:
    if not st.session_state.logged_in:
        st.markdown('<div class="login-box" style="text-align: center;"><h3>🔒 Portal Akses</h3><p>Silakan masuk atau buat akun baru untuk mulai menggunakan AI.</p></div>', unsafe_allow_html=True)
        
        auth_tab1, auth_tab2 = st.tabs(["🔑 Masuk (Login)", "📝 Daftar Baru (Register)"])
        
        # --- TAB LOGIN ---
        with auth_tab1:
            login_email = st.text_input("Email", key="log_email").strip()
            login_pwd = st.text_input("Password", type="password", key="log_pwd")
            if st.button("🚀 Masuk Sistem", use_container_width=True):
                with st.spinner("Mengecek kredensial..."):
                    api_key = st.secrets["firebase_web_api_key"]
                    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
                    res = requests.post(url, json={"email": login_email, "password": login_pwd, "returnSecureToken": True}).json()
                    
                    if "idToken" in res:
                        id_token = res["idToken"]
                        
                        # CEK STATUS VERIFIKASI EMAIL DI FIREBASE
                        url_lookup = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={api_key}"
                        lookup_res = requests.post(url_lookup, json={"idToken": id_token}).json()
                        is_verified = lookup_res.get("users", [{}])[0].get("emailVerified", False)
                        
                        user_data = get_user(login_email)
                        is_admin = user_data and user_data.get("role") == "admin"
                        
                        # LOGIKA SATPAM: Tolak jika belum verifikasi (Kecuali Admin Utama)
                        if not is_verified and not is_admin:
                            st.error("❌ Akses Ditolak: Email Anda belum diverifikasi!")
                            st.warning("📧 Silakan cek Inbox atau folder Spam di email Anda, lalu klik link verifikasi yang telah kami kirimkan saat Anda mendaftar.")
                        else:
                            # Jika user lolos verifikasi, masukkan ke sistem!
                            if not user_data:
                                save_user(login_email, login_pwd, "user")
                                user_data = {"role": "user"}
                                
                            st.session_state.logged_in = True
                            st.session_state.current_user = login_email
                            st.session_state.user_role = user_data.get("role", "user")
                            st.rerun()
                    else:
                        err = res.get("error", {}).get("message", "Gagal")
                        if err == "INVALID_LOGIN_CREDENTIALS": st.error("❌ Email atau Password salah!")
                        else: st.error(f"❌ Akses Ditolak: {err}")
                        
        # --- TAB REGISTER MANDIRI ---
        with auth_tab2:
            reg_email = st.text_input("Email Aktif", key="reg_email").strip()
            reg_pwd = st.text_input("Buat Password (Min. 6 Karakter)", type="password", key="reg_pwd")
            if st.button("🎁 Daftar & Klaim Kuota Gratis", use_container_width=True):
                if len(reg_pwd) < 6:
                    st.error("❌ Password terlalu pendek. Minimal 6 karakter!")
                elif not reg_email:
                    st.error("❌ Email tidak boleh kosong!")
                else:
                    with st.spinner("Mendaftarkan akun & mengirim email verifikasi..."):
                        api_key = st.secrets["firebase_web_api_key"]
                        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}"
                        res = requests.post(url, json={"email": reg_email, "password": reg_pwd, "returnSecureToken": True}).json()
                        
                        if "idToken" in res:
                            id_token = res["idToken"]
                            
                            # PERINTAHKAN FIREBASE MENGIRIM EMAIL VERIFIKASI KE USER
                            url_verify = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}"
                            requests.post(url_verify, json={"requestType": "VERIFY_EMAIL", "idToken": id_token})
                            
                            # Simpan dompet Freemium di Firestore
                            save_user(reg_email, reg_pwd, "user")
                            
                            st.success("✅ Pembuatan akun berhasil!")
                            st.info("🚨 **LANGKAH WAJIB:** Kami telah mengirimkan link verifikasi ke email Anda. Anda **TIDAK AKAN BISA LOGIN** sebelum mengeklik link tersebut. Jangan lupa cek folder Spam!")
                        else:
                            err = res.get("error", {}).get("message", "Gagal")
                            if err == "EMAIL_EXISTS": st.error("❌ Email sudah terdaftar. Silakan langsung Login saja.")
                            elif err == "INVALID_EMAIL": st.error("❌ Format email tidak valid. Gunakan email asli!")
                            else: st.error(f"❌ Gagal mendaftar: {err}")
    else:
        st.success(f"✅ Anda saat ini masuk sebagai: **{st.session_state.current_user}**")
        st.info("💡 Silakan beralih ke tab **✨ Ekstrak AI** atau **🎙️ Rekam Suara** untuk mulai menggunakan layanan.")

with tab_ai:
    if not st.session_state.logged_in:
        st.markdown('<div style="text-align: center; padding: 20px; background-color: #fdeced; border-radius: 10px; border: 1px solid #f5c6cb; margin-bottom: 20px;"><h3 style="color: #e74c3c; margin-top: 0;">🔒 Akses Terkunci!</h3><p style="color: #e74c3c; font-weight: 500;">Silakan masuk (login) atau daftar terlebih dahulu di tab <b>🔐 Akun</b> untuk menggunakan fitur AI.</p></div>', unsafe_allow_html=True)
    else:
        if not st.session_state.transcript:
            st.markdown('<div class="custom-info-box">👆 Transkrip belum tersedia.<br><strong>ATAU</strong> Unggah file .txt di bawah ini:</div>', unsafe_allow_html=True)
            uploaded_txt = st.file_uploader("Upload File Transkrip (.txt)", type=["txt"])
            if uploaded_txt:
                st.session_state.transcript, st.session_state.filename = uploaded_txt.read().decode("utf-8"), os.path.splitext(uploaded_txt.name)[0]
                st.session_state.ai_result = "" 
                st.rerun()
        else:
            st.success("✅ Teks Transkrip Siap Diproses!")
            st.text_area("📄 Teks Saat Ini:", st.session_state.transcript, height=150, disabled=True)
            if st.button("🗑️ Hapus Teks"): 
                st.session_state.transcript, st.session_state.ai_result = "", "" 
                st.rerun()
                
            st.write("")
            st.markdown("#### ⚙️ Pilih Mesin AI")
            engine_choice = st.radio("Silakan pilih AI yang ingin digunakan:", ["Gemini", "Groq"])
            
            # --- UI INDIKATOR TAGIHAN (TRANSPARANSI) ---
            durasi_teks = hitung_estimasi_menit(st.session_state.transcript)
            jumlah_kata = len(st.session_state.transcript.split())
            
            # AMBIL DATA USER UNTUK CEK KONDISI TAGIHAN
            user_info = get_user(st.session_state.current_user)
            info_text = f"📊 **Analisis Teks:** Anda memiliki **{jumlah_kata:,} Kata**. (Setara dengan **± {durasi_teks} Menit** pemakaian sistem)."
            
            if user_info:
                batas = user_info.get("batas_durasi", 10)
                kuota = user_info.get("kuota", 0)
                role = user_info.get("role", "user")
                
                if role == "admin":
                    info_text += "\n\n👑 **Status:** Anda adalah Admin. Bebas kuota dan tanpa batas durasi."
                elif kuota > 0:
                    if durasi_teks > batas:
                        kelebihan = durasi_teks - batas
                        biaya = kelebihan * 350
                        biaya_rp = f"Rp {biaya:,}".replace(",", ".")
                        info_text += f"\n\n⚠️ **Subsidi Silang:** Durasi teks Anda melebihi kapasitas paket ({batas} Menit). Kelebihan **{kelebihan} Menit** akan memotong Saldo Utama Anda sebesar **{biaya_rp}**."
                    else:
                        info_text += f"\n\n✅ **Status Aman:** Durasi teks masih di bawah batas maksimal ({batas} Menit / Kuota). Tidak ada pemotongan saldo."
                else:
                    biaya_total = durasi_teks * 350
                    biaya_rp = f"Rp {biaya_total:,}".replace(",", ".")
                    info_text += f"\n\n⚠️ **Saldo Darurat:** Kuota Anda habis (0x). Seluruh pemrosesan (**{durasi_teks} Menit**) akan langsung memotong Saldo Utama Anda sebesar **{biaya_rp}**."
                    
            st.info(info_text)
            st.write("")
            
            col1, col2 = st.columns(2)
            with col1: btn_notulen = st.button("📝 Buat Notulen", use_container_width=True)
            with col2: btn_laporan = st.button("📋 Buat Laporan", use_container_width=True)

            if btn_notulen or btn_laporan:
                # 1. CEK BIAYA SEBELUM MEMANGGIL AI
                bisa_bayar, pesan_bayar, p_kuota, p_saldo = cek_pembayaran(st.session_state.current_user, durasi_teks)
                
                if not bisa_bayar:
                    st.error(f"❌ TRANSAKSI DITOLAK: {pesan_bayar}")
                    st.warning("💡 Silakan Top-Up Saldo atau Upgrade Paket Anda.")
                else:
                    # 2. LANJUT PROSES AI JIKA SALDO/KUOTA CUKUP
                    prompt_active = PROMPT_NOTULEN if btn_notulen else PROMPT_LAPORAN
                    ai_result = None
                    
                    active_keys = get_active_keys(engine_choice)
                    
                    if not active_keys:
                        st.error(f"❌ Sistem Sibuk: Tidak ada API Key {engine_choice} yang aktif. Saldo/Kuota Anda AMAN (Tidak dipotong).")
                    else:
                        success_generation = False
                        
                        with st.spinner(f"🚀 Memproses dengan {engine_choice} (Beban: {durasi_teks} Menit Kuota)..."):
                            for key_data in active_keys:
                                try:
                                    if engine_choice == "Gemini":
                                        genai.configure(api_key=key_data["key"])
                                        model = genai.GenerativeModel('gemini-2.5-flash')
                                        response = model.generate_content(f"{prompt_active}\n\nBerikut teks transkripnya:\n{st.session_state.transcript}")
                                        ai_result = response.text
                                        
                                    elif engine_choice == "Groq":
                                        client = Groq(api_key=key_data["key"])
                                        completion = client.chat.completions.create(
                                            model="llama-3.3-70b-versatile",
                                            messages=[{"role": "system", "content": prompt_active}, {"role": "user", "content": f"Berikut transkripnya:\n{st.session_state.transcript}"}],
                                            temperature=0.4,
                                        )
                                        ai_result = completion.choices[0].message.content

                                    increment_api_usage(key_data["id"], key_data["used"])
                                    success_generation = True
                                    break 
                                    
                                except Exception as e:
                                    st.toast(f"⚠️ Mencoba server cadangan...")
                                    continue
                        
                        if success_generation and ai_result:
                            # 3. POTONG SALDO KARENA HASIL BERHASIL DIBUAT!
                            eksekusi_pembayaran(st.session_state.current_user, p_kuota, p_saldo)
                            
                            # MENGGANTI TOAST MENJADI KOTAK SUCCESS BESAR
                            st.success(f"✅ **Proses Selesai!** {pesan_bayar}")
                            
                            st.session_state.ai_result = ai_result
                            st.session_state.ai_prefix = "Notulen_" if btn_notulen else "Laporan_"
                        elif not success_generation:
                            st.error("❌ Gagal memproses. Server API sedang gangguan. Saldo & Kuota Anda AMAN (Tidak dipotong).")

            if st.session_state.ai_result:
                st.markdown("---")
                st.markdown("### ✨ Hasil Ekstrak AI (Super Mendetail)")
                st.markdown(st.session_state.ai_result)
                
                prefix = st.session_state.ai_prefix
                st.download_button("💾 Download Hasil AI (.TXT)", st.session_state.ai_result, f"{prefix}{st.session_state.filename}.txt", "text/plain", use_container_width=True)
                docx_file = create_docx(st.session_state.ai_result, f"{prefix}{st.session_state.filename}")
                st.download_button("📄 Download Hasil AI (.DOCX)", data=docx_file, file_name=f"{prefix}{st.session_state.filename}.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", use_container_width=True)

# ==========================================
# 6. TAB 5 (PANEL ADMIN) - DATABASE API KEY & LIMIT
# ==========================================
if st.session_state.user_role == "admin":
    with tabs[4]:
        st.markdown("#### ⚙️ Pusat Kendali & Manajemen")
        
        # --- MANAJEMEN API KEY & LOAD BALANCER ---
        st.markdown("#### 🏦 Bank API Key (Load Balancer)")
        st.caption("Tambahkan API Key Anda. Sistem akan otomatis membagi beban dan melompat jika ada kunci yang error/habis limit.")
        
        with st.expander("➕ Tambah API Key Baru"):
            with st.form("form_add_key"):
                col1, col2 = st.columns(2)
                with col1:
                    new_provider = st.selectbox("Provider", ["Gemini", "Groq"])
                    new_name = st.text_input("Nama Key (Misal: Akun Istri)")
                with col2:
                    new_limit = st.number_input("Batas Limit Kuota/Hari", min_value=1, value=200)
                    new_key_str = st.text_input("Paste API Key", type="password")
                
                if st.form_submit_button("Simpan Kunci API"):
                    if new_name and new_key_str:
                        add_api_key(new_name, new_provider, new_key_str, new_limit)
                        st.success("✅ API Key berhasil ditambahkan ke Bank!")
                        st.rerun()
                    else: st.error("Isi Nama dan API Key!")

        st.markdown("#### 📋 Daftar API Key & Sisa Kuota")
        keys_ref = db.collection('api_keys').stream()
        
        for doc in keys_ref:
            k = doc.to_dict()
            sisa_kuota = k['limit'] - k['used']
            status_text = "🟢 AKTIF" if k['is_active'] else "🔴 NONAKTIF"
            status_color = "#e6f3ff" if k['is_active'] else "#fdeced"
            
            st.markdown(f"""
            <div class="api-card" style="background-color: {status_color}; color: #111111 !important;">
                <b style="color: #111111 !important;">{k['name']}</b> ({k['provider']}) <br>
                Sisa Limit: <b style="color: #111111 !important;">{sisa_kuota}</b> / <span style="color: #111111 !important;">{k['limit']}</span> &nbsp;|&nbsp; Terpakai: <span style="color: #111111 !important;">{k['used']}</span> <br>
                Status: <span style="color: #111111 !important; font-weight: bold;">{status_text}</span>
            </div>
            """, unsafe_allow_html=True)
            
            # FIX: Tombol dibuat seragam dan sejajar (tanpa HTML tambahan)
            ca1, ca2 = st.columns([1, 1])
            with ca1:
                btn_label = "🔴 Matikan" if k['is_active'] else "🟢 Hidupkan"
                if st.button(f"{btn_label} '{k['name']}'", key=f"tog_{doc.id}", use_container_width=True):
                    toggle_api_key(doc.id, k['is_active'])
                    st.rerun()
            with ca2:
                if st.button(f"🗑️ Hapus '{k['name']}'", key=f"del_{doc.id}", use_container_width=True):
                    delete_api_key(doc.id)
                    st.rerun()
            st.write("---")
        
        # --- MANAJEMEN USER ---
        st.markdown("#### 👥 Manajemen User")
        users_ref = db.collection('users').stream()
        st.write("Daftar Pengguna Saat Ini:")
        for doc in users_ref:
            u_data = doc.to_dict()
            st.markdown(f"- **{doc.id}** (Role: {u_data['role']})")
            
        with st.form("user_form"):
            add_email = st.text_input("Username Baru/Edit")
            add_pwd = st.text_input("Password Baru", type="password")
            add_role = st.selectbox("Role", ["user", "admin"])
            
            # FIX: Tombol dibuat seragam dan sejajar (tanpa HTML tambahan)
            c_add, c_del = st.columns(2)
            with c_add:
                if st.form_submit_button("Simpan User", use_container_width=True):
                    if add_email and add_pwd:
                        save_user(add_email, add_pwd, add_role)
                        st.success(f"✅ User {add_email} disimpan ke Firebase!")
                        st.rerun()
                    else: st.error("Isi Username dan Password!")
            with c_del:
                if st.form_submit_button("Hapus User", use_container_width=True):
                    if add_email:
                        if get_user(add_email):
                            if add_email == "admin": st.error("Dilarang menghapus Admin Utama!")
                            else:
                                delete_user(add_email)
                                st.warning(f"🗑️ User {add_email} dihapus dari Firebase!")
                                st.rerun()
                        else: st.error("User tidak ditemukan.")
                    else:
                        st.error("Isi Username yang ingin dihapus!")

st.markdown("<br><br><hr>", unsafe_allow_html=True) 
st.markdown("""<div style="text-align: center; font-size: 13px; color: #888;">Powered by <a href="https://espeje.com" target="_blank" class="footer-link">espeje.com</a> & <a href="https://link-gr.id" target="_blank" class="footer-link">link-gr.id</a></div>""", unsafe_allow_html=True)



