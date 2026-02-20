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

# Inisialisasi Memori Transkrip (Session State)
if 'transcript' not in st.session_state:
    st.session_state.transcript = ""
if 'filename' not in st.session_state:
    st.session_state.filename = "Hasil_STT"

# --- CUSTOM CSS (FINAL UI & MARKDOWN COLOR FIX) ---
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
    
    /* [BARU] FIX WARNA TEXT AREA (Transkrip Asli) */
    textarea {
        color: #000000 !important;
        background-color: #F8F9FA !important;
        font-weight: 500 !important;
    }

    /* [BARU] FIX WARNA SEMUA HASIL AI (MARKDOWN) AGAR HITAM TERBACA */
    div[data-testid="stMarkdownContainer"] p, 
    div[data-testid="stMarkdownContainer"] h1, 
    div[data-testid="stMarkdownContainer"] h2, 
    div[data-testid="stMarkdownContainer"] h3, 
    div[data-testid="stMarkdownContainer"] h4,
    div[data-testid="stMarkdownContainer"] li,
    div[data-testid="stMarkdownContainer"] strong,
    div[data-testid="stMarkdownContainer"] span {
        color: #111111 !important;
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

    .footer-link { text-decoration: none; font-weight: 700; color: #e74c3c !important; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. LOGIKA FFMPEG & AI
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

# System Prompts AI
PROMPT_NOTULEN = """Kamu adalah seorang Sekretaris Profesional dan Notulis Rapat yang sangat teliti. Tugasmu adalah mengubah teks transkrip audio menjadi Notulen Rapat yang terstruktur, baku, dan mudah dibaca. 
Abaikan kata-kata pengisi (filler words) atau obrolan di luar konteks pekerjaan. Susun notulen dengan format berikut:
1. Agenda/Topik Utama: (1-2 kalimat ringkasan tujuan rapat).
2. Ringkasan Pembahasan: (Buat poin-poin terstruktur tentang dinamika diskusi dan gagasan yang muncul).
3. Keputusan/Kesimpulan: (Poin-poin hasil akhir yang disepakati).
4. Tindak Lanjut (Action Items): (Daftar tugas yang harus dilakukan selanjutnya).
Gunakan bahasa Indonesia formal dan ejaan yang disempurnakan (EYD)."""

PROMPT_LAPORAN = """Kamu adalah seorang Aparatur Sipil Negara (ASN) yang ditugaskan menyusun Laporan Memorandum atau Nota Dinas. Tugasmu adalah menyusun bagian ISI LAPORAN dari teks transkrip yang diberikan. 
Jangan membuat kop surat, tanggal, 'Yth', 'Dari', atau 'Hal'. Langsung masuk ke isi laporan. Gunakan gaya bahasa birokrasi pemerintahan yang formal, terstruktur, rapi, dan analitis.
Susun laporan dengan struktur berikut:
1. Pendahuluan: (Paragraf pembuka standar laporan kegiatan).
2. Uraian Pembahasan: (Gunakan sistem penomoran/bullet points untuk menjabarkan poin substansial yang dibahas. Rangkum menjadi kalimat pasif atau formal).
3. Kesimpulan dan Rekomendasi: (Rangkuman akhir dan saran tindak lanjut).
4. Penutup: (Kalimat penutup standar, misalnya: 'Demikian kami laporkan, mohon arahan Bapak Pimpinan lebih lanjut. Terima kasih.')."""

# ==========================================
# 3. SIDEBAR (API KEYS)
# ==========================================
with st.sidebar:
    st.header("⚙️ Pengaturan AI")
    st.caption("Masukkan API Key Anda di bawah ini agar fitur AI dapat bekerja.")
    groq_key = st.text_input("🔑 Groq API Key (Utama)", type="password")
    gemini_key = st.text_input("🔑 Gemini API Key (Cadangan)", type="password")
    st.markdown("---")
    st.caption("🔒 Key Anda aman dan hanya tersimpan sementara di browser ini.")

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

# --- TAB SELECTION ---
tab1, tab2, tab3 = st.tabs(["📂 Upload File", "🎙️ Rekam Suara", "✨ Ekstrak AI"])
audio_to_process = None
source_name = "audio" 

# TAB 1
with tab1:
    uploaded_file = st.file_uploader("Pilih File Audio", type=["aac", "mp3", "wav", "m4a", "opus", "mp4", "3gp", "amr", "ogg", "flac", "wma"])
    if uploaded_file:
        audio_to_process = uploaded_file
        source_name = uploaded_file.name

# TAB 2
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

# --- PROSES STT ---
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
        
        # SIMPAN KE SESSION STATE AGAR BISA DIPAKAI DI TAB AI
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
# 5. TAB 3 (EKSTRAK AI)
# ==========================================
with tab3:
    if not st.session_state.transcript:
        st.markdown("""
            <div class="custom-info-box">
                👆 Transkrip belum tersedia.<br><small>Selesaikan proses di tab Upload/Rekam terlebih dahulu.</small>
            </div>
        """, unsafe_allow_html=True)
    else:
        st.success("Teks Transkrip Siap Diproses!")
        st.text_area("📄 Teks Asli:", st.session_state.transcript, height=150, disabled=True)
        st.write("")
        
        # Dua Tombol Ajaib
        col1, col2 = st.columns(2)
        with col1:
            btn_notulen = st.button("📝 Buat Notulen", use_container_width=True)
        with col2:
            btn_laporan = st.button("📋 Buat Laporan", use_container_width=True)

        if btn_notulen or btn_laporan:
            if not gemini_key and not groq_key:
                st.error("⚠️ Mohon masukkan API Key Groq atau Gemini di menu Sidebar sebelah kiri terlebih dahulu.")
            else:
                prompt_active = PROMPT_NOTULEN if btn_notulen else PROMPT_LAPORAN
                ai_result = None
                
                # 1. COBA GROQ DULUAN (MESIN UTAMA KARENA LEBIH CEPAT & BEBAS KUOTA)
                if groq_key:
                    try:
                        with st.spinner("⚡ Menggunakan Groq (Utama)... Sedang merangkum..."):
                            client = Groq(api_key=groq_key)
                            completion = client.chat.completions.create(
                                model="llama-3.3-70b-versatile",
                                messages=[
                                    {"role": "system", "content": prompt_active},
                                    {"role": "user", "content": f"Berikut adalah teks transkripnya:\n{st.session_state.transcript}"}
                                ],
                                temperature=0.3,
                            )
                            ai_result = completion.choices[0].message.content
                    except Exception as e:
                        st.warning(f"Groq sibuk/gagal. Beralih ke Gemini... (Log: {e})")
                
                # 2. JIKA GROQ GAGAL/TIDAK ADA KEY, COBA GEMINI (CADANGAN)
                if gemini_key and ai_result is None:
                    try:
                        with st.spinner("🤖 Menggunakan Gemini (Cadangan)... Sedang merangkum..."):
                            genai.configure(api_key=gemini_key)
                            # Gunakan versi pro yang paling stabil
                            model = genai.GenerativeModel('gemini-1.5-pro')
                            response = model.generate_content(f"{prompt_active}\n\nBerikut adalah teks transkripnya:\n{st.session_state.transcript}")
                            ai_result = response.text
                    except Exception as e:
                        st.error(f"Gemini juga mengalami kendala kuota/sistem. (Log: {e})")

                # 3. TAMPILKAN HASIL JIKA BERHASIL
                if ai_result:
                    st.markdown("---")
                    st.markdown("### ✨ Hasil Ekstrak AI")
                    st.markdown(ai_result)
                    
                    # Fitur Download Hasil AI
                    prefix = "Notulen_" if btn_notulen else "Laporan_"
                    ai_filename = f"{prefix}{st.session_state.filename}.txt"
                    st.download_button("💾 Download Hasil AI (.TXT)", ai_result, ai_filename, "text/plain", use_container_width=True)
                elif not ai_result and (gemini_key or groq_key):
                    st.error("❌ Mohon maaf, server AI utama maupun cadangan saat ini sedang penuh atau kuota API Anda habis. Silakan coba beberapa saat lagi.")

# Footer
st.markdown("<br><br><hr>", unsafe_allow_html=True) 
st.markdown("""<div style="text-align: center; font-size: 13px; color: #888;">Powered by <a href="https://espeje.com" target="_blank" class="footer-link">espeje.com</a> & <a href="https://link-gr.id" target="_blank" class="footer-link">link-gr.id</a></div>""", unsafe_allow_html=True)
