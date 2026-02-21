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
    user_ref = db.collection('users').document(username)
    existing_user = user_ref.get()
    
    if existing_user.exists:
        user_ref.update({"password": password, "role": role})
    else:
        user_ref.set({
            "password": password,
            "role": role,
            "paket_aktif": "Freemium",
            "kuota": 2,                
            "saldo": 0,                
            "batas_durasi": 10,        
            "masa_aktif": "Selamanya",
            "created_at": datetime.now()
        })

def delete_user(username):
    db.collection('users').document(username).delete()
    
# --- FUNGSI KASIR & SUBSIDI SILANG ---
def hitung_estimasi_menit(teks):
    if not teks: return 0
    jumlah_kata = len(teks.split())
    durasi = math.ceil(jumlah_kata / 130)
    return durasi if durasi > 0 else 1 

def cek_pembayaran(username, durasi_menit):
    user_ref = db.collection('users').document(username)
    user_data = user_ref.get().to_dict()
    
    if user_data.get("role") == "admin": return True, "Akses Admin (Gratis)", 0, 0

    kuota = user_data.get("kuota", 0)
    saldo = user_data.get("saldo", 0)
    batas_durasi = user_data.get("batas_durasi", 10)
    TARIF = 350 

    if durasi_menit <= batas_durasi:
        if kuota > 0: return True, "1 Kuota Terpakai.", 1, 0
        else:
            biaya = durasi_menit * TARIF
            if saldo >= biaya: return True, f"Saldo terpotong Rp {biaya:,}", 0, biaya
            else: return False, f"Saldo kurang. Butuh Rp {biaya:,} untuk {durasi_menit} Menit.", 0, 0
    else:
        kelebihan = durasi_menit - batas_durasi
        biaya_tambahan = kelebihan * TARIF
        if kuota > 0:
            if saldo >= biaya_tambahan: return True, f"1 Kuota + Saldo Rp {biaya_tambahan:,} terpakai (Kelebihan waktu).", 1, biaya_tambahan
            else: return False, f"Kelebihan durasi! Saldo Anda kurang untuk menutupi biaya tambahan Rp {biaya_tambahan:,}", 0, 0
        else:
            biaya_total = durasi_menit * TARIF
            if saldo >= biaya_total: return True, f"Saldo terpotong Rp {biaya_total:,}", 0, biaya_total
            else: return False, f"Saldo kurang. Butuh Rp {biaya_total:,} untuk total {durasi_menit} Menit.", 0, 0

def eksekusi_pembayaran(username, potong_kuota, potong_saldo):
    if potong_kuota == 0 and potong_saldo == 0: return 
    user_ref = db.collection('users').document(username)
    user_ref.update({
        "kuota": firestore.Increment(-potong_kuota),
        "saldo": firestore.Increment(-potong_saldo)
    })

# --- FUNGSI DATABASE FIREBASE (API KEYS) ---
def add_api_key(name, provider, key_string, limit):
    db.collection('api_keys').add({"name": name, "provider": provider, "key": key_string, "limit": int(limit), "used": 0, "is_active": True})

def delete_api_key(doc_id): db.collection('api_keys').document(doc_id).delete()
def toggle_api_key(doc_id, current_status): db.collection('api_keys').document(doc_id).update({"is_active": not current_status})
def increment_api_usage(doc_id, current_used): db.collection('api_keys').document(doc_id).update({"used": current_used + 1})

def get_active_keys(provider):
    keys_ref = db.collection('api_keys').where("provider", "==", provider).where("is_active", "==", True).stream()
    valid_keys = []
    for doc in keys_ref:
        data = doc.to_dict()
        data['id'] = doc.id
        if data['used'] < data['limit']: valid_keys.append(data)
    return valid_keys

# ==========================================
# INISIALISASI MEMORI (SESSION STATE & AUTO LOGIN)
# ==========================================
if 'transcript' not in st.session_state: st.session_state.transcript = ""
if 'filename' not in st.session_state: st.session_state.filename = "Hasil_STT"
if 'logged_in' not in st.session_state: st.session_state.logged_in = False
if 'current_user' not in st.session_state: st.session_state.current_user = ""
if 'user_role' not in st.session_state: st.session_state.user_role = ""
if 'ai_result' not in st.session_state: st.session_state.ai_result = "" 
if 'ai_prefix' not in st.session_state: st.session_state.ai_prefix = "" 

# 🚀 ANTI-LOGOUT: Membaca Sesi dari URL Browser
if "user_session" in st.query_params and "user_role" in st.query_params:
    st.session_state.logged_in = True
    st.session_state.current_user = st.query_params["user_session"]
    st.session_state.user_role = st.query_params["user_role"]

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
    
    div.stButton > button, div.stDownloadButton > button, div[data-testid="stFormSubmitButton"] > button { width: 100%; background-color: #000000 !important; color: #FFFFFF !important; border: 1px solid #000000; padding: 14px 20px; font-size: 16px; font-weight: 700; border-radius: 10px; transition: all 0.2s; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    div.stButton > button p, div.stDownloadButton > button p, div[data-testid="stFormSubmitButton"] > button p { color: #FFFFFF !important; }
    div.stButton > button:hover, div.stDownloadButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover { background-color: #333333 !important; color: #FFFFFF !important; transform: translateY(-2px); }
    
    .stCaption, p { color: #444444 !important; }
    textarea { color: #000000 !important; background-color: #F8F9FA !important; font-weight: 500 !important; }
    textarea:disabled { color: #000000 !important; -webkit-text-fill-color: #000000 !important; opacity: 1 !important; }
    
    [data-testid="collapsedControl"] svg, [data-testid="stSidebarCollapseButton"] svg, button[kind="header"] svg { fill: #111111 !important; stroke: #111111 !important; color: #111111 !important; }
    [data-testid="stExpander"] details summary p, [data-testid="stExpander"] details summary span { color: #111111 !important; font-weight: 700 !important; }
    [data-testid="stExpander"] details summary svg { fill: #111111 !important; color: #111111 !important; }
    div[data-testid="stMarkdownContainer"] p, div[data-testid="stMarkdownContainer"] h1, div[data-testid="stMarkdownContainer"] h2, div[data-testid="stMarkdownContainer"] h3, div[data-testid="stMarkdownContainer"] li, div[data-testid="stMarkdownContainer"] strong, div[data-testid="stMarkdownContainer"] span { color: #111111 !important; }
    
    [data-testid="stSidebar"] { background-color: #F4F6F9 !important; }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] p, [data-testid="stSidebar"] label { color: #111111 !important; font-weight: 600 !important; }
    [data-testid="stSidebar"] input { background-color: #FFFFFF !important; color: #000000 !important; border: 1px solid #CCCCCC !important; }
    
    .custom-info-box { background-color: #e6f3ff; color: #0068c9; padding: 15px; border-radius: 10px; text-align: center; font-weight: 600; border: 1px solid #cce5ff; margin-bottom: 20px; }
    .login-box { background-color: #F8F9FA; padding: 25px; border-radius: 12px; border: 1px solid #E0E0E0; margin-bottom: 20px; }
    .footer-link { text-decoration: none; font-weight: 700; color: #e74c3c !important; }
    .api-card { background-color: #f8f9fa; border: 1px solid #ddd; padding: 15px; border-radius: 8px; margin-bottom: 15px; color: #111111 !important; }
	
	/* FIX MODAL DIALOG (PAKSA PUTIH & TEKS HITAM) */
    div[data-testid="stDialog"], div[role="dialog"] { background-color: #FFFFFF !important; }
    div[data-testid="stDialog"] > div { background-color: #FFFFFF !important; }
    div[role="dialog"] h1, div[role="dialog"] h2, div[role="dialog"] h3, div[role="dialog"] p, div[role="dialog"] li, div[role="dialog"] span { color: #111111 !important; }
    div[role="dialog"] div.stButton > button p { color: #FFFFFF !important; }
    div[role="dialog"] hr { border-color: #EEEEEE !important; }
	
	/* FIX TOMBOL BAYAR MIDTRANS */
    div[data-testid="stLinkButton"] > a { width: 100% !important; background-color: #000000 !important; border: 1px solid #000000 !important; border-radius: 10px !important; padding: 14px 20px !important; text-decoration: none !important; display: flex !important; justify-content: center !important; align-items: center !important; transition: all 0.2s !important; }
    div[data-testid="stLinkButton"] > a p, div[data-testid="stLinkButton"] > a span, div[role="dialog"] div[data-testid="stLinkButton"] > a p, div[role="dialog"] div[data-testid="stLinkButton"] > a span { color: #FFFFFF !important; font-weight: 700 !important; font-size: 16px !important; }
    div[data-testid="stLinkButton"] > a:hover { background-color: #333333 !important; transform: translateY(-2px) !important; }
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

PROMPT_NOTULEN = "Kamu adalah Sekretaris Profesional...\n(Instruksi disingkat di sini, tetap gunakan prompt asli Anda di app sebelumnya jika mau)"
PROMPT_LAPORAN = "Kamu adalah ASN tingkat manajerial...\n(Instruksi disingkat di sini, tetap gunakan prompt asli Anda di app sebelumnya jika mau)"

# ==========================================
# 3. SIDEBAR & ETALASE HARGA (MIDTRANS SNAP)
# ==========================================
def buat_tagihan_midtrans(nama_paket, harga, user_email):
    url = "https://app.sandbox.midtrans.com/snap/v1/transactions" 
    server_key = st.secrets["midtrans_server_key"]
    order_id = f"TOM-{nama_paket.split()[0].upper()}-{uuid.uuid4().hex[:6].upper()}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {
        "transaction_details": {"order_id": order_id, "gross_amount": harga},
        "customer_details": {"email": user_email},
	    "custom_field1": user_email,
        "item_details": [{"id": nama_paket.replace(" ", "_"), "price": harga, "quantity": 1, "name": f"Paket {nama_paket} TOM'STT"}]
    }
    response = requests.post(url, auth=(server_key, ''), json=payload, headers=headers)
    if response.status_code == 201: return response.json().get("redirect_url")
    else: st.error(f"Gagal menghubungi gateway pembayaran: {response.text}"); return None

@st.dialog("🛒 Pilih Paket Kebutuhan Anda", width="large")
def show_pricing_dialog():
    user_email = st.session_state.current_user
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("""
        📦 **PAKET STARTER**
        ### Rp 50.750
        ✅ **5x** Ekstrak AI (Laporan/Notulen)
        ✅ Maks. Durasi Audio **1 Jam (~7.800 Kata)** / File
        🎁 Bonus Saldo **Rp 3.000**
        """)
        if st.button("🛒 Beli Paket Starter", use_container_width=True, key="buy_starter"):
            with st.spinner("Mencetak tagihan..."):
                link_bayar = buat_tagihan_midtrans("Starter", 50750, user_email)
                if link_bayar: st.link_button("💳 Bayar Sekarang (QRIS/VA)", link_bayar, use_container_width=True)
            
        st.markdown("---")
        st.markdown("""
        💼 **PAKET PRO NOTULIS**
        ### Rp 101.500
        ✅ **15x** Ekstrak AI (Laporan/Notulen)
        ✅ Maks. Durasi Audio **1,5 Jam (~11.700 Kata)** / File
        🎁 Bonus Saldo **Rp 10.000**
        """)
        if st.button("🛒 Beli Paket Pro", use_container_width=True, key="buy_pro"):
            with st.spinner("Mencetak tagihan..."):
                link_bayar = buat_tagihan_midtrans("Pro", 101500, user_email)
                if link_bayar: st.link_button("💳 Bayar Sekarang (QRIS/VA)", link_bayar, use_container_width=True)

    with col2:
        st.markdown("""
        🏢 **PAKET EKSEKUTIF (Divisi)**
        ### Rp 304.500
        ✅ **50x** Ekstrak AI (Laporan/Notulen)
        ✅ Maks. Durasi Audio **2 Jam (~15.600 Kata)** / File
        🎁 Bonus Saldo **Rp 20.000**
        """)
        if st.button("🛒 Beli Paket Eksekutif", use_container_width=True, key="buy_exec"):
            with st.spinner("Mencetak tagihan..."):
                link_bayar = buat_tagihan_midtrans("Eksekutif", 304500, user_email)
                if link_bayar: st.link_button("💳 Bayar Sekarang (QRIS/VA)", link_bayar, use_container_width=True)
            
        st.markdown("---")
        st.markdown("""
        👑 **PAKET VIP INSTANSI**
        ### Rp 507.500
        ✅ **100x** Ekstrak AI (Laporan/Notulen)
        ✅ Maks. Durasi Audio **3 Jam (~23.400 Kata)** / File
        🎁 Bonus Saldo **Rp 35.000**
        """)
        if st.button("🛒 Beli Paket VIP", use_container_width=True, key="buy_vip"):
            with st.spinner("Mencetak tagihan..."):
                link_bayar = buat_tagihan_midtrans("VIP", 507500, user_email)
                if link_bayar: st.link_button("💳 Bayar Sekarang (QRIS/VA)", link_bayar, use_container_width=True)

    st.markdown("---")
    st.markdown("💡 **Informasi:** Sistem Adil (Fair Usage). 1 Kuota = 1x Pembuatan Dokumen. Kelebihan menit akan dipotong dari Saldo Darurat dengan tarif Rp350/menit.")

with st.sidebar:
    st.header("⚙️ Status Sistem")
    
    if st.session_state.logged_in:
        st.success(f"👤 Login as: {st.session_state.current_user}")
        
        user_data = get_user(st.session_state.current_user)
        
        if user_data:
            st.markdown("---")
            st.markdown("### 💼 Dompet Anda")
            
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
                saldo_rp = f"Rp {saldo:,}".replace(",", ".")
                
                st.info(f"📦 Paket: **{paket}**")
                
                col_k, col_b = st.columns(2)
                col_k.metric("Sisa Kuota", f"{kuota}x")
                col_b.metric("Batas Paket", f"{batas} Menit")
                
                st.metric("💳 Saldo Darurat", saldo_rp)
                
                # 🚀 TOMBOL SEGARKAN DOMPET
                if st.button("🔄 Segarkan Dompet", use_container_width=True):
                    st.rerun()
                
                if st.button("🛒 Upgrade / Top-Up", use_container_width=True):
                    show_pricing_dialog() 
                
            st.markdown("---")

        if st.session_state.user_role == "admin": st.info("👑 Anda Administrator.")
            
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state.logged_in, st.session_state.current_user, st.session_state.user_role = False, "", ""
            st.session_state.ai_result = ""
            st.query_params.clear() # 🚀 BERSIHKAN SESI URL SAAT LOGOUT
            st.rerun()
    else:
        st.caption("Silakan login di Tab '🔐 Akun'.")

# ==========================================
# 4. MAIN LAYOUT & TABS
# ==========================================
st.markdown('<div class="main-header">🎙️ TOM\'<span style="color: #e74c3c !important;">STT</span></div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Speech-to-Text | Konversi Audio ke Teks</div>', unsafe_allow_html=True)

tab_titles = ["📂 Upload File", "🎙️ Rekam Suara", "✨ Ekstrak AI", "🔐 Akun"]
if st.session_state.user_role == "admin": tab_titles.append("⚙️ Panel Admin")
tabs = st.tabs(tab_titles)
tab_upload, tab_rekam, tab_ai, tab_auth = tabs[0], tabs[1], tabs[2], tabs[3]

audio_to_process, source_name, submit_btn, lang_code = None, "audio", False, "id-ID"

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
        else: st.markdown('<div class="custom-info-box">👆 Silakan Upload terlebih dahulu.</div>', unsafe_allow_html=True)

with tab_rekam:
    if not st.session_state.logged_in: st.markdown('<div class="custom-info-box">🔒 Silakan masuk (login) di tab <b>🔐 Akun</b> untuk merekam.</div>', unsafe_allow_html=True)
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
            else: st.markdown('<div class="custom-info-box">👆 Silakan Rekam terlebih dahulu.</div>', unsafe_allow_html=True)

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
            subprocess.run([ffmpeg_cmd, "-y", "-i", input_path, "-ss", str(start_time), "-t", str(chunk_len), "-filter:a", "volume=3.0", "-ar", "16000", "-ac", "1", chunk_filename], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
# 5. TAB 3 (AKSES) & TAB 4 (EKSTRAK AI)
# ==========================================
with tab_auth:
    if not st.session_state.logged_in:
        st.markdown('<div class="login-box" style="text-align: center;"><h3>🔒 Portal Akses</h3><p>Silakan masuk atau buat akun baru untuk mulai menggunakan AI.</p></div>', unsafe_allow_html=True)
        auth_tab1, auth_tab2 = st.tabs(["🔑 Masuk (Login)", "📝 Daftar Baru (Register)"])
        
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
                        url_lookup = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={api_key}"
                        lookup_res = requests.post(url_lookup, json={"idToken": id_token}).json()
                        is_verified = lookup_res.get("users", [{}])[0].get("emailVerified", False)
                        
                        user_data = get_user(login_email)
                        is_admin = user_data and user_data.get("role") == "admin"
                        
                        if not is_verified and not is_admin:
                            st.error("❌ Akses Ditolak: Email Anda belum diverifikasi!")
                            st.warning("📧 Silakan cek Inbox email Anda.")
                        else:
                            if not user_data:
                                save_user(login_email, login_pwd, "user")
                                user_data = {"role": "user"}
                                
                            st.session_state.logged_in = True
                            st.session_state.current_user = login_email
                            st.session_state.user_role = user_data.get("role", "user")
                            
                            # 🚀 SIMPAN SESI KE URL SAAT BERHASIL LOGIN
                            st.query_params["user_session"] = login_email
                            st.query_params["user_role"] = user_data.get("role", "user")
                            
                            st.rerun()
                    else: st.error("❌ Email atau Password salah!")
                        
        with auth_tab2:
            reg_email = st.text_input("Email Aktif", key="reg_email").strip()
            reg_pwd = st.text_input("Buat Password (Min. 6 Karakter)", type="password", key="reg_pwd")
            if st.button("🎁 Daftar & Klaim Kuota", use_container_width=True):
                if len(reg_pwd) < 6 or not reg_email: st.error("❌ Periksa kembali email dan password Anda!")
                else:
                    with st.spinner("Mendaftarkan akun..."):
                        api_key = st.secrets["firebase_web_api_key"]
                        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}"
                        res = requests.post(url, json={"email": reg_email, "password": reg_pwd, "returnSecureToken": True}).json()
                        if "idToken" in res:
                            requests.post(f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}", json={"requestType": "VERIFY_EMAIL", "idToken": res["idToken"]})
                            save_user(reg_email, reg_pwd, "user")
                            st.success("✅ Pembuatan akun berhasil! Cek email untuk verifikasi.")
                        else: st.error("❌ Gagal mendaftar, email mungkin sudah ada.")
    else: st.success(f"✅ Masuk sebagai: **{st.session_state.current_user}**")

with tab_ai:
    if not st.session_state.logged_in: st.markdown('<div class="custom-info-box">🔒 Akses Terkunci! Silakan Login.</div>', unsafe_allow_html=True)
    else:
        if not st.session_state.transcript:
            st.markdown('<div class="custom-info-box">👆 Transkrip belum tersedia. Unggah file .txt:</div>', unsafe_allow_html=True)
            uploaded_txt = st.file_uploader("Upload .txt", type=["txt"])
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
                
            engine_choice = st.radio("Pilih Mesin AI:", ["Gemini", "Groq"])
            durasi_teks = hitung_estimasi_menit(st.session_state.transcript)
            st.info(f"📊 Beban Pemrosesan: **± {durasi_teks} Menit** pemakaian paket.")
            
            col1, col2 = st.columns(2)
            with col1: btn_notulen = st.button("📝 Buat Notulen", use_container_width=True)
            with col2: btn_laporan = st.button("📋 Buat Laporan", use_container_width=True)

            if btn_notulen or btn_laporan:
                bisa_bayar, pesan_bayar, p_kuota, p_saldo = cek_pembayaran(st.session_state.current_user, durasi_teks)
                
                if not bisa_bayar: st.error(f"❌ DITOLAK: {pesan_bayar}")
                else:
                    prompt_active = PROMPT_NOTULEN if btn_notulen else PROMPT_LAPORAN
                    ai_result, active_keys, success_generation = None, get_active_keys(engine_choice), False
                    
                    if not active_keys: st.error("❌ Sistem Sibuk: API Key habis limit.")
                    else:
                        with st.spinner(f"🚀 Memproses dengan {engine_choice}..."):
                            for key_data in active_keys:
                                try:
                                    if engine_choice == "Gemini":
                                        genai.configure(api_key=key_data["key"])
                                        model = genai.GenerativeModel('gemini-2.5-flash')
                                        ai_result = model.generate_content(f"{prompt_active}\n\nTranskrip:\n{st.session_state.transcript}").text
                                    elif engine_choice == "Groq":
                                        client = Groq(api_key=key_data["key"])
                                        ai_result = client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role": "system", "content": prompt_active}, {"role": "user", "content": st.session_state.transcript}], temperature=0.4).choices[0].message.content
                                    increment_api_usage(key_data["id"], key_data["used"])
                                    success_generation = True
                                    break 
                                except: continue
                        
                        if success_generation and ai_result:
                            eksekusi_pembayaran(st.session_state.current_user, p_kuota, p_saldo)
                            st.success(f"✅ **Berhasil!** {pesan_bayar}")
                            st.session_state.ai_result = ai_result
                            st.session_state.ai_prefix = "Notulen_" if btn_notulen else "Laporan_"
                        else: st.error("❌ Server API gagal.")

            if st.session_state.ai_result:
                st.markdown("### ✨ Hasil Ekstrak AI")
                st.markdown(st.session_state.ai_result)
                prefix = st.session_state.ai_prefix
                st.download_button("💾 Download (.TXT)", st.session_state.ai_result, f"{prefix}{st.session_state.filename}.txt", "text/plain", use_container_width=True)
                st.download_button("📄 Download (.DOCX)", data=create_docx(st.session_state.ai_result, f"{prefix}{st.session_state.filename}"), file_name=f"{prefix}{st.session_state.filename}.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", use_container_width=True)

# ==========================================
# 6. TAB 5 (PANEL ADMIN)
# ==========================================
if st.session_state.user_role == "admin":
    with tabs[4]:
        st.markdown("#### ⚙️ Panel Admin")
        with st.expander("➕ Tambah API Key"):
            with st.form("form_add_key"):
                c1, c2 = st.columns(2)
                with c1:
                    new_provider = st.selectbox("Provider", ["Gemini", "Groq"])
                    new_name = st.text_input("Nama Key")
                with c2:
                    new_limit = st.number_input("Batas Limit", min_value=1, value=200)
                    new_key_str = st.text_input("API Key", type="password")
                if st.form_submit_button("Simpan"):
                    add_api_key(new_name, new_provider, new_key_str, new_limit); st.rerun()

        st.markdown("#### 📋 API Keys")
        for doc in db.collection('api_keys').stream():
            k = doc.to_dict()
            st.markdown(f"**{k['name']}** ({k['provider']}) - Terpakai: {k['used']}/{k['limit']} - Status: {'AKTIF' if k['is_active'] else 'MATI'}")
            ca1, ca2 = st.columns(2)
            with ca1:
                if st.button("Toggle Status", key=f"tog_{doc.id}", use_container_width=True): toggle_api_key(doc.id, k['is_active']); st.rerun()
            with ca2:
                if st.button("Hapus", key=f"del_{doc.id}", use_container_width=True): delete_api_key(doc.id); st.rerun()
            st.write("---")
            
        st.markdown("#### 👥 Manajemen User")
        with st.form("user_form"):
            add_email = st.text_input("Username")
            add_pwd = st.text_input("Password", type="password")
            add_role = st.selectbox("Role", ["user", "admin"])
            c_add, c_del = st.columns(2)
            with c_add:
                if st.form_submit_button("Simpan User", use_container_width=True): save_user(add_email, add_pwd, add_role); st.rerun()
            with c_del:
                if st.form_submit_button("Hapus User", use_container_width=True): delete_user(add_email); st.rerun()

st.markdown("<br><hr><div style='text-align: center; font-size: 13px; color: #888;'>Powered by <a href='https://espeje.com'>espeje.com</a></div>", unsafe_allow_html=True)
