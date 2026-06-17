import os, time, threading, tempfile, xml.etree.ElementTree as ET, subprocess
import cv2, numpy as np
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont
from tkinter import filedialog
from tkinterdnd2 import TkinterDnD, DND_FILES

DETX_FPS = 25
TRACKS = 4
PPS = 200  # Pixels par seconde pour la bande rythmo

# Couleur en RGB du background de la bande rythmo
COLOR = (184,245,180,255)

# Framerate et résolution options
FRAMERATES = [24, 30, 60, 120]
RESOLUTIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4K": (3840, 2160)
}

def tc_to_seconds(tc):
    h,m,s,f = map(int, tc.split(":"))
    return h*3600+m*60+s+f/DETX_FPS-3600

def parse_detx(path):
    root = ET.parse(path).getroot()

    roles = {}
    for role in root.findall("./roles/role"):
        roles[role.attrib["id"]] = role.attrib.get("color","#FFFFFF")

    segments = []

    for line in root.findall("./body/line"):
        track = int(line.attrib.get("track",0))
        role = line.attrib.get("role","")
        color = roles.get(role,"#FFFFFF")

        start = None
        current_text = []
        current_start = None

        for child in line:
            if child.tag == "lipsync":
                typ = child.attrib.get("type","")
                t = tc_to_seconds(child.attrib["timecode"])

                if typ == "in_open":
                    start = t
                    current_start = t
                    current_text = []

                elif typ == "mpb":
                    # Marqueur labial - créer un segment pour le texte accumulé
                    if current_text and current_start is not None:
                        text_content = " ".join(current_text)
                        # Garder même si c'est juste des espaces (pauses)
                        if text_content.strip() or text_content:
                            segments.append({
                                "start": current_start,
                                "end": t,
                                "text": text_content.strip() if text_content.strip() else " ",
                                "track": track,
                                "color": color
                            })
                        current_start = t
                        current_text = []

                elif typ == "out_open" and start is not None:
                    # Fin de la ligne - créer un segment pour le texte restant
                    if current_text:
                        text_content = " ".join(current_text)
                        if text_content.strip() or text_content:
                            segments.append({
                                "start": current_start,
                                "end": t,
                                "text": text_content.strip() if text_content.strip() else " ",
                                "track": track,
                                "color": color
                            })
                    start = None
                    current_text = []
                    current_start = None

            elif child.tag == "text":
                txt = child.text or ""
                # Garder le texte tel quel (avec les espaces)
                if txt:
                    current_text.append(txt)

    return segments

def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2],16) for i in (0,2,4))

def fit_frame(frame, export_w, export_h):
    h, w = frame.shape[:2]
    scale = min(export_w/w, export_h/h)
    nw, nh = int(w*scale), int(h*scale)

    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)

    canvas = np.zeros((export_h, export_w, 3), dtype=np.uint8)

    x = (export_w-nw)//2
    y = (export_h-nh)//2

    canvas[y:y+nh, x:x+nw] = resized
    return canvas

def stretch_text(draw_img, text, x_pos, width_px, color, y):
    if not text:
        return

    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except:
        font = ImageFont.load_default()

    tmp = Image.new("RGBA",(4000,100),(0,0,0,0))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0,0), text, font=font)

    tw = max(1,bbox[2]-bbox[0])
    th = bbox[3]-bbox[1]

    text_img = Image.new("RGBA",(tw+20,th+20),(0,0,0,0))
    d2 = ImageDraw.Draw(text_img)
    d2.text((10,10), text, fill=color, font=font)

    width_px = max(width_px, tw)
    text_img = text_img.resize((width_px, text_img.height))

    draw_img.alpha_composite(text_img,(x_pos,y))

def make_rhythmo(segments, t, export_w, export_h, rhythmo_h, tracks, pps):
    # Background modifiable dans la variable "COLOR"
    img = Image.new("RGBA",(export_w, rhythmo_h),(COLOR))
    d = ImageDraw.Draw(img)

    th = rhythmo_h // tracks

    # Lignes de séparation entre pistes
    for i in range(tracks+1):
        y = i*th
        d.line((0,y,export_w,y),fill=(140,140,140))

    # Playhead rouge à gauche
    center = 150
    d.line((center,0,center,rhythmo_h),fill=(255,0,0),width=3)

    # Dessiner les segments de dialogue
    for seg in segments:
        track = min(seg["track"], tracks-1)

        x1 = center + int((seg["start"]-t)*pps)
        x2 = center + int((seg["end"]-t)*pps)

        if x2 < -2000 or x1 > export_w+2000:
            continue

        width = max(50, x2-x1)

        stretch_text(
            img,
            seg["text"],
            x1,
            width,
            hex2rgb(seg["color"]),
            track*th+10
        )

    return cv2.cvtColor(np.array(img.convert("RGB")),cv2.COLOR_RGB2BGR)

class App:

    def __init__(self):
        self.detx = None
        self.video = None
        self.audio = None

        # Créer le root EN PREMIER
        self.root = TkinterDnD.Tk()
        self.root.title("SuturaCappella")
        self.root.geometry("700x900")
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        # Options sélectionnées (APRÈS root)
        self.selected_fps = ctk.IntVar(value=60)
        self.selected_resolution = ctk.StringVar(value="1080p")
        self.mute_audio = ctk.BooleanVar(value=False)

        # Main frame
        main_frame = ctk.CTkFrame(self.root)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        # Titre
        title = ctk.CTkLabel(main_frame, text="SuturaCappella", font=("Arial", 32, "bold"))
        title.pack(pady=20)

        # 1. DETX
        detx_label = ctk.CTkLabel(main_frame, text="1. Fichier Cappella .detx", font=("Arial", 14, "bold"))
        detx_label.pack(anchor="w", pady=(20,5))

        detx_frame = ctk.CTkFrame(main_frame)
        detx_frame.pack(fill="x", pady=5)

        self.detx_label = ctk.CTkLabel(
            detx_frame,
            text="📄 Déposez le fichier .detx ici",
            height=60,
            corner_radius=8,
            fg_color=("#f0f0f0", "#2b2b2b")
        )
        self.detx_label.pack(side="left", fill="both", expand=True, padx=(0,5))
        self.detx_label.drop_target_register(DND_FILES)
        self.detx_label.dnd_bind("<<Drop>>", self.drop_detx)

        detx_reset = ctk.CTkButton(
            detx_frame,
            text="Reset",
            width=70,
            height=60,
            command=self.reset_detx
        )
        detx_reset.pack(side="right")

        # 2. Vidéo
        video_label = ctk.CTkLabel(main_frame, text="2. Vidéo (MP4, MOV, AVI)", font=("Arial", 14, "bold"))
        video_label.pack(anchor="w", pady=(15,5))

        video_frame = ctk.CTkFrame(main_frame)
        video_frame.pack(fill="x", pady=5)

        self.video_label = ctk.CTkLabel(
            video_frame,
            text="🎬 Déposez la vidéo ici",
            height=60,
            corner_radius=8,
            fg_color=("#f0f0f0", "#2b2b2b")
        )
        self.video_label.pack(side="left", fill="both", expand=True, padx=(0,5))
        self.video_label.drop_target_register(DND_FILES)
        self.video_label.dnd_bind("<<Drop>>", self.drop_video)

        video_reset = ctk.CTkButton(
            video_frame,
            text="Reset",
            width=70,
            height=60,
            command=self.reset_video
        )
        video_reset.pack(side="right")

        # 3. Audio (optionnel)
        audio_label = ctk.CTkLabel(main_frame, text="3. Audio (MP3, WAV, M4A) - Optionnel", font=("Arial", 14, "bold"))
        audio_label.pack(anchor="w", pady=(15,5))

        audio_frame = ctk.CTkFrame(main_frame)
        audio_frame.pack(fill="x", pady=5)

        self.audio_label = ctk.CTkLabel(
            audio_frame,
            text="🔊 Déposez un fichier audio (optionnel)",
            height=60,
            corner_radius=8,
            fg_color=("#f0f0f0", "#2b2b2b")
        )
        self.audio_label.pack(side="left", fill="both", expand=True, padx=(0,5))
        self.audio_label.drop_target_register(DND_FILES)
        self.audio_label.dnd_bind("<<Drop>>", self.drop_audio)

        audio_reset = ctk.CTkButton(
            audio_frame,
            text="Reset",
            width=70,
            height=60,
            command=self.reset_audio
        )
        audio_reset.pack(side="right")

        # Checkbox Mute audio
        mute_frame = ctk.CTkFrame(main_frame)
        mute_frame.pack(fill="x", pady=10)

        self.mute_checkbox = ctk.CTkCheckBox(
            mute_frame,
            text="🔇 Mute audio (désactiver le son final)",
            variable=self.mute_audio
        )
        self.mute_checkbox.pack(anchor="w")

        # Framerate
        framerate_frame = ctk.CTkFrame(main_frame)
        framerate_frame.pack(fill="x", pady=15)

        framerate_label = ctk.CTkLabel(framerate_frame, text="Framerate:", font=("Arial", 12, "bold"))
        framerate_label.pack(side="left", padx=(0,10))

        for fps in FRAMERATES:
            rb = ctk.CTkRadioButton(
                framerate_frame,
                text=f"{fps} fps",
                variable=self.selected_fps,
                value=fps
            )
            rb.pack(side="left", padx=5)

        # Résolution
        resolution_frame = ctk.CTkFrame(main_frame)
        resolution_frame.pack(fill="x", pady=15)

        resolution_label = ctk.CTkLabel(resolution_frame, text="Résolution:", font=("Arial", 12, "bold"))
        resolution_label.pack(side="left", padx=(0,10))

        for res in RESOLUTIONS.keys():
            rb = ctk.CTkRadioButton(
                resolution_frame,
                text=res,
                variable=self.selected_resolution,
                value=res
            )
            rb.pack(side="left", padx=5)

        # Barre de progression
        self.progress = ctk.CTkProgressBar(main_frame)
        self.progress.pack(fill="x", pady=15)
        self.progress.set(0)

        # Status
        self.status = ctk.CTkLabel(main_frame, text="Prêt à générer", font=("Arial", 12), text_color="#aaaaaa")
        self.status.pack(pady=10)

        # Bouton générer
        generate_btn = ctk.CTkButton(
            main_frame,
            text="Créer la vidéo",
            height=45,
            font=("Arial", 14, "bold"),
            command=self.generate
        )
        generate_btn.pack(fill="x", pady=20)

    def drop_detx(self, e):
        self.detx = e.data.strip("{}")
        self.detx_label.configure(text=f"✓ {os.path.basename(self.detx)}")

    def drop_video(self, e):
        self.video = e.data.strip("{}")
        self.video_label.configure(text=f"✓ {os.path.basename(self.video)}")

    def drop_audio(self, e):
        file_path = e.data.strip("{}")
        ext = os.path.splitext(file_path)[1].lower()
        
        # Extensions vidéo à bloquer
        video_exts = ['.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.webm', '.m4v']
        
        if ext in video_exts:
            self.status.configure(
                text="❌ Impossible d'importer une vidéo dans le champ audio",
                text_color="#ff6b6b"
            )
            self.audio = None
            return
        
        self.audio = file_path
        self.audio_label.configure(text=f"✓ {os.path.basename(self.audio)}")

    def reset_detx(self):
        self.detx = None
        self.detx_label.configure(text="📄 Déposez le fichier .detx ici")

    def reset_video(self):
        self.video = None
        self.video_label.configure(text="🎬 Déposez la vidéo ici")

    def reset_audio(self):
        self.audio = None
        self.audio_label.configure(text="🔊 Déposez un fichier audio (optionnel)")

    def generate(self):
        if not self.detx or not self.video:
            self.status.configure(text="❌ Veuillez déposer DETX et vidéo", text_color="#ff6b6b")
            return

        out = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4"), ("All Files", "*.*")]
        )
        if not out:
            return

        threading.Thread(target=self.render, args=(out,), daemon=True).start()

    def render(self, out):
        try:
            fps = self.selected_fps.get()
            res_key = self.selected_resolution.get()
            export_w, export_h = RESOLUTIONS[res_key]
            
            rhythmo_h = int(export_h * 0.25)  # 25% pour la bande rythmo
            video_h = export_h - rhythmo_h

            segments = parse_detx(self.detx)

            cap = cv2.VideoCapture(self.video)
            source_fps = cap.get(cv2.CAP_PROP_FPS) or 30
            total_source_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_source_frames / source_fps
            total = int(duration * fps)

            tmp = tempfile.mktemp(suffix=".mp4")

            writer = cv2.VideoWriter(
                tmp,
                cv2.VideoWriter_fourcc(*"mp4v"),
                fps,
                (export_w, export_h)
            )

            start = time.time()
            
            frame_queue = []
            
            for i in range(total):
                t = i / fps

                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if not ok:
                    break
                
                frame = fit_frame(frame, export_w, video_h)
                rh = make_rhythmo(segments, t, export_w, export_h, rhythmo_h, TRACKS, PPS)

                final = np.zeros((export_h, export_w, 3), dtype=np.uint8)
                final[:video_h] = frame
                final[video_h:] = rh

                writer.write(final)

                prog = (i+1)/total
                self.progress.set(prog)

                elapsed = time.time()-start
                fps_actual = (i+1)/max(elapsed, 0.01)
                eta = (total-(i+1))/max(fps_actual, 0.01)

                self.status.configure(
                    text=f"{i+1:,}/{total:,} frames | {fps_actual:.1f} fps | ETA {eta:.0f}s",
                    text_color="#ffffff"
                )
                self.root.update()

            writer.release()
            cap.release()

            self.status.configure(text="Intégration du son...", text_color="#ffffff")
            self.root.update()

            # Intégration audio avec ffmpeg (OPTIMISÉ)
            if self.integrate_audio_ffmpeg(tmp, out, self.audio, duration):
                self.status.configure(text="✓ Vidéo créée avec succès!", text_color="#4ade80")
            else:
                # integrate_audio_ffmpeg a déjà affiché l'erreur
                pass

        except Exception as e:
            self.status.configure(text=f"❌ Erreur: {str(e)}", text_color="#ff6b6b")
            print(f"Error: {e}")

    def integrate_audio_ffmpeg(self, video_tmp, output, audio_file, duration):
        """Intégrer l'audio avec ffmpeg (ULTRA-RAPIDE)"""
        try:
            mute_enabled = self.mute_audio.get()
            has_audio = False

            if mute_enabled:
                # Mute - aucun audio dans la sortie
                cmd = [
                    "ffmpeg",
                    "-i", video_tmp,
                    "-c:v", "copy",
                    "-an",
                    "-y",
                    "-loglevel", "error",
                    output
                ]
            elif audio_file:
                # Audio externe fourni - l'utiliser
                cmd = [
                    "ffmpeg",
                    "-i", video_tmp,
                    "-i", audio_file,
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-shortest",
                    "-y",
                    "-loglevel", "error",
                    output
                ]
            else:
                # Pas d'audio externe - utiliser l'audio de la vidéo source
                try:
                    cap = cv2.VideoCapture(self.video)
                    # Vérifier s'il y a un stream audio
                    has_audio = int(cap.get(cv2.CAP_PROP_AUDIO_STREAM_IDX)) >= 0
                    cap.release()
                except:
                    has_audio = False
                
                if has_audio:
                    # Extraire audio temporaire de la vidéo source
                    audio_tmp = tempfile.mktemp(suffix=".aac")
                    extract_cmd = [
                        "ffmpeg",
                        "-i", self.video,
                        "-c:a", "aac",
                        "-q:a", "9",
                        "-vn",
                        "-y",
                        "-loglevel", "error",
                        audio_tmp
                    ]
                    subprocess.run(extract_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # Combiner vidéo avec audio extrait
                    cmd = [
                        "ffmpeg",
                        "-i", video_tmp,
                        "-i", audio_tmp,
                        "-c:v", "copy",
                        "-c:a", "aac",
                        "-shortest",
                        "-y",
                        "-loglevel", "error",
                        output
                    ]
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False
                    )
                    
                    try:
                        os.remove(audio_tmp)
                    except:
                        pass
                    
                    if result.returncode != 0:
                        error_msg = result.stderr if result.stderr else "Erreur ffmpeg inconnue"
                        print(f"Erreur ffmpeg: {error_msg}")
                        self.status.configure(
                            text=f"❌ Erreur ffmpeg: {error_msg[:50]}...",
                            text_color="#ff6b6b"
                        )
                        return False
                    
                    print(f"✓ Vidéo finalisée: {output}")
                    return True
                else:
                    # Aucun audio dans la vidéo source - vidéo sans son
                    cmd = [
                        "ffmpeg",
                        "-i", video_tmp,
                        "-c:v", "copy",
                        "-an",
                        "-y",
                        "-loglevel", "error",
                        output
                    ]

            # Exécuter ffmpeg pour les autres cas
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False
            )

            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else "Erreur ffmpeg inconnue"
                print(f"Erreur ffmpeg: {error_msg}")
                self.status.configure(
                    text=f"❌ Erreur ffmpeg: {error_msg[:50]}...",
                    text_color="#ff6b6b"
                )
                return False

            print(f"✓ Vidéo finalisée: {output}")
            return True

        except FileNotFoundError:
            self.status.configure(
                text="❌ ffmpeg non trouvé. Installer ffmpeg sur le système",
                text_color="#ff6b6b"
            )
            print("Erreur: ffmpeg n'est pas installé ou pas dans le PATH")
            return False
        except Exception as e:
            self.status.configure(
                text=f"❌ Erreur: {str(e)[:50]}",
                text_color="#ff6b6b"
            )
            print(f"Erreur: {e}")
            return False
        finally:
            # Nettoyer le fichier temporaire
            try:
                if os.path.exists(video_tmp):
                    os.remove(video_tmp)
            except:
                pass

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    App().run()
