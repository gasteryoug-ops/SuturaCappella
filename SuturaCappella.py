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
COLOR = (235,235,235,255)

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
        roles[role.attrib["id"]] = {
            "color": role.attrib.get("color","#FFFFFF"),
            "name": role.attrib.get("name", "Unknown")
        }

    segments = []

    for line in root.findall("./body/line"):
        track = int(line.attrib.get("track",0))
        role = line.attrib.get("role","")
        color = roles.get(role, {}).get("color", "#FFFFFF")
        role_name = roles.get(role, {}).get("name", "Unknown")

        start = None
        current_text = []
        current_start = None
        phrase_started = False

        for child in line:
            if child.tag == "lipsync":
                typ = child.attrib.get("type","")
                t = tc_to_seconds(child.attrib["timecode"])

                if typ == "in_open":
                    start = t
                    current_start = t
                    current_text = []
                    phrase_started = False  # Réinitialiser pour chaque phrase

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
                                "color": color,
                                "role_name": role_name,
                                "is_phrase_start": not phrase_started  # True seulement pour le premier
                            })
                            phrase_started = True  # Marquer qu'on a créé au moins un segment
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
                                "color": color,
                                "role_name": role_name,
                                "is_phrase_start": not phrase_started  # True seulement pour le premier
                            })
                    start = None
                    current_text = []
                    current_start = None
                    phrase_started = False

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

def stretch_text(draw_img, text, x_pos, width_px, color, y, line_height):
    """Dessiner le texte centré verticalement dans sa ligne
    
    Args:
        draw_img: Image PIL à dessiner dessus
        text: Texte à afficher
        x_pos: Position X
        width_px: Largeur disponible
        color: Couleur RGB
        y: Position Y du début de la ligne
        line_height: Hauteur totale de la ligne
    """
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

    # Créer l'image de texte avec padding minimal
    text_img = Image.new("RGBA",(tw+10,th+10),(0,0,0,0))
    d2 = ImageDraw.Draw(text_img)
    d2.text((5,5), text, fill=color, font=font)

    # Redimensionner pour matcher la largeur disponible
    text_img = text_img.resize((width_px, text_img.height))

    # Centrer verticalement dans la ligne
    text_y = y + (line_height - text_img.height) // 2
    text_y = max(y, text_y)  # Ne pas dépasser le début de la ligne
    
    draw_img.alpha_composite(text_img,(x_pos, text_y))

def make_rhythmo(segments, t, export_w, export_h, rhythmo_h, tracks, pps, color, show_names):
    # Ajouter padding en haut pour overflow des noms
    name_padding = 40 if show_names else 0
    total_height = rhythmo_h + name_padding
    
    # Background personnalisable via paramètre (sans le padding)
    img = Image.new("RGBA", (export_w, total_height), (255, 255, 255, 0))
    
    # Créer la bande rythmo dans la partie basse
    rhythmo_img = Image.new("RGBA", (export_w, rhythmo_h), color)
    d = ImageDraw.Draw(rhythmo_img)

    th = rhythmo_h // tracks

    # Lignes de séparation entre pistes
    for i in range(tracks+1):
        y = i*th
        d.line((0,y,export_w,y),fill=(140,140,140))

    # Playhead rouge à gauche
    center = 150
    d.line((center,0,center,rhythmo_h),fill=(255,0,0),width=3)

    # Calculer les positions des noms pour détecter les chevauchements
    name_positions = []
    
    # Dessiner les segments de dialogue
    for seg in segments:
        track = min(seg["track"], tracks-1)

        x1 = center + int((seg["start"]-t)*pps)
        x2 = center + int((seg["end"]-t)*pps)

        if x2 < -2000 or x1 > export_w+2000:
            continue

        # Largeur minimum augmentée pour éviter overlap du texte
        width = max(150, x2-x1)

        stretch_text(
            rhythmo_img,
            seg["text"],
            x1,
            width,
            hex2rgb(seg["color"]),
            track*th,
            th
        )
        
        # Stocker la position du nom pour vérifier les chevauchements
        if show_names:
            name_positions.append({
                "name": seg.get("role_name", "Unknown"),
                "track": track,
                "x1": x1,
                "x2": x2,
                "y": track*th,
                "color": hex2rgb(seg["color"]),
                "is_phrase_start": seg.get("is_phrase_start", False)
            })

    # Coller la bande rythmo dans la partie basse de l'image finale
    img.alpha_composite(rhythmo_img, (0, name_padding))

    # Afficher les noms des rôles si activé
    if show_names:
        try:
            name_font = ImageFont.truetype("arial.ttf", 20)
        except:
            name_font = ImageFont.load_default()
        
        name_height = int(th * 0.4)  # 40% de la hauteur de la ligne
        
        # Vérifier les chevauchements et déterminer l'opacité
        for i, pos in enumerate(name_positions):
            # Afficher le nom seulement au début de la phrase
            if not pos["is_phrase_start"]:
                continue
            
            opacity = 255  # Opacité par défaut
            
            # Vérifier si ce nom chevauche d'autres
            for j, other_pos in enumerate(name_positions):
                if i != j and pos["track"] == other_pos["track"] and other_pos["is_phrase_start"]:
                    # Même piste et autres débuts de phrase - vérifier chevauchement X
                    if (pos["x1"] < other_pos["x2"] and pos["x2"] > other_pos["x1"]):
                        # Chevauchement détecté
                        opacity = 128  # 50% transparence
                        break
            
            # Créer badge du nom avec background coloré
            name_text = pos["name"]
            name_draw_temp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            bbox = name_draw_temp.textbbox((0, 0), name_text, font=name_font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            
            # Padding autour du texte
            padding_x = 8
            name_width = text_width + padding_x * 2
            
            # Créer le badge avec background coloré
            name_img = Image.new("RGBA", (name_width, name_height), pos["color"])
            name_draw_img = ImageDraw.Draw(name_img)
            
            # Centrer le texte verticalement dans le badge
            text_y = (name_height - text_height) // 2
            name_draw_img.text((padding_x, text_y), name_text, fill=(255, 255, 255), font=name_font)
            
            # Appliquer l'opacité
            if opacity < 255:
                alpha = name_img.split()[3]
                alpha = alpha.point(lambda p: int(p * opacity / 255))
                name_img.putalpha(alpha)
            
            # Placer le nom à gauche du texte, haut touchant la ligne de séparation
            name_x = pos["x1"] - name_width
            name_y = pos["y"] + name_padding  # Ajuster pour le padding
            
            # Dessiner sur l'image finale avec padding
            img.alpha_composite(name_img, (name_x, name_y))

    # Recadrer pour retourner seulement la bande rythmo (sans le padding)
    result_img = img.crop((0, name_padding, export_w, total_height))
    
    return cv2.cvtColor(np.array(result_img.convert("RGB")),cv2.COLOR_RGB2BGR)

class App:

    def __init__(self):
        self.detx = None
        self.video = None
        self.audio = None
        self.current_color = (235, 235, 235, 255)  # Couleur RGB du background

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

        # Checkbox Afficher les noms
        names_frame = ctk.CTkFrame(main_frame)
        names_frame.pack(fill="x", pady=10)

        self.show_names = ctk.BooleanVar(value=True)
        self.names_checkbox = ctk.CTkCheckBox(
            names_frame,
            text="👤 Afficher les noms des rôles",
            variable=self.show_names
        )
        self.names_checkbox.pack(anchor="w")

        # Color picker pour la bande rythmo
        color_frame = ctk.CTkFrame(main_frame)
        color_frame.pack(fill="x", pady=15)

        color_label = ctk.CTkLabel(color_frame, text="Couleur bande rythmo:", font=("Arial", 12, "bold"))
        color_label.pack(side="left", padx=(0,10))

        self.color_entry = ctk.CTkEntry(
            color_frame,
            placeholder_text="#EBEBEB",
            width=120
        )
        self.color_entry.pack(side="left", padx=(0,10))
        self.color_entry.insert(0, "#EBEBEB")

        color_apply_btn = ctk.CTkButton(
            color_frame,
            text="Appliquer",
            width=100,
            command=self.apply_color
        )
        color_apply_btn.pack(side="left")

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

    def apply_color(self):
        """Convertir hex en RGB et appliquer la couleur"""
        hex_color = self.color_entry.get().strip()
        
        if not hex_color.startswith("#"):
            hex_color = "#" + hex_color
        
        try:
            # Valider et convertir hex en RGB
            rgb = hex2rgb(hex_color)
            self.current_color = (rgb[0], rgb[1], rgb[2], 255)
            self.status.configure(
                text=f"✓ Couleur appliquée: {hex_color}",
                text_color="#4ade80"
            )
        except:
            self.status.configure(
                text="❌ Code couleur invalide (ex: #EBEBEB)",
                text_color="#ff6b6b"
            )

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
                rh = make_rhythmo(segments, t, export_w, export_h, rhythmo_h, TRACKS, PPS, self.current_color, self.show_names.get())

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
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                if result.returncode != 0:
                    error_msg = result.stderr if result.stderr else "Erreur ffmpeg inconnue"
                    print(f"Erreur ffmpeg: {error_msg}")
                    self.status.configure(text=f"❌ Erreur ffmpeg: {error_msg[:50]}...", text_color="#ff6b6b")
                    return False

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
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                if result.returncode != 0:
                    error_msg = result.stderr if result.stderr else "Erreur ffmpeg inconnue"
                    print(f"Erreur ffmpeg: {error_msg}")
                    self.status.configure(text=f"❌ Erreur ffmpeg: {error_msg[:50]}...", text_color="#ff6b6b")
                    return False

            else:
                # Pas d'audio externe - extraire de la vidéo source
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
                
                # Essayer d'extraire l'audio
                extract_result = subprocess.run(
                    extract_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False
                )
                
                if extract_result.returncode == 0 and os.path.getsize(audio_tmp) > 1000:
                    # Audio extraire avec succès - combiner avec la vidéo
                    print(f"Audio extrait de la vidéo source: {audio_tmp}")
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
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                    
                    try:
                        os.remove(audio_tmp)
                    except:
                        pass
                    
                    if result.returncode != 0:
                        error_msg = result.stderr if result.stderr else "Erreur ffmpeg inconnue"
                        print(f"Erreur ffmpeg: {error_msg}")
                        self.status.configure(text=f"❌ Erreur ffmpeg: {error_msg[:50]}...", text_color="#ff6b6b")
                        return False
                else:
                    # Pas d'audio trouvé - vidéo sans son
                    print("Aucun audio détecté dans la vidéo source")
                    try:
                        os.remove(audio_tmp)
                    except:
                        pass
                    
                    cmd = [
                        "ffmpeg",
                        "-i", video_tmp,
                        "-c:v", "copy",
                        "-an",
                        "-y",
                        "-loglevel", "error",
                        output
                    ]
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                    if result.returncode != 0:
                        error_msg = result.stderr if result.stderr else "Erreur ffmpeg inconnue"
                        print(f"Erreur ffmpeg: {error_msg}")
                        self.status.configure(text=f"❌ Erreur ffmpeg: {error_msg[:50]}...", text_color="#ff6b6b")
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
