import streamlit as st
import speech_recognition as sr
import os
import subprocess
import math
import tempfile
from shutil import which

# Import Library AI
import google.generativeai as genai
from groq import Groq

# ==========================================
# 1. SETUP & CONFIG
# ==========================================
st.set_page_config(
    page_title="TOM'STT", 
    page_icon="🎙️", 
    layout="centered",
    initial_sidebar_state="expanded" 
)

# Inisialisasi Memori (Session State)
if 'transcript' not in st.session_state:
    st.session_state.transcript = ""
if 'filename' not in st.session_state:
    st.session_state.filename = "Hasil_STT"
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .stApp { background-color: #FFFFFF !important; }
    
    .main-header {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        font-weight: 800; color: #111111 !important; text-align: center;
        margin-top: 20px; margin-bottom: 5px; font-size: 2.4rem; letter-spacing: -1.5px;
    }
    .sub-header {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        color: #666666 !important; text-align: center; font-size: 1rem;
        margin-bottom: 30px; font-weight: 500;
    }

    .stFileUploader label, div[data-testid="stSelectbox"] label, .stAudioInput label {
        width: 100% !important; text-align: center !important; display: block !important;
        color: #000000 !important; font-size: 1rem !important; font-weight: 700 !important;
        margin-bottom: 8px !important;
    }

    [data-testid="stFileUploaderDropzone"] {
        background-color: #F0F2F6 !important; border: 1px dashed #444 !important; border-radius: 12px;
    }
    [data-testid="stFileUploaderDropzone"] div, [data-testid="stFileUploaderDropzone"] span, [data-testid="stFileUploaderDropzone"] small {
        color: #000000 !important;
    }
    [data-testid="stFileUploaderDropzone"] button {
        background-color: #000000 !important; color: #FFFFFF !important; border: none !important;
    }
    .stFileUploader > div > small { display: none !important; }
    div[data-testid="stFileUploaderFileName"] { color: #000000 !important; font-weight: 600 !important; }
    
    div.stButton > button, div.stDownloadButton > button {
        width: 100%; background-color: #000000 !important; color: #FFFFFF !important;
        border: 1px solid #000000; padding: 14px 20px; font-size: 16px; font-weight: 700;
        border-radius: 10px; transition: all 0.2s; box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    div.stButton > button p, div.stDownloadButton > button p { color: #FFFFFF !important; }
    div.stButton > button:hover, div.stDownloadButton > button:hover {
        background-color: #333333 !important; color: #FFFFFF !important; transform: translateY(-2px);
    }
    
    .stCaption, div[data-testid="stCaptionContainer"], p { color: #444444 !important; }
    
    textarea {
        color: #000000 !important; background-color: #F8F9FA !important; font-weight: 500 !important;
    }

    div[data-testid="stMarkdownContainer"] p, div[data-testid="stMarkdownContainer"] h1, 
    div[data-testid="stMarkdownContainer"] h2, div[data-testid="stMarkdownContainer"] h3, 
    div[data-testid="stMarkdownContainer"] h4, div[data-testid="stMarkdownContainer"] li,
    div[data-testid="stMarkdownContainer"] strong, div[data-testid="stMarkdownContainer"] span {
        color: #111111 !important;
    }
    
    [data-testid="stSidebar"] { background-color: #F4F6F9 !important; }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3, 
    [data-testid="stSidebar"] p, [data-testid="stSidebar"] span, [data-testid="stSidebar"] label {
        color: #111111 !important; font-weight: 600 !important;
    }
    [data-testid="stSidebar"] input {
        background-color: #FFFFFF !important; color: #000000 !important; border: 1px solid #CCCCCC !important;
    }
    
    .mobile-tips {
        background-color: #FFF3CD; color: #856404; padding: 12px; border-radius: 10px;
        font-size: 0.9rem; text-align: center; margin-bottom: 25px; border: 1px solid #FFEEBA;
    }
    .mobile-tips b, .mobile-tips small { color: #856404 !important; }

    .custom-info-box {
        background-color: #e6f3ff; color: #0068c9; padding: 15px; border-radius: 10px;
        text-align: center; font-weight: 600; border: 1px solid #cce5ff; margin-bottom: 20px;
    }

    .login-box {
        background-color: #F8F9FA; padding: 25px; border-radius: 12px; border: 1px solid #E0E0E0;
        margin-bottom: 20px;
    }

    .footer-link { text-decoration: none; font-weight: 700; color: #e74c3c !important; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. LOGIKA FFMPEG & AI PROMPTS
# ==========================================
project_folder = os.getcwd()
local_ffmpeg = os.path.join(project_folder, "ffmpeg.exe")
local_ffprobe = os.path.join(project_folder, "ffprobe.exe")

if os.path.exists(local_ffmpeg) and os.path.exists(local_ffprobe):
    ffmpeg_cmd = local_ffmpeg
    ffprobe_cmd = local_ffprobe
    os.environ["PATH"] += os.pathsep + project_folder
else:
    if which("ffmpeg") and which("ffprobe"):
        ffmpeg_cmd = "ffmpeg"
        ffprobe_cmd = "ffprobe"
    else:
        st.error("❌ Critical Error: FFmpeg tools not found.")
        st.stop()

def get_duration(file_path):
    cmd = [ffprobe_cmd, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
    try:
        return float(subprocess.check_output(cmd, stderr=subprocess.STDOUT))
    except:
        return 0.0

# PENGUATAN PROMPT (3X LEBIH PANJANG & KOMPREHENSIF)
PROMPT_NOTULEN = """Kamu adalah seorang Sekretaris Profesional dan Notulis Rapat tingkat senior. Tugasmu adalah mengubah teks transkrip audio menjadi Notulen Rapat yang sangat komprehensif, mendetail, dan terstruktur.
Instruksi Penting:
- Jangan hanya membuat ringkasan singkat. Jabarkan setiap poin, argumen, dan dinamika diskusi secara mendalam.
- Hasil akhir harus minimal 3x lipat lebih panjang dan detail dari ringkasan standar AI.
- Tangkap semua nuansa percakapan, alasan di balik keputusan, dan masukan dari berbagai pihak.
Susun notulen dengan format berikut:
1. Agenda/Topik Utama: (Jabarkan secara komprehensif latar belakang dan tujuan pembahasan).
2. Uraian Detail Pembahasan: (Buat poin-poin yang sangat mendetail tentang setiap isu yang dibahas, argumen yang diangkat, dan solusi yang ditawarkan).
3. Keputusan/Kesimpulan: (Jabarkan hasil akhir secara spesifik beserta alasannya).
4. Tindak Lanjut (Action Items): (Daftar tugas detail, langkah teknis yang perlu diambil, dan penanggung jawabnya).
Gunakan bahasa Indonesia formal (EYD)."""

PROMPT_LAPORAN = """Kamu adalah seorang Aparatur Sipil Negara (ASN) tingkat manajerial yang ditugaskan menyusun Laporan Memorandum atau Nota Dinas. Tugasmu adalah menyusun bagian ISI LAPORAN yang sangat komprehensif, analitis, dan mendetail dari teks transkrip.
Instruksi Penting:
- Jabarkan setiap bahasan secara ekstensif. Hasil akhir harus minimal 3x lipat lebih panjang dari sekadar rangkuman biasa.
- Jangan menghilangkan detail penting, data, atau arahan spesifik yang ada di transkrip.
- Abaikan pembuatan kop surat, 'Yth', 'Dari', atau 'Hal'. Langsung masuk ke isi.
Susun laporan dengan struktur berikut:
1. Pendahuluan: (Uraikan latar belakang pelaksanaan kegiatan secara komprehensif).
2. Uraian Hasil Pelaksanaan: (Gunakan sistem penomoran. Jabarkan secara sangat mendetail setiap materi/topik yang dibahas, fakta-fakta lapangan, serta dinamika yang terjadi).
3. Kesimpulan dan Analisis: (Berikan rangkuman komprehensif dan analisis dari hasil bahasan tersebut).
4. Rekomendasi/Tindak Lanjut: (Berikan saran langkah ke depan yang detail dan terukur).
5. Penutup: ('Demikian kami laporkan, mohon arahan Bapak Pimpinan lebih lanjut. Terima kasih.')."""

# ==========================================
# 3. SIDEBAR (API KEYS)
# ==========================================
saved_groq = st.secrets.get("GROQ_API_KEY", "")
saved_gemini = st.secrets.get("GEMINI_API_KEY", "")

with st.sidebar:
    st.header("⚙️ Pengaturan AI")
    if saved_groq or saved_gemini:
        st.success("✅ Sistem mendeteksi API Key permanen aktif.")
        groq_key = saved_groq
        gemini_key = saved_gemini
    else:
        st.caption("Masukkan API Key Anda di bawah ini:")
        groq_key = st.text_input("🔑 Groq API Key (Utama)", type="password")
        gemini_key = st.text_input("🔑 Gemini API Key (Cadangan)", type="password")
        st.markdown("---")
        st.caption("🔒 Key yang diketik di sini hanya tersimpan sementara.")

# ==========================================
# 4. UI LAYOUT
# ==========================================

st.markdown("""
<div class="main-header">
    🎙️ TOM'<span style="color: #e74c3c;">STT</span>
</div>
""", unsafe_allow_html=True)

st.markdown('<div class="sub-header">Speech-to-Text | Konversi Audio ke Teks</div>', unsafe_allow_html=True)

st.markdown("""
<div class="mobile-tips">
    <b>Tips Pengguna Handphone:</b><br>
    Saat proses upload & transkrip berjalan, <b>jangan biarkan layar mati atau berpindah aplikasi</b> agar koneksi tidak terputus.
</div>
""", unsafe_allow_html=True)

tab1, tab2, tab3 = st.tabs(["📂 Upload File", "🎙️ Rekam Suara", "✨ Ekstrak AI"])
audio_to_process = None
source_name = "audio" 

# TAB 1 & 2: STT ENGINE
with tab1:
    uploaded_file = st.file_uploader("Pilih File Audio", type=["aac", "mp3", "wav", "m4a", "opus", "mp4", "3gp", "amr", "ogg", "flac", "wma"])
    if uploaded_file:
        audio_to_process = uploaded_file
        source_name = uploaded_file.name

with tab2:
    audio_mic = st.audio_input("Klik ikon mic untuk mulai merekam")
    if audio_mic:
        audio_to_process = audio_mic
        source_name = "rekaman_mic.wav"

if tab1 or tab2:
    st.write("") 
    c1, c2, c3 = st.columns([1, 4, 1]) 
    with c2:
        lang_choice = st.selectbox("Pilih Bahasa Audio", ("Indonesia", "Inggris"))
        st.write("") 
        if audio_to_process:
            submit_btn = st.button("🚀 Mulai Transkrip", use_container_width=True)
        else:
            st.markdown("""
                <div class="custom-info-box">
                    👆 Silakan Upload atau Rekam terlebih dahulu.
                </div>
            """, unsafe_allow_html=True)
            submit_btn = False

if submit_btn and audio_to_process:
    st.markdown("---")
    status_box = st.empty()
    progress_bar = st.progress(0)
    result_area = st.empty()
    full_transcript = []
    
    if source_name == "rekaman_mic.wav":
        file_ext = ".wav"
    else:
        file_ext = os.path.splitext(source_name)[1]
        if not file_ext: file_ext = ".wav"

    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
        tmp_file.write(audio_to_process.getvalue())
        input_path = tmp_file.name

    try:
        duration_sec = get_duration(input_path)
        if duration_sec == 0:
            st.error("Gagal membaca audio.")
            st.stop()
            
        chunk_len = 59 
        total_chunks = math.ceil(duration_sec / chunk_len)
        status_box.info(f"⏱️ Durasi: {duration_sec:.2f}s")
        
        recognizer = sr.Recognizer()
        recognizer.energy_threshold = 300 
        recognizer.dynamic_energy_threshold = True 
        lang_code = "id-ID" if lang_choice == "Indonesia" else "en-US"

        for i in range(total_chunks):
            start_time = i * chunk_len
            chunk_filename = f"temp_slice_{i}.wav"
            cmd = [
                ffmpeg_cmd, "-y", "-i", input_path,
                "-ss", str(start_time), "-t", str(chunk_len),
                "-filter:a", "volume=3.0", 
                "-ar", "16000", "-ac", "1", chunk_filename
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            try:
                with sr.AudioFile(chunk_filename) as source:
                    audio_data = recognizer.record(source)
                    text = recognizer.recognize_google(audio_data, language=lang_code)
                    full_transcript.append(text)
                    result_area.text_area("📝 Live Preview:", " ".join(full_transcript), height=250)
            except sr.UnknownValueError:
                pass 
            except Exception:
                full_transcript.append("") 
            finally:
                if os.path.exists(chunk_filename):
                    os.remove(chunk_filename)
            
            pct = int(((i + 1) / total_chunks) * 100)
            progress_bar.progress(pct)
            status_box.caption(f"Sedang memproses... ({pct}%)")

        status_box.success("✅ Selesai! Transkrip tersimpan di sistem. Silakan klik Tab '✨ Ekstrak AI'.")
        final_text = " ".join(full_transcript)
        
        st.session_state.transcript = final_text
        st.session_state.filename = os.path.splitext(source_name)[0]
        
        output_filename = f"{st.session_state.filename}.txt"
        st.download_button("💾 Download Hasil (.TXT)", final_text, output_filename, "text/plain", use_container_width=True)

    except Exception as e:
        st.error(f"Error: {e}")
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)

# ==========================================
# 5. TAB 3 (EKSTRAK AI & AUTENTIKASI)
# ==========================================
with tab3:
    # 1. CEK STATUS LOGIN
    if not st.session_state.logged_in:
        st.markdown('<div class="login-box">', unsafe_allow_html=True)
        st.markdown("### 🔒 Login Diperlukan")
        st.caption("Silakan masukkan kredensial Anda untuk mengakses fitur AI Ekstraksi.")
        input_email = st.text_input("Username / Email")
        input_pwd = st.text_input("Password", type="password")
        
        if st.button("Masuk / Login"):
            if input_email == "tommy.masri@gmail.com" and input_pwd == "PassD97i0":
                st.session_state.logged_in = True
                st.rerun()
            else:
                st.error("❌ Username atau Password salah!")
        st.markdown('</div>', unsafe_allow_html=True)
    
    # 2. JIKA SUDAH LOGIN
    else:
        # Tombol Logout kecil di pojok
        col_logout1, col_logout2 = st.columns([4, 1])
        with col_logout2:
            if st.button("🚪 Logout"):
                st.session_state.logged_in = False
                st.rerun()

        # Handle ketersediaan transkrip
        if not st.session_state.transcript:
            st.markdown("""
                <div class="custom-info-box">
                    👆 Transkrip belum tersedia dari proses rekam/upload audio.<br>
                    <strong>ATAU</strong> Anda dapat mengunggah file teks transkrip di bawah ini:
                </div>
            """, unsafe_allow_html=True)
            
            # FITUR BARU: UPLOAD FILE .TXT
            uploaded_txt = st.file_uploader("Upload File Transkrip (.txt)", type=["txt"])
            if uploaded_txt:
                # Membaca isi file txt
                try:
                    teks_transkrip = uploaded_txt.read().decode("utf-8")
                    st.session_state.transcript = teks_transkrip
                    st.session_state.filename = os.path.splitext(uploaded_txt.name)[0]
                    st.rerun()
                except Exception as e:
                    st.error(f"Gagal membaca file teks. Pastikan format file .txt benar. (Log: {e})")
        else:
            st.success("✅ Teks Transkrip Siap Diproses!")
            st.text_area("📄 Teks Saat Ini:", st.session_state.transcript, height=150, disabled=True)
            
            # Tombol Hapus Transkrip agar user bisa upload txt baru
            if st.button("🗑️ Hapus Teks & Mulai Baru"):
                st.session_state.transcript = ""
                st.rerun()
                
            st.write("")
            
            # Dua Tombol Ajaib
            col1, col2 = st.columns(2)
            with col1:
                btn_notulen = st.button("📝 Buat Notulen", use_container_width=True)
            with col2:
                btn_laporan = st.button("📋 Buat Laporan", use_container_width=True)

            if btn_notulen or btn_laporan:
                if not gemini_key and not groq_key:
                    st.error("⚠️ Mohon masukkan/simpan API Key Groq atau Gemini terlebih dahulu.")
                else:
                    prompt_active = PROMPT_NOTULEN if btn_notulen else PROMPT_LAPORAN
                    ai_result = None
                    
                    if groq_key:
                        try:
                            with st.spinner("⚡ Menggunakan Groq (Utama)... Sedang menganalisis secara komprehensif..."):
                                client = Groq(api_key=groq_key)
                                completion = client.chat.completions.create(
                                    model="llama-3.3-70b-versatile",
                                    messages=[
                                        {"role": "system", "content": prompt_active},
                                        {"role": "user", "content": f"Berikut adalah teks transkripnya:\n{st.session_state.transcript}"}
                                    ],
                                    temperature=0.4, # Sedikit dinaikkan agar AI lebih deskriptif
                                )
                                ai_result = completion.choices[0].message.content
                        except Exception as e:
                            st.warning(f"Groq sibuk/gagal. Beralih ke Gemini... (Log: {e})")
                    
                    if gemini_key and ai_result is None:
                        try:
                            with st.spinner("🤖 Menggunakan Gemini (Cadangan)... Sedang menganalisis..."):
                                genai.configure(api_key=gemini_key)
                                model = genai.GenerativeModel('gemini-2.0-flash')
                                response = model.generate_content(f"{prompt_active}\n\nBerikut adalah teks transkripnya:\n{st.session_state.transcript}")
                                ai_result = response.text
                        except Exception as e:
                            st.error(f"Gemini juga mengalami kendala kuota/sistem. (Log: {e})")

                    if ai_result:
                        st.markdown("---")
                        st.markdown("### ✨ Hasil Ekstrak AI (Mendetail)")
                        st.markdown(ai_result)
                        
                        prefix = "Notulen_" if btn_notulen else "Laporan_"
                        ai_filename = f"{prefix}{st.session_state.filename}.txt"
                        st.download_button("💾 Download Hasil AI (.TXT)", ai_result, ai_filename, "text/plain", use_container_width=True)
                    elif not ai_result and (gemini_key or groq_key):
                        st.error("❌ Mohon maaf, server AI utama maupun cadangan saat ini sedang penuh. Silakan coba beberapa saat lagi.")

# Footer
st.markdown("<br><br><hr>", unsafe_allow_html=True) 
st.markdown("""<div style="text-align: center; font-size: 13px; color: #888;">Powered by <a href="https://espeje.com" target="_blank" class="footer-link">espeje.com</a> & <a href="https://link-gr.id" target="_blank" class="footer-link">link-gr.id</a></div>""", unsafe_allow_html=True)
