import streamlit as st
import speech_recognition as sr
import os
import subprocess
import math
import tempfile
import io
from shutil import which

# Import Library AI, DOCX, & Firebase
import google.generativeai as genai
from groq import Groq
from docx import Document
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

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
    # Validasi tambahan untuk mencegah query dengan string kosong
    if not username: return None
    doc = db.collection('users').document(username).get()
    return doc.to_dict() if doc.exists else None

def save_user(username, password, role):
    db.collection('users').document(username).set({"password": password, "role": role})

def delete_user(username):
    db.collection('users').document(username).delete()

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

# Bikin Default Admin jika belum ada di Firebase
if not get_user("admin"):
    save_user("admin", "payP@ssD97i0pal", "admin")

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
    
    /* UPDATE: CSS untuk tombol di dalam form agar konsisten */
    div.stButton > button, div.stDownloadButton > button, div[data-testid="stFormSubmitButton"] > button { 
        width: 100%; background-color: #000000 !important; color: #FFFFFF !important; border: 1px solid #000000; padding: 14px 20px; font-size: 16px; font-weight: 700; border-radius: 10px; transition: all 0.2s; box-shadow: 0 4px 6px rgba(0,0,0,0.1); 
    }
    div.stButton > button p, div.stDownloadButton > button p, div[data-testid="stFormSubmitButton"] > button p { color: #FFFFFF !important; }
    div.stButton > button:hover, div.stDownloadButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover { background-color: #333333 !important; color: #FFFFFF !important; transform: translateY(-2px); }
    
    /* Tombol Danger Merah untuk Hapus/Nonaktif */
    .btn-danger > button, .btn-danger > button:hover { background-color: #e74c3c !important; border-color: #c0392b !important; }
    .btn-warning > button, .btn-warning > button:hover { background-color: #f39c12 !important; border-color: #e67e22 !important; }
    
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

PROMPT_LAPORAN = """Kamu adalah ASN tingkat manajerial. Tugasmu menyusun ISI LAPORAN Memorandum dari transkrip.
INSTRUKSI MUTLAK:
- TULIS SANGAT PANJANG, MENDETAIL, DAN KOMPREHENSIF.
- JANGAN MERINGKAS. Jabarkan setiap topik yang dibahas, masalah yang ditemukan, dan solusi secara ekstensif.
- Abaikan kop surat (Yth, Hal, dll). Langsung ke isi.
Format:
1. Pendahuluan: (Penjelasan acara/rapat secara lengkap).
2. Uraian Hasil Pelaksanaan: (Penjabaran ekstensif seluruh dinamika, fakta, dan informasi dari transkrip).
3. Kesimpulan & Analisis: (Analisis mendalam atas hasil pembahasan).
4. Rekomendasi/Tindak Lanjut: (Saran konkret ke depan).
5. Penutup: ('Demikian kami laporkan, mohon arahan Bapak Pimpinan lebih lanjut. Terima kasih.')."""

# ==========================================
# 3. SIDEBAR (INFO & STATUS)
# ==========================================
with sidebar:
    st.header("⚙️ Status Sistem")
    if st.session_state.logged_in:
        st.success(f"👤 Login as: {st.session_state.current_user}")
        if st.session_state.user_role == "admin": st.info("👑 Anda Administrator.")
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state.logged_in, st.session_state.current_user, st.session_state.user_role = False, "", ""
            st.session_state.ai_result = ""
            st.rerun()
    else:
        st.caption("Silakan login di Tab 'Ekstrak AI'.")

# ==========================================
# 4. MAIN LAYOUT & TABS
# ==========================================
st.markdown('<div class="main-header">🎙️ TOM\'<span style="color: #e74c3c;">STT</span></div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Speech-to-Text | Konversi Audio ke Teks</div>', unsafe_allow_html=True)

tab_titles = ["📂 Upload File", "🎙️ Rekam Suara", "✨ Ekstrak AI"]
if st.session_state.user_role == "admin": tab_titles.append("⚙️ Panel Admin")
tabs = st.tabs(tab_titles)
tab1, tab2, tab3 = tabs[0], tabs[1], tabs[2]

audio_to_process, source_name = None, "audio"

# TAB 1 & 2: STT ENGINE
with tab1:
    uploaded_file = st.file_uploader("Pilih File Audio", type=["aac", "mp3", "wav", "m4a", "opus", "mp4", "3gp", "amr", "ogg", "flac", "wma"])
    if uploaded_file: audio_to_process, source_name = uploaded_file, uploaded_file.name

with tab2:
    audio_mic = st.audio_input("Klik ikon mic untuk mulai merekam")
    if audio_mic: audio_to_process, source_name = audio_mic, "rekaman_mic.wav"

if tab1 or tab2:
    st.write("") 
    c1, c2, c3 = st.columns([1, 4, 1]) 
    with c2:
        lang_choice = st.selectbox("Pilih Bahasa Audio", ("Indonesia", "Inggris"))
        st.write("") 
        if audio_to_process:
            submit_btn = st.button("🚀 Mulai Transkrip", use_container_width=True)
        else:
            st.markdown('<div class="custom-info-box">👆 Silakan Upload atau Rekam terlebih dahulu.</div>', unsafe_allow_html=True)
            submit_btn = False

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
        lang_code = "id-ID" if lang_choice == "Indonesia" else "en-US"

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
# 5. TAB 3 (EKSTRAK AI - DENGAN LOAD BALANCER)
# ==========================================
with tab3:
    if not st.session_state.logged_in:
        st.markdown('<div class="login-box"><h3>🔒 Login Diperlukan</h3><p>Silakan masukkan kredensial Anda.</p>', unsafe_allow_html=True)
        input_email = st.text_input("Username / Email")
        input_pwd = st.text_input("Password", type="password")
        if st.button("Masuk / Login"):
            user_data = get_user(input_email)
            if user_data and user_data["password"] == input_pwd:
                st.session_state.logged_in, st.session_state.current_user, st.session_state.user_role = True, input_email, user_data["role"]
                st.rerun()
            else: st.error("❌ Username atau Password salah!")
        st.markdown('</div>', unsafe_allow_html=True)
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
            st.write("")
            
            col1, col2 = st.columns(2)
            with col1: btn_notulen = st.button("📝 Buat Notulen", use_container_width=True)
            with col2: btn_laporan = st.button("📋 Buat Laporan", use_container_width=True)

            if btn_notulen or btn_laporan:
                prompt_active = PROMPT_NOTULEN if btn_notulen else PROMPT_LAPORAN
                ai_result = None
                
                # MENGAMBIL SEMUA API KEY YANG AKTIF & BELUM LIMIT
                active_keys = get_active_keys(engine_choice)
                
                if not active_keys:
                    st.error(f"❌ Tidak ada API Key {engine_choice} yang aktif atau semua Key sudah melebihi batas limit. Hubungi Admin!")
                else:
                    success_generation = False
                    
                    with st.spinner(f"🚀 Memproses dengan {engine_choice} (Load Balancer Aktif)..."):
                        # LOOP LOAD BALANCER: COBA SATU PER SATU
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
                                        messages=[{"role": "system", "content": prompt_active}, {"role": "user", "content": f"Berikut teks transkripnya:\n{st.session_state.transcript}"}],
                                        temperature=0.4,
                                    )
                                    ai_result = completion.choices[0].message.content

                                # JIKA BERHASIL: Catat pemakaian, hentikan loop
                                increment_api_usage(key_data["id"], key_data["used"])
                                success_generation = True
                                break 
                                
                            except Exception as e:
                                # JIKA GAGAL: Lanjut ke kunci berikutnya diam-diam
                                st.toast(f"⚠️ Kunci '{key_data['name']}' sibuk. Mencoba kunci cadangan...")
                                continue
                    
                    if success_generation and ai_result:
                        st.session_state.ai_result = ai_result
                        st.session_state.ai_prefix = "Notulen_" if btn_notulen else "Laporan_"
                    elif not success_generation:
                        st.error("❌ Gagal memproses. Seluruh API Key cadangan sedang mengalami gangguan server. Silakan coba lagi nanti.")

            if st.session_state.ai_result:
                st.markdown("---")
                st.markdown("### ✨ Hasil Ekstrak AI (Super Mendetail)")
                st.markdown(st.session_state.ai_result)
                
                prefix = st.session_state.ai_prefix
                st.download_button("💾 Download Hasil AI (.TXT)", st.session_state.ai_result, f"{prefix}{st.session_state.filename}.txt", "text/plain", use_container_width=True)
                docx_file = create_docx(st.session_state.ai_result, f"{prefix}{st.session_state.filename}")
                st.download_button("📄 Download Hasil AI (.DOCX)", data=docx_file, file_name=f"{prefix}{st.session_state.filename}.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", use_container_width=True)

# ==========================================
# 6. TAB 4 (PANEL ADMIN) - DATABASE API KEY & LIMIT
# ==========================================
if st.session_state.user_role == "admin":
    with tabs[3]:
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
                    # UPDATE: Nilai default limit menjadi 200
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
            
            ca1, ca2 = st.columns([1, 1])
            with ca1:
                btn_label = "🔴 Matikan" if k['is_active'] else "🟢 Hidupkan"
                st.markdown('<div class="btn-warning">', unsafe_allow_html=True)
                if st.button(f"{btn_label} '{k['name']}'", key=f"tog_{doc.id}"):
                    toggle_api_key(doc.id, k['is_active'])
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            with ca2:
                st.markdown('<div class="btn-danger">', unsafe_allow_html=True)
                if st.button(f"🗑️ Hapus '{k['name']}'", key=f"del_{doc.id}"):
                    delete_api_key(doc.id)
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
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
            
            c_add, c_del = st.columns(2)
            with c_add:
                # UPDATE: Tambahkan use_container_width=True agar rata
                if st.form_submit_button("Simpan User", use_container_width=True):
                    if add_email and add_pwd:
                        save_user(add_email, add_pwd, add_role)
                        st.success(f"✅ User {add_email} disimpan ke Firebase!")
                        st.rerun()
                    else: st.error("Isi Username dan Password!")
            with c_del:
                st.markdown('<div class="btn-danger">', unsafe_allow_html=True)
                # UPDATE: Tambahkan use_container_width=True dan validasi input kosong
                if st.form_submit_button("Hapus User", use_container_width=True):
                    if add_email: # Cek apakah email diisi
                        if get_user(add_email):
                            if add_email == "admin": st.error("Dilarang menghapus Admin Utama!")
                            else:
                                delete_user(add_email)
                                st.warning(f"🗑️ User {add_email} dihapus dari Firebase!")
                                st.rerun()
                        else: st.error("User tidak ditemukan.")
                    else:
                        st.error("Isi Username yang ingin dihapus!")
                st.markdown('</div>', unsafe_allow_html=True)

st.markdown("<br><br><hr>", unsafe_allow_html=True) 
st.markdown("""<div style="text-align: center; font-size: 13px; color: #888;">Powered by <a href="https://espeje.com" target="_blank" class="footer-link">espeje.com</a> & <a href="https://link-gr.id" target="_blank" class="footer-link">link-gr.id</a></div>""", unsafe_allow_html=True)
