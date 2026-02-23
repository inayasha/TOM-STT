import uuid
import streamlit as st
import streamlit.components.v1 as components
import speech_recognition as sr
import os
import subprocess
import math
import tempfile
import io
import requests
import re
from shutil import which

# Import Library AI, DOCX, & Firebase
import google.generativeai as genai
from groq import Groq
from docx import Document
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from firebase_admin import auth
from datetime import datetime
from streamlit_cookies_controller import CookieController

# ==========================================
# 1. SETUP & CONFIG
# ==========================================
st.set_page_config(page_title="TOM'STT AI", page_icon="🎙️", layout="centered", initial_sidebar_state="expanded")

cookie_manager = CookieController()

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
    user_ref = db.collection('users').document(username)
    existing_user = user_ref.get()
    
    if existing_user.exists:
        user_ref.update({"password": password, "role": role})
    else:
        user_ref.set({
            "password": password,
            "role": role,
            "inventori": [],           # FORMAT BARU: Rak Penyimpanan Array
            "saldo": 0,                
            "tanggal_expired": "Selamanya",
            "created_at": datetime.now()
        })

def delete_user(username):
    db.collection('users').document(username).delete()
    try:
        user_record = auth.get_user_by_email(username)
        auth.delete_user(user_record.uid)
    except: pass
    
def check_expired(username, user_data):
    """SATPAM: Mengecek kedaluwarsa & MIGRASI OTOMATIS data lama."""
    if not user_data or user_data.get("role") == "admin": return user_data 
    
    # 1. AUTO-MIGRASI DATA LAMA KE FORMAT INVENTORI
    if "paket_aktif" in user_data and "inventori" not in user_data:
        paket_lama = user_data.get("paket_aktif", "Freemium")
        kuota_lama = user_data.get("kuota", 0)
        batas_lama = user_data.get("batas_durasi", 10)
        
        inventori_baru = []
        if paket_lama != "Freemium" and kuota_lama > 0:
            inventori_baru.append({"nama": paket_lama, "kuota": kuota_lama, "batas_durasi": batas_lama})
        user_data["inventori"] = inventori_baru
    
    # 2. CEK KEDALUWARSA GLOBAL
    exp_val = user_data.get("tanggal_expired")
    if exp_val and exp_val != "Selamanya":
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            exp_date = datetime.datetime.fromisoformat(exp_val.replace("Z", "+00:00")) if isinstance(exp_val, str) else exp_val
            if now > exp_date:
                st.toast("⚠️ Masa aktif habis. Inventori & Saldo di-reset.", icon="🚨")
                db.collection('users').document(username).update({
                    "inventori": [], "saldo": 0, "tanggal_expired": firestore.DELETE_FIELD
                })
                user_data["inventori"] = []
                user_data["saldo"] = 0
                user_data.pop("tanggal_expired", None)
        except: pass
            
    return user_data
    
def hitung_estimasi_menit(teks):
    if not teks: return 0
    jumlah_kata = len(teks.split())
    durasi = math.ceil(jumlah_kata / 130)
    return durasi if durasi > 0 else 1

def cek_pembayaran(user_data, durasi_menit, index_paket):
    """Mengecek kesanggupan bayar berdasarkan pilihan Dropdown User."""
    if user_data.get("role") == "admin": return True, "Akses Admin (Gratis)", 0
        
    saldo = user_data.get("saldo", 0)
    inventori = user_data.get("inventori", [])
    TARIF = 350
    
    # Skenario 1: Bayar Pakai Saldo Murni
    if index_paket == -1:
        biaya = durasi_menit * TARIF
        if saldo >= biaya: return True, f"Saldo terpotong Rp {biaya:,}", biaya
        else: return False, f"Saldo kurang. Butuh Rp {biaya:,}", 0
    
    # Skenario 2: Bayar Pakai Inventori Paket + Subsidi Silang
    if 0 <= index_paket < len(inventori):
        paket = inventori[index_paket]
        batas = paket.get("batas_durasi", 10)
        
        if durasi_menit <= batas:
            return True, f"1 Kuota '{paket['nama']}' Terpakai.", 0
        else:
            biaya_subsidi = (durasi_menit - batas) * TARIF
            if saldo >= biaya_subsidi: return True, f"1 Kuota '{paket['nama']}' + Saldo Rp {biaya_subsidi:,} terpakai.", biaya_subsidi
            else: return False, f"Saldo kurang untuk bayar kelebihan waktu (Butuh Rp {biaya_subsidi:,}).", 0
            
    return False, "Sistem Gagal Membaca Paket.", 0

def eksekusi_pembayaran(username, user_data, index_paket, potong_saldo):
    """Mengeksekusi pemotongan di Firebase secara presisi pada array inventori."""
    if user_data.get("role") == "admin": return 
    
    user_ref = db.collection('users').document(username)
    updates = {"saldo": firestore.Increment(-potong_saldo)}
    
    if index_paket != -1:
        inventori = user_data.get("inventori", [])
        if 0 <= index_paket < len(inventori):
            inventori[index_paket]["kuota"] -= 1
            if inventori[index_paket]["kuota"] <= 0:
                inventori.pop(index_paket) # Buang paket dari rak jika kuota habis
            updates["inventori"] = inventori # Simpan rak baru ke Firebase
            
    user_ref.update(updates)
    
def redeem_voucher(username, kode_voucher):
    """Mengecek dan mengeksekusi voucher dengan aman, menambah masa aktif max 90 hari, dan memberikan BONUS SALDO."""
    kode_voucher = kode_voucher.upper().strip()
    v_ref = db.collection('vouchers').document(kode_voucher)
    v_doc = v_ref.get()
    
    if not v_doc.exists:
        return False, "❌ Voucher tidak ditemukan atau salah ketik."
        
    v_data = v_doc.to_dict()
    
    # 1. Cek Kuota & Riwayat (Sistem Anti-Curang)
    if v_data.get('jumlah_terklaim', 0) >= v_data.get('max_klaim', 1):
        return False, "❌ Kuota klaim voucher ini sudah habis."
    if username in v_data.get('riwayat_pengguna', []):
        return False, "❌ Anda sudah pernah mengklaim voucher ini."
        
    user_ref = db.collection('users').document(username)
    
    # 2. Transaksi Aman 
    @firestore.transactional
    def eksekusi_klaim(transaction, user_ref, v_ref):
        u_snap = user_ref.get(transaction=transaction)
        u_data = u_snap.to_dict()
        v_latest = v_ref.get(transaction=transaction).to_dict()
        
        # Ambil Saldo Saat Ini
        current_saldo = u_data.get("saldo", 0)
        
        # Hitung Tanggal Expired
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        current_exp = u_data.get("tanggal_expired")
        
        if current_exp and current_exp != "Selamanya":
            try:
                exp_date = current_exp if not isinstance(current_exp, str) else datetime.datetime.fromisoformat(current_exp.replace("Z", "+00:00"))
                base_date = now if exp_date < now else exp_date
            except: base_date = now
        else:
            base_date = now
            
        # Tentukan tambahan hari & BONUS SALDO sesuai paket
        hari_tambah = 14
        bonus_saldo = 0  # Starter tidak dapat bonus
        
        if "Pro" in v_latest['nama_paket']: 
            hari_tambah = 30
            bonus_saldo = 5000
        elif "Eksekutif" in v_latest['nama_paket']: 
            hari_tambah = 45
            bonus_saldo = 12000
        elif "VIP" in v_latest['nama_paket']: 
            hari_tambah = 60
            bonus_saldo = 20000
        
        # Kalkulasi Expired (Maks 90 Hari)
        new_exp_date = base_date + datetime.timedelta(days=hari_tambah)
        max_exp_date = now + datetime.timedelta(days=90)
        if new_exp_date > max_exp_date: new_exp_date = max_exp_date 
            
        # Suntikkan Paket ke Array Inventori
        inventori = u_data.get("inventori", [])
        ditemukan = False
        for pkt in inventori:
            if pkt['nama'] == v_latest['nama_paket'] and pkt['batas_durasi'] == v_latest['batas_durasi']:
                pkt['kuota'] += v_latest['kuota_paket']
                ditemukan = True
                break
        if not ditemukan:
            inventori.append({"nama": v_latest['nama_paket'], "kuota": v_latest['kuota_paket'], "batas_durasi": v_latest['batas_durasi']})
            
        # Eksekusi Pembaruan Database (Tambahkan Inventori, Expired, dan SALDO BARU)
        new_saldo = current_saldo + bonus_saldo
        transaction.update(user_ref, {
            "inventori": inventori, 
            "tanggal_expired": new_exp_date,
            "saldo": new_saldo
        })
        transaction.update(v_ref, {"jumlah_terklaim": firestore.Increment(1), "riwayat_pengguna": firestore.ArrayUnion([username])})
        
        # Tampilkan pesan sukses dengan nominal bonus
        pesan_sukses = f"🎉 Paket {v_latest['nama_paket']} + Bonus Saldo Rp {bonus_saldo:,} berhasil ditambahkan!"
        return True, pesan_sukses.replace(',', '.')
        
    transaction = db.transaction()
    try:
        success, msg = eksekusi_klaim(transaction, user_ref, v_ref)
        return success, msg
    except Exception as e:
        return False, f"Terjadi kesalahan sistem: {str(e)}"
        
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

# --- SISTEM AUTO-LOGIN (BUFFER ANTI-LOGOUT) ---
if not st.session_state.logged_in:
    if 'retry_cookie' not in st.session_state:
        st.session_state.retry_cookie = 0
        
    saved_user = None
    try:
        saved_user = cookie_manager.get('tomstt_session')
    except Exception:
        pass
    
    if saved_user:
        user_data = get_user(saved_user)
        if user_data: 
            st.session_state.logged_in = True
            st.session_state.current_user = saved_user
            st.session_state.user_role = user_data.get("role", "user")
            
            # --- INJEKSI DRAFT KE MEMORI SEBELUM REFRESH UI ---
            st.session_state.transcript = user_data.get("draft_transcript", "")
            st.session_state.filename = user_data.get("draft_filename", "Hasil_STT")
            st.session_state.ai_result = user_data.get("draft_ai_result", "")
            st.session_state.ai_prefix = user_data.get("draft_ai_prefix", "")
            
            st.session_state.retry_cookie = 0 
            st.rerun()
    else:
        # Jika cookie kosong/belum siap, tunggu 0.5 detik dan paksa cek ulang (Maks 3x percobaan)
        if st.session_state.retry_cookie < 3:
            import time
            st.session_state.retry_cookie += 1
            time.sleep(0.5)
            st.rerun()

# --- PENGAMANAN DRAFT (RESTORASI GLOBAL SAAT LOGIN MANUAL) ---
if st.session_state.logged_in and not st.session_state.transcript and not st.session_state.ai_result:
    user_info = get_user(st.session_state.current_user)
    if user_info and ("draft_transcript" in user_info or "draft_ai_result" in user_info):
        st.session_state.transcript = user_info.get("draft_transcript", "")
        st.session_state.filename = user_info.get("draft_filename", "Hasil_STT")
        st.session_state.ai_result = user_info.get("draft_ai_result", "")
        st.session_state.ai_prefix = user_info.get("draft_ai_prefix", "")

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
    .mobile-warning-box { background-color: #fff8e1; color: #b78103; padding: 12px 15px; border-radius: 10px; border-left: 5px solid #ffc107; font-size: 0.9rem; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); text-align: left; }
    .mobile-warning-box b { color: #8f6200; }
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
    /* Fix label Role di Form Admin agar rata kiri */
    div[data-testid="stForm"] div[data-testid="stSelectbox"] label { width: auto !important; text-align: left !important; display: block !important; margin-bottom: 8px !important; }
    
    /* Tombol Hapus bergaya teks link merah (Tertiary) */
    div.stButton > button[kind="tertiary"] {
        background-color: transparent !important;
        color: #e74c3c !important;
        border: none !important;
        padding: 0 !important;
        font-weight: 700 !important;
        box-shadow: none !important;
        width: auto !important;
        transform: none !important;
        justify-content: flex-start !important;
    }
    div.stButton > button[kind="tertiary"] p { color: #e74c3c !important; font-size: 15px !important; }
    div.stButton > button[kind="tertiary"]:hover {
        background-color: transparent !important;
        color: #c0392b !important;
        text-decoration: underline !important;
    }
    div.stButton > button[kind="tertiary"]:hover p { color: #c0392b !important; }
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
    from docx import Document
    from docx.shared import Pt  # Modul tambahan untuk mengatur jarak spasi (indentasi)
    
    doc = Document()
    doc.add_heading(title, level=1)
    
    for line in text.split('\n'):
        # Abaikan baris yang kosong
        if not line.strip(): 
            continue
            
        # 1. Deteksi Garis Pembatas Markdown (---)
        if re.match(r'^\s*---\s*$', line):
            doc.add_paragraph("_" * 50)
            continue
        
        # 2. Deteksi Heading Markdown (#, ##, ###)
        heading_match = re.match(r'^(#+)\s+(.*)', line.strip())
        if heading_match:
            level = len(heading_match.group(1))
            doc.add_heading(heading_match.group(2), level=min(level, 9))
            continue
            
        # 3. Deteksi Bullet Point (*, -, atau +) dan indentasi
        bullet_match = re.match(r'^(\s*)[\*\-\+]\s+(.*)', line)
        
        # 4. Deteksi Numbering & Alphabet (1., 12., A., B., a., b.)
        number_match = re.match(r'^(\s*)([A-Za-z0-9]+[\.\)])\s+(.*)', line)
        
        p = None
        if bullet_match:
            indent_spaces = len(bullet_match.group(1))
            try:
                # Gunakan List Bullet 2 jika menjorok ke dalam
                style_name = 'List Bullet 2' if indent_spaces >= 2 else 'List Bullet'
                p = doc.add_paragraph(style=style_name)
            except:
                p = doc.add_paragraph(style='List Bullet')
            line_content = bullet_match.group(2)
            
        elif number_match:
            indent_spaces = len(number_match.group(1))
            
            # KUNCI PERBAIKAN: Gunakan paragraf normal agar angka tidak di-reset oleh Word menjadi (1, 1, 1)
            p = doc.add_paragraph()
            
            # Berikan efek menjorok ke dalam (indentasi) jika ini adalah sub-list
            if indent_spaces > 0:
                try:
                    p.paragraph_format.left_indent = Pt(18) 
                except: pass
                
            # KEMBALIKAN angka/huruf aslinya (Misal: "11. " atau "A. ") ke dalam teks
            line_content = number_match.group(2) + " " + number_match.group(3)
            
        else:
            # Teks Paragraf Biasa
            p = doc.add_paragraph()
            line_content = line.strip()
            
        # 5. PARSING INLINE CANGGIH (Bold & Italic) dengan "Regex Tokenizer"
        tokens = re.split(r'(\*\*.*?\*\*|\*.*?\*)', line_content)
        
        for token in tokens:
            if not token: 
                continue
                
            if token.startswith('**') and token.endswith('**') and len(token) > 4:
                # Cetak Tebal
                run = p.add_run(token[2:-2])
                run.bold = True
            elif token.startswith('*') and token.endswith('*') and len(token) > 2:
                # Cetak Miring
                run = p.add_run(token[1:-1])
                run.italic = True
            else:
                # Teks Normal
                p.add_run(token)

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
            "name": f"Paket {nama_paket} "
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
    
    tab_paket, tab_saldo = st.tabs(["📦 BELI PAKET KUOTA", "💳 TOP-UP SALDO"])
    
    with tab_paket:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("""
            **1. Paket Starter**
            *Cocok untuk kebutuhan personal & tugas ringan.*
            * 📄 **2x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 45 Menit / File
            * 📅 **Masa Aktif:** 14 Hari
            * 🎁 **Bonus Saldo:** Rp 0
            * 🛡️ **Akses:** Server API Terjamin
            """)
            if st.button("🛒 Beli Starter - Rp 51.000", use_container_width=True, key="buy_starter"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Starter", 51000, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar (Termasuk 2% Biaya Layanan)", link_bayar, use_container_width=True)
            
            st.markdown("---")
            
            st.markdown("""
            **2. Paket Pro Notulis**
            *Standar profesional untuk notulis & sekretaris.*
            * 📄 **5x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 1 Jam / File
            * 📅 **Masa Aktif:** 30 Hari
            * 🎁 **Bonus Saldo:** Rp 5.000
            * 🛡️ **Akses:** Server API Terjamin
            """)
            if st.button("🛒 Beli Pro - Rp 102.000", use_container_width=True, key="buy_pro"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Pro", 102000, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar (Termasuk 2% Biaya Layanan)", link_bayar, use_container_width=True)

        with col2:
            st.markdown("""
            **3. Paket Eksekutif**
            *Pilihan tepat untuk intensitas rapat tinggi.*
            * 📄 **18x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 1,5 Jam / File
            * 📅 **Masa Aktif:** 45 Hari
            * 🎁 **Bonus Saldo:** Rp 12.000
            * 🛡️ **Jaminan Akses:** Multi-Server API (Anti-Limit)
            """)
            if st.button("🛒 Beli Eksekutif - Rp 306.000", use_container_width=True, key="buy_exec"):
                with st.spinner("Mencetak tagihan..."):
                    link_bayar = buat_tagihan_midtrans("Eksekutif", 306000, user_email)
                    if link_bayar: st.link_button("💳 Lanjut Bayar (Termasuk 2% Biaya Layanan)", link_bayar, use_container_width=True)
            
            st.markdown("---")
            
            st.markdown("""
            **4. Paket VIP**
            *Akses maksimal untuk kementerian/instansi.*
            * 📄 **35x** Ekstrak AI (Laporan/Notulen)
            * ⏱️ **Kapasitas:** Maks. 3 Jam / File
            * 📅 **Masa Aktif:** 60 Hari
            * 🎁 **Bonus Saldo:** Rp 20.000
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
                    
    # KOTAK REDEEM VOUCHER
    st.markdown("---")
    col_v1, col_v2 = st.columns([3, 1])
    with col_v1:
        input_voucher = st.text_input("🎁 Punya Kode Voucher / Promo?", placeholder="Masukkan kode di sini...", key="input_vc").strip().upper()
    with col_v2:
        st.write("") 
        if st.button("Klaim Voucher", use_container_width=True, type="primary"):
            if input_voucher:
                with st.spinner("Memeriksa kode..."):
                    sukses, pesan = redeem_voucher(user_email, input_voucher)
                    if sukses:
                        st.success(pesan)
                        st.balloons()
                    else:
                        st.error(pesan)
            else:
                st.warning("Silakan masukkan kode terlebih dahulu.")
    st.markdown("---")

    # KOTAK INFO PINDAH KE BAWAH
    st.markdown("""
    > ⚡ **Investasi Waktu Terbaik Anda**
    > Dapatkan kembali waktu istirahat Anda. Biarkan AI kami yang bekerja keras menyusun laporan rumit hanya dengan biaya setara segelas kopi per dokumen!
    """)
    st.info("""
    💡 **Informasi Sistem & Ketentuan:**
    * 🎟️ **Sistem Tiket:** 1 Kuota = 1x Pembuatan Dokumen (Laporan/Notulen).
    * ⚖️ **Tagihan Adil (Deteksi Jeda):** Durasi dihitung berdasarkan **jumlah kata aktual** yang diucapkan, BUKAN total waktu rekaman mentah. Waktu hening & jeda tidak memotong kuota Anda.
    * 📅 **Akumulasi Masa Aktif:** Pembelian paket baru otomatis menambah sisa masa aktif Anda *(Maksimal 90 Hari / 3 Bulan)*.
    * 💳 **Saldo Tambahan:** Jika rekaman melebihi batas maksimal, sistem menggunakan Saldo Utama dengan tarif **Rp 350 / Menit**.
    """)
                    
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
                inventori = user_data.get("inventori", [])
                saldo = user_data.get("saldo", 0)
                exp_val = user_data.get("tanggal_expired")
                
                # Menampilkan Rak Inventori (Format Baru & Lebih Rapi)
                st.markdown("📦 **Inventori Paket Anda:**")
                if not inventori:
                    st.markdown("<span style='color:#e74c3c;'><i>Belum ada paket aktif.</i></span>", unsafe_allow_html=True)
                else:
                    for pkt in inventori:
                        # Menurunkan batas menit ke baris baru agar lebih lega
                        st.markdown(f"**{pkt['nama']} : {pkt['kuota']}x**<br><span style='color:#666; font-size:14px;'>(Maks. {pkt['batas_durasi']} menit per Kuota)</span>", unsafe_allow_html=True)
                
                # Format Tanggal Expired Global
                status_waktu = "⏳ **Berlaku hingga:** Selamanya"
                if exp_val and exp_val != "Selamanya":
                    import datetime
                    try:
                        exp_date = datetime.datetime.fromisoformat(exp_val.replace("Z", "+00:00")) if isinstance(exp_val, str) else exp_val
                        status_waktu = f"⏳ **Berlaku hingga:** {exp_date.strftime('%d %b %Y')}"
                    except: pass
                
                st.write("")
                st.markdown(status_waktu)
                st.markdown("---")
                
                estimasi_menit = math.floor(saldo / 350)
                saldo_rp = f"Rp {saldo:,}".replace(",", ".")
                st.metric("💳 Saldo Utama", saldo_rp)
                st.caption(f"*(Melindungi ± {estimasi_menit} Menit kelebihan durasi)*")
                
                # MENGEMBALIKAN KEDUA TOMBOL YANG HILANG
                st.write("")
                if st.button("⚡ Refresh Dompet", use_container_width=True):
                    st.rerun()
                if st.button("🛒 Beli Paket / Top-Up", use_container_width=True):
                    show_pricing_dialog()
                    
            st.markdown("---")
            
        if st.session_state.user_role == "admin": 
            st.info("👑 Anda Administrator.")
            
        if st.button("🚪 Logout", use_container_width=True):
            cookie_manager.remove('tomstt_session') # HAPUS COOKIE DARI HP
            st.session_state.logged_in, st.session_state.current_user, st.session_state.user_role = False, "", ""
            st.session_state.ai_result = ""
            st.rerun()
    else:
        st.caption("Silakan login di Tab 🔐 Akun.")

# ==========================================
# 4. MAIN LAYOUT & TABS
# ==========================================
st.markdown(
    "<div class='main-header'>🎙️ TOM'<font color='#e74c3c'>STT</font> AI</div>", 
    unsafe_allow_html=True
)

# KOTAK SELAMAT DATANG (COPYWRITING BARU)
st.info("""
🚀 **Otomatisasi Notulen & Laporan dalam Hitungan Menit** Mengubah rekaman rapat berjam-jam menjadi teks manual bisa menyita 1-2 hari kerja Anda. Dengan mesin AI , semuanya selesai secara instan!

🧠 **Bukan Sekadar Transkrip Biasa:** AI kami telah diprogram khusus untuk langsung mengekstrak **Notulen Rapat** atau **Laporan** siap cetak—lengkap dengan latar belakang, analisis, dan tindak lanjut berstandar profesional.
""")

# --- FITUR WAKE LOCK (ANTI-LAYAR MATI) ---
components.html(
    """
    <script>
    async function requestWakeLock() {
        try {
            if ('wakeLock' in navigator) {
                const wakeLock = await navigator.wakeLock.request('screen');
                console.log('Wake Lock aktif: Layar tidak akan mati.');
                document.addEventListener('visibilitychange', async () => {
                    if (document.visibilityState === 'visible') {
                        await navigator.wakeLock.request('screen');
                    }
                });
            }
        } catch (err) {
            console.log('Wake Lock error: ' + err.message);
        }
    }
    requestWakeLock();
    </script>
    """,
    height=0, width=0
)

tab_titles = ["📂 Upload File", "🎙️ Rekam Suara", "✨ Ekstrak AI", "🔐 Akun"]
if st.session_state.user_role == "admin": tab_titles.append("⚙️ Panel Admin")
tabs = st.tabs(tab_titles)
tab_upload, tab_rekam, tab_ai, tab_auth = tabs[0], tabs[1], tabs[2], tabs[3]

audio_to_process, source_name = None, "audio"
submit_btn = False
lang_code = "id-ID"

def show_mobile_warning():
    st.markdown("""
    <div class="mobile-warning-box">
        📱 <b>Peringatan untuk Pengguna HP:</b><br>
        Harap biarkan layar tetap menyala dan <b>jangan berpindah ke aplikasi lain</b> (seperti WA/IG) selama proses berjalan agar sistem tidak terputus di tengah jalan.
    </div>
    """, unsafe_allow_html=True)

# TAB 1: UPLOAD FILE (Bebas Akses)
with tab_upload:
    # 1. Tentukan Limitasi Berdasarkan Status Login & Paket
    limit_mb = 10 # Default Freemium (Belum login / Paket Freemium)
    if st.session_state.logged_in:
        user_info = get_user(st.session_state.current_user)
        if user_info:
            role = user_info.get("role", "user")
            # Logika Baru: Cek apakah rak inventori ada isinya
            inventori = user_info.get("inventori", [])
            
            if role == "admin" or len(inventori) > 0:
                limit_mb = 200 # Premium / Admin mendapat 200MB
    
    # 2. Teks Edukasi Transparan
    st.markdown(f"<p style='text-align: center; color: #666; font-size: 14px; margin-bottom: 10px;'>Batas ukuran file: <b>10MB</b> (Freemium) | <b>200MB</b> (Premium)</p>", unsafe_allow_html=True)
    
    uploaded_file = st.file_uploader("Pilih File Audio", type=["aac", "mp3", "wav", "m4a", "opus", "mp4", "3gp", "amr", "ogg", "flac", "wma"])
    
    # 3. Sistem Pencegat (Interceptor)
    file_diizinkan = False
    if uploaded_file:
        file_size_mb = uploaded_file.size / (1024 * 1024)
        if file_size_mb > limit_mb:
            st.error(f"❌ File terlalu besar! ({file_size_mb:.1f} MB). Batas akun Anda saat ini adalah {limit_mb} MB.")
            if limit_mb == 10:
                st.warning("💡 Silakan login dan Beli Paket di tab **🔐 Akun** untuk mengunggah file hingga 200MB.")
        else:
            audio_to_process, source_name = uploaded_file, uploaded_file.name
            file_diizinkan = True
    
    st.write("") 
    c1, c2, c3 = st.columns([1, 4, 1]) 
    with c2:
        lang_choice_upload = st.selectbox("Pilih Bahasa Audio", ("Indonesia", "Inggris"), key="lang_up")
        st.write("") 
        if file_diizinkan: # Tombol Mulai HANYA muncul jika file lolos limit
            show_mobile_warning()
            if st.button("🚀 Mulai Transkrip", use_container_width=True, key="btn_up"):
                submit_btn = True
                lang_code = "id-ID" if lang_choice_upload == "Indonesia" else "en-US"
        elif not uploaded_file:
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
                show_mobile_warning()
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
        # CHECKPOINT 1: Simpan Transkrip ke Firebase (Auto-Save)
        if st.session_state.logged_in:
            db.collection('users').document(st.session_state.current_user).update({
                "draft_transcript": st.session_state.transcript,
                "draft_filename": st.session_state.filename,
                "draft_ai_result": "",
                "draft_ai_prefix": ""
            })
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
                            
                            cookie_manager.set('tomstt_session', login_email, max_age=30*86400)
                                
                            st.session_state.logged_in = True
                            st.session_state.current_user = login_email
                            st.session_state.user_role = user_data.get("role", "user")
                            st.rerun()
                    else:
                        err = res.get("error", {}).get("message", "Gagal")
                        if err == "INVALID_LOGIN_CREDENTIALS": st.error("❌ Email atau Password salah!")
                        else: st.error(f"❌ Akses Ditolak: {err}")
            
            # --- FITUR LUPA PASSWORD (SEKARANG SEJAJAR & DI LUAR TOMBOL LOGIN) ---
            st.write("")
            with st.expander("Lupa Password?"):
                st.caption("Masukkan email terdaftar Anda di bawah ini. Kami akan mengirimkan tautan aman untuk membuat password baru.")
                reset_email = st.text_input("Email untuk Reset", key="reset_email").strip()
                
                if st.button("Kirim Link Reset Password", use_container_width=True):
                    if reset_email:
                        with st.spinner("Mengirim tautan..."):
                            api_key = st.secrets["firebase_web_api_key"]
                            url_reset = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}"
                            payload = {"requestType": "PASSWORD_RESET", "email": reset_email}
                            
                            res_reset = requests.post(url_reset, json=payload).json()
                            
                            if "email" in res_reset:
                                st.success("✅ Tautan reset password berhasil dikirim! Silakan periksa kotak masuk (Inbox) atau folder Spam pada email Anda.")
                            else:
                                err_msg = res_reset.get("error", {}).get("message", "Gagal")
                                if err_msg == "EMAIL_NOT_FOUND":
                                    st.error("❌ Email tersebut tidak ditemukan atau belum terdaftar di sistem kami.")
                                else:
                                    st.error(f"❌ Gagal mengirim tautan: {err_msg}")
                    else:
                        st.warning("⚠️ Silakan ketik alamat email Anda terlebih dahulu.")
                        
        # --- TAB REGISTER MANDIRI ---
        with auth_tab2:
            reg_email = st.text_input("Email Aktif", key="reg_email").strip()
            reg_pwd = st.text_input("Buat Password (Min. 6 Karakter)", type="password", key="reg_pwd")
            reg_pwd_confirm = st.text_input("Ulangi Password", type="password", key="reg_pwd_confirm")
            
            if st.button("🎁 Daftar & Klaim Kuota Gratis", use_container_width=True):
                if not reg_email:
                    st.error("❌ Email tidak boleh kosong!")
                elif len(reg_pwd) < 6:
                    st.error("❌ Password terlalu pendek. Minimal 6 karakter!")
                elif reg_pwd != reg_pwd_confirm:
                    st.error("❌ Konfirmasi password tidak cocok! Silakan periksa kembali ketikan Anda.")
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
        # HEADER PROFIL PREMIUM (Email Diperkecil & Bold)
        st.markdown(f"""
        <div style="text-align: center; padding: 15px 0;">
            <p style="color: #666; font-size: 15px; margin-bottom: 5px;">Anda saat ini masuk sebagai:</p>
            <div style="font-size: 24px;"><b><font color="#e74c3c">{st.session_state.current_user}</font></b></div>
        </div>
        """, unsafe_allow_html=True)
        
        st.markdown("---")
        
        # DASBOR NILAI PLUS (Ikon Rata Tengah Sempurna)
        col_vp1, col_vp2, col_vp3 = st.columns(3)
        with col_vp1:
            st.markdown("<div style='text-align: center; padding: 5px;'><div style='font-size: 35px; margin-bottom: 8px;'>⚡</div><b style='color:#111; font-size: 16px;'>Hemat Waktu</b><br><div style='font-size:14px; color:#555; margin-top: 4px;'>Selesai dalam hitungan menit, bukan berhari-hari.</div></div>", unsafe_allow_html=True)
        with col_vp2:
            st.markdown("<div style='text-align: center; padding: 5px;'><div style='font-size: 35px; margin-bottom: 8px;'>🧠</div><b style='color:#111; font-size: 16px;'>AI Pintar</b><br><div style='font-size:14px; color:#555; margin-top: 4px;'>Otomatis susun Laporan & Notulen siap cetak.</div></div>", unsafe_allow_html=True)
        with col_vp3:
            st.markdown("<div style='text-align: center; padding: 5px;'><div style='font-size: 35px; margin-bottom: 8px;'>⚖️</div><b style='color:#111; font-size: 16px;'>Sistem Adil</b><br><div style='font-size:14px; color:#555; margin-top: 4px;'>Jeda hening tidak memotong tagihan kuota Anda.</div></div>", unsafe_allow_html=True)
        
        st.write("")

with tab_ai:
    if not st.session_state.logged_in:
        st.markdown('<div style="text-align: center; padding: 20px; background-color: #fdeced; border-radius: 10px; border: 1px solid #f5c6cb; margin-bottom: 20px;"><h3 style="color: #e74c3c; margin-top: 0;">🔒 Akses Terkunci!</h3><p style="color: #e74c3c; font-weight: 500;">Silakan masuk (login) atau daftar terlebih dahulu di tab <b>🔐 Akun</b> untuk menggunakan fitur AI.</p></div>', unsafe_allow_html=True)
    else:
        user_info = get_user(st.session_state.current_user)
        
        if not st.session_state.transcript:
            st.markdown('<div class="custom-info-box">👆 Transkrip belum tersedia.<br><strong>ATAU</strong> Unggah file .txt di bawah ini:</div>', unsafe_allow_html=True)
            uploaded_txt = st.file_uploader("Upload File Transkrip (.txt)", type=["txt"])
            
            if uploaded_txt:
                st.session_state.transcript, st.session_state.filename = uploaded_txt.read().decode("utf-8"), os.path.splitext(uploaded_txt.name)[0]
                st.session_state.ai_result = "" 
                
                # KUNCI PERBAIKAN: Simpan ke Firebase agar tidak hilang saat di-refresh!
                if st.session_state.logged_in:
                    db.collection('users').document(st.session_state.current_user).update({
                        "draft_transcript": st.session_state.transcript,
                        "draft_filename": st.session_state.filename,
                        "draft_ai_result": "",
                        "draft_ai_prefix": ""
                    })
                st.rerun()
        else:
            st.success("✅ Teks Transkrip Siap Diproses!")
            st.text_area("📄 Teks Saat Ini:", st.session_state.transcript, height=150, disabled=True)
            if st.button("🗑️ Hapus Teks"): 
                st.session_state.transcript, st.session_state.ai_result = "", "" 
                if user_info:
                    db.collection('users').document(st.session_state.current_user).update({"draft_transcript": "", "draft_ai_result": ""})
                st.rerun()
                
            st.write("")
            st.markdown("#### ⚙️ Pilih Mesin AI")
            engine_choice = st.radio("Silakan pilih AI yang ingin digunakan:", ["Gemini", "Groq"])
            
            # --- UI KENDALI TAGIHAN & SUBSIDI SILANG ---
            durasi_teks = hitung_estimasi_menit(st.session_state.transcript)
            jumlah_kata = len(st.session_state.transcript.split())
            
            user_info = get_user(st.session_state.current_user)
            user_info = check_expired(st.session_state.current_user, user_info) # Pastikan migrasi berjalan
            
            st.info(f"📊 **Analisis Teks:** Dokumen Anda memiliki **{jumlah_kata:,} Kata** (Setara dengan **± {durasi_teks} Menit** pemrosesan AI).")
            st.write("")
            
            # MEMBUAT DROPDOWN OPSI PEMBAYARAN
            pilihan_paket_dict = {}
            if user_info and user_info.get("role") != "admin":
                st.markdown("#### 💳 Pilih Metode Pembayaran")
                inventori = user_info.get("inventori", [])
                saldo = user_info.get("saldo", 0)
                opsi_list = []
                
                # Looping Isi Inventori User
                for i, pkt in enumerate(inventori):
                    batas = pkt["batas_durasi"]
                    if durasi_teks <= batas:
                        teks_opsi = f"🎟️ Paket {pkt['nama']} (Maks {batas}m) - Saldo Aman!"
                    else:
                        biaya_lebih = (durasi_teks - batas) * 350
                        ket = "✅ Cukup" if saldo >= biaya_lebih else f"❌ Saldo Kurang Rp {biaya_lebih - saldo:,}"
                        teks_opsi = f"🎟️ Paket {pkt['nama']} (Maks {batas}m) + Potong Saldo Rp {biaya_lebih:,} ({ket})"
                    
                    opsi_list.append(teks_opsi)
                    pilihan_paket_dict[teks_opsi] = i
                
                # Opsi Terakhir: Bayar Murni Pakai Saldo
                biaya_murni = durasi_teks * 350
                ket_murni = "✅ Cukup" if saldo >= biaya_murni else f"❌ Saldo Kurang Rp {biaya_murni - saldo:,}"
                opsi_saldo = f"💳 Gunakan Saldo Murni (Biaya Rp {biaya_murni:,} - {ket_murni})"
                opsi_list.append(opsi_saldo)
                pilihan_paket_dict[opsi_saldo] = -1
                
                # Tampilkan Dropdown/Radio ke Layar
                selected_opsi_teks = st.radio("Pilih aset dompet yang ingin digunakan untuk dokumen ini:", opsi_list)
                selected_index_paket = pilihan_paket_dict[selected_opsi_teks]
            else:
                selected_index_paket = -1
                st.info("👑 Anda menggunakan akses Super Admin (Gratis tanpa batas).")
                
            st.write("")
            show_mobile_warning()
            
            col1, col2 = st.columns(2)
            with col1: btn_notulen = st.button("📝 Buat Notulen", use_container_width=True)
            with col2: btn_laporan = st.button("📋 Buat Laporan", use_container_width=True)

            if btn_notulen or btn_laporan:
                # 1. CEK BIAYA BERDASARKAN PILIHAN USER
                bisa_bayar, pesan_bayar, p_saldo = cek_pembayaran(user_info, durasi_teks, selected_index_paket)
                
                if not bisa_bayar:
                    st.error(f"❌ TRANSAKSI DITOLAK: {pesan_bayar}")
                    st.warning("💡 Silakan pilih metode pembayaran lain, atau Top-Up Saldo Anda.")
                else:
                    # 2. LANJUT PROSES AI JIKA SALDO/KUOTA CUKUP
                    prompt_active = PROMPT_NOTULEN if btn_notulen else PROMPT_LAPORAN
                    ai_result = None
                    
                    active_keys = get_active_keys(engine_choice)
                    
                    if not active_keys:
                        st.error(f"❌ Sistem Sibuk: Tidak ada API Key {engine_choice} yang aktif. Saldo/Kuota Anda AMAN (Tidak dipotong).")
                    else:
                        success_generation = False
                        
                        # --- 1. MUNCULKAN LAYAR LOADING MEGAH (OVERLAY) ---
                        loading_overlay = st.empty()
                        loading_overlay.markdown(f"""
                        <style>
                        .loading-screen {{
                            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
                            background-color: rgba(255, 255, 255, 0.92);
                            display: flex; flex-direction: column; justify-content: center; align-items: center;
                            z-index: 999999; backdrop-filter: blur(8px);
                        }}
                        .spinner-large {{
                            width: 80px; height: 80px; border: 8px solid #F0F2F6; border-top: 8px solid #e74c3c;
                            border-radius: 50%; animation: spin-large 1s linear infinite; margin-bottom: 25px;
                            box-shadow: 0 4px 15px rgba(231, 76, 60, 0.2);
                        }}
                        @keyframes spin-large {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
                        .loading-title {{ font-size: 24px; font-weight: 800; color: #111; margin-bottom: 10px; text-align: center; }}
                        .loading-subtitle {{ font-size: 15px; color: #666; font-weight: 500; text-align: center; padding: 0 20px; }}
                        </style>
                        <div class="loading-screen">
                            <div class="spinner-large"></div>
                            <div class="loading-title">🚀 AI Sedang Bekerja...</div>
                            <div class="loading-subtitle">Memproses dengan {engine_choice} (Beban: {durasi_teks} Menit).<br>Mohon jangan tutup atau keluar dari halaman ini.</div>
                        </div>
                        """, unsafe_allow_html=True)
                        
                        # --- 2. JALANKAN PROSES AI (DI BALIK LAYAR) ---
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
                                
                        # --- 3. HAPUS LAYAR LOADING SETELAH AI SELESAI ---
                        loading_overlay.empty()
                        
                        if success_generation and ai_result:
                            # 3. POTONG SALDO & INVENTORI KARENA BERHASIL!
                            eksekusi_pembayaran(st.session_state.current_user, user_info, selected_index_paket, p_saldo)
                            
                            st.session_state.ai_result = ai_result
                            st.session_state.ai_prefix = "Notulen_" if btn_notulen else "Laporan_"
                            
                            # CHECKPOINT 2: Simpan Hasil AI ke Firebase
                            db.collection('users').document(st.session_state.current_user).update({
                                "draft_transcript": st.session_state.transcript, # PASTIKAN TEKS JUGA IKUT DISIMPAN BERSAMAAN
                                "draft_filename": st.session_state.filename,
                                "draft_ai_result": st.session_state.ai_result,
                                "draft_ai_prefix": st.session_state.ai_prefix
                            })
                            
                            st.success(f"✅ **Proses Selesai!** {pesan_bayar}")
                        elif not success_generation:
                            st.error("❌ Gagal memproses. Server API sedang gangguan. Saldo & Kuota Anda AMAN (Tidak dipotong).")

            if st.session_state.ai_result:
                st.markdown("---")
                st.markdown("### ✨ Hasil Ekstrak AI")
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
            
        # --- GENERATOR VOUCHER ---
        st.markdown("#### 🎫 Generator Voucher Promo / B2B")
        st.caption("Buat kode akses untuk diberikan secara manual kepada instansi/klien atau sebagai promo gratis.")
        
        with st.expander("➕ Buat Voucher Baru"):
            with st.form("form_voucher"):
                v_paket = st.selectbox("Pilih Paket yang Diberikan", ["Starter", "Pro Notulis", "Eksekutif", "VIP"])
                v_kode = st.text_input("Custom Kode Voucher (Kosongkan jika ingin dibuat acak otomatis)", placeholder="Contoh: BAPPEDA-VIP-01").strip().upper()
                
                col_t1, col_t2 = st.columns(2)
                with col_t1: v_tipe = st.radio("Tipe Voucher", ["Eksklusif (1x Pakai)", "Massal (Multi-Klaim)"])
                # FIX: Menghapus "disabled" agar admin bebas mengetik angka
                with col_t2: v_kuota_klaim = st.number_input("Batas Klaim (Khusus Massal)", min_value=1, value=10) 
                
                if st.form_submit_button("🔨 Generate Voucher"):
                    import random, string
                    if not v_kode: v_kode = "TOM-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                    
                    paket_map = {
                        "Starter": {"k": 2, "d": 45},
                        "Pro Notulis": {"k": 5, "d": 60},
                        "Eksekutif": {"k": 18, "d": 90},
                        "VIP": {"k": 35, "d": 180}
                    }
                    
                    max_k = 1 if v_tipe == "Eksklusif (1x Pakai)" else v_kuota_klaim
                    
                    if db.collection('vouchers').document(v_kode).get().exists:
                        st.error(f"❌ Kode '{v_kode}' sudah pernah dibuat! Silakan gunakan kode lain.")
                    else:
                        db.collection('vouchers').document(v_kode).set({
                            "kode_voucher": v_kode,
                            "nama_paket": v_paket,
                            "kuota_paket": paket_map[v_paket]["k"],
                            "batas_durasi": paket_map[v_paket]["d"],
                            "tipe": v_tipe,
                            "max_klaim": int(max_k),
                            "jumlah_terklaim": 0,
                            "riwayat_pengguna": [],
                            "created_at": firestore.SERVER_TIMESTAMP
                        })
                        st.success(f"✅ Berhasil! Kode Voucher: **{v_kode}** siap digunakan.")
                        st.rerun()

        st.write("")
        # Menampilkan Tabel/Daftar Voucher Aktif + Riwayat Penebus
        if st.checkbox("Lihat Daftar Voucher Aktif & Riwayat"):
            vouchers_ref = db.collection('vouchers').order_by('created_at', direction=firestore.Query.DESCENDING).limit(10).stream()
            for v in vouchers_ref:
                vd = v.to_dict()
                kode_v = vd.get('kode_voucher', v.id)
                sisa = vd['max_klaim'] - vd.get('jumlah_terklaim', 0)
                status_v = "🟢 AKTIF" if sisa > 0 else "🔴 HABIS"
                riwayat = vd.get('riwayat_pengguna', [])
                
                # Membagi kolom agar tombol sejajar dengan teks
                col_info, col_btn1, col_btn2 = st.columns([5, 1.5, 1.5])
                
                with col_info:
                    st.markdown(f"**{kode_v}** &nbsp;|&nbsp; Paket: {vd.get('nama_paket', '')} &nbsp;|&nbsp; Sisa Klaim: **{sisa}** &nbsp;|&nbsp; {status_v}")
                    if riwayat:
                        teks_riwayat = ", ".join(riwayat)
                        st.caption(f"👤 *Diklaim oleh: {teks_riwayat}*")
                
                with col_btn1:
                    # Hanya muncul jika voucher masih AKTIF
                    if sisa > 0: 
                        if st.button("Hapus Voucher", key=f"del_v_{kode_v}", type="tertiary"):
                            db.collection('vouchers').document(kode_v).delete()
                            st.rerun()
                
                with col_btn2:
                    if sisa == 0:
                        # Jika HABIS, tombol Hapus Log akan menghapus dokumen untuk bersih-bersih
                        if st.button("Hapus Log", key=f"del_log_habis_{kode_v}", type="tertiary"):
                            db.collection('vouchers').document(kode_v).delete()
                            st.rerun()
                    elif len(riwayat) > 0:
                        # Jika AKTIF tapi sudah ada yang pakai, Hapus Log akan MERESET riwayat klaimnya
                        if st.button("Hapus Log", key=f"del_log_aktif_{kode_v}", type="tertiary"):
                            db.collection('vouchers').document(kode_v).update({
                                "riwayat_pengguna": [],
                                "jumlah_terklaim": 0
                            })
                            st.rerun()
        st.markdown("---")
        
        # --- MANAJEMEN USER ---
        st.markdown("#### 👥 Manajemen User")
        
        # POP-UP KONFIRMASI HAPUS
        @st.dialog("⚠️ Konfirmasi Hapus Akun")
        def dialog_hapus_user(user_id):
            st.warning(f"Anda yakin ingin menghapus pengguna **{user_id}** secara permanen?")
            st.info("Tindakan ini akan menghapus dompet di Firestore dan akses login di Firebase Auth. Data tidak dapat dipulihkan.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("❌ Batal", use_container_width=True):
                    st.rerun()
            with c2:
                if st.button("🚨 Ya, Hapus!", use_container_width=True, key=f"confirm_{user_id}"):
                    delete_user(user_id)
                    st.toast(f"✅ User {user_id} berhasil dihapus permanen!")
                    st.rerun()
        
        # 1. Mengambil data dari Firestore
        users_ref = db.collection('users').stream()
        all_users = []
        for doc in users_ref:
            u_data = doc.to_dict()
            u_data['id'] = doc.id
            all_users.append(u_data)
            
        # 2. Menyortir data (Terbaru di atas)
        def sort_by_date(user_dict):
            t = user_dict.get('created_at')
            return t.timestamp() if t else 0
        all_users.sort(key=sort_by_date, reverse=True)

        st.write("Daftar Pengguna Saat Ini:")
        
        # 3. Menampilkan List dengan Teks "Hapus User" Sejajar
        for u_data in all_users:
            user_id = u_data['id']
            role = u_data.get('role', 'user')
            created_at = u_data.get('created_at')
            
            tgl_daftar = created_at.strftime("%d %b %Y") if created_at else "Data lama"
            
            # Membagi kolom agar tombol nempel dengan teks
            col_info, col_btn = st.columns([4, 1])
            with col_info:
                st.markdown(f"👤 **{user_id}** &nbsp;|&nbsp; Role: `{role}` &nbsp;|&nbsp; 📅 {tgl_daftar}")
            with col_btn:
                is_self = (user_id == st.session_state.current_user)
                if not is_self:
                    # Menggunakan type="tertiary" agar disulap oleh CSS menjadi teks link merah
                    if st.button("Hapus User", key=f"del_usr_{user_id}", type="tertiary"):
                        dialog_hapus_user(user_id)
                else:
                    st.caption("*(Admin Aktif)*")
                    
        st.markdown("---")
        
        # 4. Form Tambah / Edit User Baru
        st.markdown("#### ➕ Tambah / Edit Akun")
        with st.form("user_form"):
            add_email = st.text_input("Email / Username Baru")
            add_pwd = st.text_input("Password", type="password")
            add_role = st.selectbox("Role", ["user", "admin"])
            
            if st.form_submit_button("💾 Simpan Data User", use_container_width=True):
                if add_email and add_pwd:
                    save_user(add_email, add_pwd, add_role)
                    st.success(f"✅ User {add_email} berhasil disimpan!")
                    st.rerun()
                else: 
                    st.error("❌ Isi Username dan Password!")

st.markdown("<br><br><hr>", unsafe_allow_html=True) 
st.markdown("""<div style="text-align: center; font-size: 13px; color: #888;">Powered by <a href="https://espeje.com" target="_blank" class="footer-link">espeje.com</a> & <a href="https://link-gr.id" target="_blank" class="footer-link">link-gr.id</a></div>""", unsafe_allow_html=True)
