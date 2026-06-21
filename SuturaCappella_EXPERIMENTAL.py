import os, time, threading, tempfile, xml.etree.ElementTree as ET, subprocess
from queue import Queue, Empty
from collections import OrderedDict
import cv2, numpy as np
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont
from tkinter import filedialog
from tkinterdnd2 import TkinterDnD, DND_FILES

DETX_FPS = 25
TRACKS = 4
PPS = 200

COLOR = (235,235,235,255)

FRAMERATES = [24, 30, 60, 120]
RESOLUTIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4K": (3840, 2160)
}

# Cache global avec limite LRU
_FONT_CACHE = {}
_TEXT_IMG_CACHE_SIZE = 0
_TEXT_IMG_CACHE = OrderedDict()  # LRU cache
_NAME_BADGE_CACHE_SIZE = 0
_NAME_BADGE_CACHE = OrderedDict()  # LRU cache

MAX_TEXT_CACHE_SIZE = 500 * 1024 * 1024  # 500MB max pour images texte
MAX_BADGE_CACHE_SIZE = 100 * 1024 * 1024  # 100MB max pour badges

def get_font(font_name, size):
    """Cache global des fonts"""
    cache_key = (font_name, size)
    if cache_key not in _FONT_CACHE:
        try:
            _FONT_CACHE[cache_key] = ImageFont.truetype(font_name, size)
        except:
            _FONT_CACHE[cache_key] = ImageFont.load_default()
    return _FONT_CACHE[cache_key]

def tc_to_seconds(tc):
    h,m,s,f = map(int, tc.split(":"))
    result = h*3600+m*60+s+f/DETX_FPS-3600
    # Afficher la première et quelques timecodes pour diagnostic
    # (Non affiché à chaque appel pour ne pas flood le console)
    return result

def hex2rgb(h):
    """Hex to RGB"""
    h = h.lstrip("#")
    return tuple(int(h[i:i+2],16) for i in (0,2,4))

def parse_detx(path):
    """Parse DETX avec pré-conversion couleurs"""
    root = ET.parse(path).getroot()

    roles = {}
    for role in root.findall("./roles/role"):
        color_hex = role.attrib.get("color","#FFFFFF")
        roles[role.attrib["id"]] = {
            "color": hex2rgb(color_hex),
            "name": role.attrib.get("name", "Unknown")
        }

    segments = []

    for line in root.findall("./body/line"):
        track = int(line.attrib.get("track",0))
        role = line.attrib.get("role","")
        color = roles.get(role, {}).get("color", (255, 255, 255))
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
                    phrase_started = False

                elif typ == "mpb":
                    if current_text and current_start is not None:
                        text_content = "".join(current_text)
                        if text_content.strip() or text_content:
                            segments.append({
                                "start": current_start,
                                "end": t,
                                "text": text_content.strip() if text_content.strip() else " ",
                                "track": track,
                                "color": color,
                                "role_name": role_name,
                                "is_phrase_start": not phrase_started
                            })
                            phrase_started = True
                        current_start = t
                        current_text = []

                elif typ == "out_open" and start is not None:
                    if current_text:
                        text_content = "".join(current_text)
                        if text_content.strip() or text_content:
                            segments.append({
                                "start": current_start,
                                "end": t,
                                "text": text_content.strip() if text_content.strip() else " ",
                                "track": track,
                                "color": color,
                                "role_name": role_name,
                                "is_phrase_start": not phrase_started
                            })
                    start = None
                    current_text = []
                    current_start = None
                    phrase_started = False

            elif child.tag == "text":
                txt = child.text or ""
                if txt:
                    current_text.append(txt)

    return segments

def fit_frame(frame, export_w, export_h, canvas_buffer):
    """Resize avec buffer réutilisé"""
    h, w = frame.shape[:2]
    scale = min(export_w/w, export_h/h)
    nw, nh = int(w*scale), int(h*scale)

    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)

    canvas_buffer.fill(0)
    x = (export_w-nw)//2
    y = (export_h-nh)//2
    canvas_buffer[y:y+nh, x:x+nw] = resized
    return canvas_buffer

def get_text_img_cached(text, color, width_px, line_height):
    """Cache LRU des images texte avec éviction automatique"""
    global _TEXT_IMG_CACHE_SIZE
    
    cache_key = (text, color, width_px, line_height)
    
    if cache_key in _TEXT_IMG_CACHE:
        # Déplacer au bout (LRU)
        _TEXT_IMG_CACHE.move_to_end(cache_key)
        return _TEXT_IMG_CACHE[cache_key]

    font_size = 34
    font = None
    
    while font_size > 8:
        font = get_font("arial.ttf", font_size)
        tmp = Image.new("RGBA", (4000, 100), (0, 0, 0, 0))
        d_tmp = ImageDraw.Draw(tmp)
        bbox = d_tmp.textbbox((0, 0), text, font=font)
        tw = max(1, bbox[2] - bbox[0])
        
        if tw <= width_px:
            break
        font_size -= 2

    if font is None:
        font = get_font("arial.ttf", 8)

    # Créer image texte
    tmp = Image.new("RGBA", (4000, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    tw = max(1, bbox[2] - bbox[0])
    th = bbox[3] - bbox[1]
    
    text_img = Image.new("RGBA", (tw + 10, th + 10), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(text_img)
    d2.text((5, 5), text, fill=color, font=font)
    
    text_img = text_img.resize((width_px, text_img.height), Image.Resampling.LANCZOS)
    text_img = text_img.resize((width_px, line_height), Image.Resampling.LANCZOS)
    
    # Ajouter au cache LRU
    img_size = text_img.size[0] * text_img.size[1] * 4  # RGBA
    _TEXT_IMG_CACHE[cache_key] = text_img
    _TEXT_IMG_CACHE_SIZE += img_size
    _TEXT_IMG_CACHE.move_to_end(cache_key)
    
    # Éviction LRU si dépassement
    while _TEXT_IMG_CACHE_SIZE > MAX_TEXT_CACHE_SIZE and _TEXT_IMG_CACHE:
        old_key, old_img = _TEXT_IMG_CACHE.popitem(last=False)
        _TEXT_IMG_CACHE_SIZE -= old_img.size[0] * old_img.size[1] * 4
    
    return text_img

def make_rhythmo_frame(visible_segments, name_positions, export_w, rhythmo_h, show_names, th, center, color, tracks):
    """Génère la bande rythmo"""
    name_padding = 40 if show_names else 0
    total_height = rhythmo_h + name_padding
    
    img = Image.new("RGBA", (export_w, total_height), (255, 255, 255, 0))
    rhythmo_img = Image.new("RGBA", (export_w, rhythmo_h), color)
    d = ImageDraw.Draw(rhythmo_img)

    # Lignes et playhead
    for i in range(tracks+1):
        y = i*th
        d.line((0,y,export_w,y),fill=(140,140,140))
    d.line((center,0,center,rhythmo_h),fill=(255,0,0),width=3)

    # Dessiner segments texte avec cache
    for seg in visible_segments:
        text_img = get_text_img_cached(seg["text"], seg["color"], seg["width"], th)
        text_y = seg["y_offset"] + (th - text_img.height) // 2
        rhythmo_img.alpha_composite(text_img, (seg["x1"], text_y))

    img.alpha_composite(rhythmo_img, (0, name_padding))

    # Noms avec cache LRU
    if show_names and name_positions:
        name_font = get_font("arial.ttf", 20)
        name_height = int(th * 0.4)
        phrase_starts = [pos for pos in name_positions if pos["is_phrase_start"]]
        
        for pos in phrase_starts:
            opacity = 255
            
            for other_pos in phrase_starts:
                if pos is not other_pos and pos["track"] == other_pos["track"]:
                    if (pos["x1"] < other_pos["x2"] and pos["x2"] > other_pos["x1"]):
                        opacity = 128
                        break
            
            name_text = pos["name"]
            badge_cache_key = (name_text, pos["color"], name_height, opacity)
            
            if badge_cache_key not in _NAME_BADGE_CACHE:
                name_draw_temp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
                bbox = name_draw_temp.textbbox((0, 0), name_text, font=name_font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
                
                padding_x = 8
                name_width = text_width + padding_x * 2
                
                name_img = Image.new("RGBA", (name_width, name_height), pos["color"])
                name_draw_img = ImageDraw.Draw(name_img)
                text_y = (name_height - text_height) // 2
                name_draw_img.text((padding_x, text_y), name_text, fill=(255, 255, 255), font=name_font)
                
                if opacity < 255:
                    alpha = name_img.split()[3]
                    alpha = alpha.point(lambda p: int(p * opacity / 255))
                    name_img.putalpha(alpha)
                
                _NAME_BADGE_CACHE[badge_cache_key] = name_img
                _NAME_BADGE_CACHE.move_to_end(badge_cache_key)
            
            name_img = _NAME_BADGE_CACHE[badge_cache_key]
            name_x = pos["x1"] - name_img.width
            name_y = pos["y"] + name_padding
            img.alpha_composite(name_img, (name_x, name_y))

    result_img = img.crop((0, name_padding, export_w, total_height))
    return cv2.cvtColor(np.array(result_img.convert("RGB")), cv2.COLOR_RGB2BGR)

def precompute_visible_segments(segments, t, export_w, rhythmo_h, pps, tracks, center, th):
    """Pré-calcule les segments visibles"""
    visible = []
    name_pos = []
    
    for seg in segments:
        track = min(seg["track"], tracks-1)
        x1 = center + int((seg["start"]-t)*pps)
        x2 = center + int((seg["end"]-t)*pps)

        if x2 < -2000 or x1 > export_w+2000:
            continue

        width = max(50, x2-x1)
        
        visible.append({
            "text": seg["text"],
            "x1": x1,
            "width": width,
            "color": seg["color"],
            "track": track,
            "y_offset": track*th
        })
        
        name_pos.append({
            "name": seg.get("role_name", "Unknown"),
            "track": track,
            "x1": x1,
            "x2": x2,
            "y": track*th,
            "color": seg["color"],
            "is_phrase_start": seg.get("is_phrase_start", False)
        })
    
    return visible, name_pos

class FrameReadThread(threading.Thread):
    """Thread 1: Lecture vidéo séquentielle (sans seek)"""
    def __init__(self, video_path, total_frames, fps, output_queue, source_fps):
        threading.Thread.__init__(self)
        self.video_path = video_path
        self.total_frames = total_frames
        self.fps = fps
        self.output_queue = output_queue
        self.daemon = False
        self.running = True
        self.source_fps = source_fps
        self.frames_read = 0
        
    def run(self):
        try:
            cap = cv2.VideoCapture(self.video_path)
            
            # OPT#1: Lecture séquentielle avec duplication intelligente
            # Au lieu de seeker à chaque frame (très lent),
            # on lit séquentiellement et on duplique intelligemment
            
            frame_export_idx = 0
            source_frame_count = 0
            
            while self.running:
                ret, frame = cap.read()
                if not ret:
                    break
                
                # Calculer combien de fois dupliquer ce frame
                # Pour préserver le timing exact
                next_idx = int(round((source_frame_count + 1) * self.fps / self.source_fps))
                duplicates = next_idx - frame_export_idx
                
                # Envoyer le frame N fois
                for dup in range(duplicates):
                    if not self.running:
                        break
                    try:
                        self.output_queue.put((frame_export_idx, frame), timeout=2)
                        self.frames_read += 1
                    except:
                        if self.running:
                            pass
                    frame_export_idx += 1
                
                source_frame_count += 1
            
            cap.release()
        finally:
            print(f"[FrameReadThread] Terminé (OPT#1 lecture seq). total_frames={self.total_frames}, frames_read={self.frames_read}")
            self.output_queue.put(None)  # Signal de fin

class RythmoThread(threading.Thread):
    """Thread 2: Génération bande rythmo"""
    def __init__(self, input_queue, output_queue, segments, export_w, export_h, rhythmo_h, fps, current_color, show_names, source_fps):
        threading.Thread.__init__(self)
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.segments = segments
        self.export_w = export_w
        self.export_h = export_h
        self.rhythmo_h = rhythmo_h
        self.fps = fps
        self.source_fps = source_fps
        self.current_color = current_color
        self.show_names = show_names
        self.daemon = False
        self.running = True
        self.frames_processed = 0
        
    def run(self):
        try:
            th = self.rhythmo_h // TRACKS
            center = 150
            first_frame_logged = False
            last_frame_idx = None
            
            while self.running:
                try:
                    item = self.input_queue.get(timeout=1)
                    if item is None:
                        print(f"[RythmoThread] Terminé. frames_processed={self.frames_processed}, dernière frame_idx={last_frame_idx}")
                        break
                    
                    frame_idx, video_frame = item
                    # Timecode RÉEL à 60 FPS pour fluidité de la bande rythmo
                    t = frame_idx / self.fps
                    last_frame_idx = frame_idx
                    
                    # LOG : première frame et quelques points de repère
                    if not first_frame_logged:
                        print(f"[RythmoThread] PREMIERE FRAME:")
                        print(f"  frame_idx={frame_idx}, fps_export={self.fps}")
                        print(f"  t = {frame_idx} / {self.fps} = {t:.6f}s")
                        first_frame_logged = True
                    elif frame_idx % 50 == 0:  # Tous les 50 frames
                        print(f"[RythmoThread] frame_idx={frame_idx}, t={t:.6f}s")
                    
                    # Pré-calcul segments visibles
                    visible_segs, name_pos = precompute_visible_segments(
                        self.segments, t, self.export_w, self.rhythmo_h, PPS, TRACKS, center, th
                    )
                    
                    # Génération rhythmo
                    rhythmo = make_rhythmo_frame(
                        visible_segs, name_pos, self.export_w, self.rhythmo_h,
                        self.show_names, th, center, self.current_color, TRACKS
                    )
                    
                    self.output_queue.put((frame_idx, video_frame, rhythmo))
                    self.frames_processed += 1
                except Empty:
                    continue
                except Exception as e:
                    print(f"Erreur RythmoThread: {e}")
                    break
        finally:
            self.output_queue.put(None)  # Signal de fin

class App:

    def __init__(self):
        self.detx = None
        self.video = None
        self.audio = None
        self.current_color = (235, 235, 235, 255)
        self.text_cache = {}

        self.root = TkinterDnD.Tk()
        self.root.title("SuturaCappella [EXPERIMENTAL]")
        self.root.geometry("700x900")
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.selected_fps = ctk.IntVar(value=60)
        self.selected_resolution = ctk.StringVar(value="1080p")
        self.mute_audio = ctk.BooleanVar(value=False)

        main_frame = ctk.CTkFrame(self.root)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        title = ctk.CTkLabel(main_frame, text="SuturaCappella [EXPERIMENTAL]", font=("Arial", 32, "bold"))
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
        hex_color = self.color_entry.get().strip()
        
        if not hex_color.startswith("#"):
            hex_color = "#" + hex_color
        
        try:
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
            # Vider les caches (mais garder structure LRU)
            _TEXT_IMG_CACHE.clear()
            _NAME_BADGE_CACHE.clear()
            
            fps = self.selected_fps.get()
            res_key = self.selected_resolution.get()
            export_w, export_h = RESOLUTIONS[res_key]
            
            rhythmo_h = int(export_h * 0.25)
            video_h = export_h - rhythmo_h

            segments = parse_detx(self.detx)

            cap = cv2.VideoCapture(self.video)
            source_fps = cap.get(cv2.CAP_PROP_FPS) or 30
            total_source_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_source_frames / source_fps
            total = int(duration * fps)
            cap.release()

            # ==== LOGS DE DIAGNOSTIC ====
            print("\n" + "="*60)
            print("DIAGNOSTIC TIMECODE ET DURÉE")
            print("="*60)
            print(f"[VIDEO SOURCE]")
            print(f"  source_fps = {source_fps}")
            print(f"  total_source_frames = {total_source_frames}")
            print(f"  video_duration = {duration:.6f} secondes")
            print(f"\n[EXPORT]")
            print(f"  fps_export = {fps}")
            print(f"  total_frames_a_generer = {total}")
            print(f"\n[PARAMETRES PASSES AU THREAD]")
            print(f"  FrameReadThread recevra : total_frames={total}")
            print(f"  FrameReadThread recevra : fps={fps}")
            print(f"  FrameReadThread recevra : source_fps={source_fps}")
            print(f"\n[PROBLEME IDENTIFIE]")
            print(f"  total_frames != total_source_frames")
            print(f"  {total} (export) != {total_source_frames} (source)")
            print(f"  FrameReadThread va boucler {total}x mais source n'a que {total_source_frames} frames")
            print(f"\n[TIMECODE DANS RHYTHMO]")
            print(f"  Code : t = frame_idx / fps_export")
            print(f"  Avec fps_export={fps}")
            print(f"  Exemple frame 0   : t = 0 / {fps} = 0.000s (CORRECT)")
            print(f"  Exemple frame 1   : t = 1 / {fps} = {1.0/fps:.6f}s (DEVRAIT ETRE {1.0/source_fps:.6f}s)")
            print(f"  Exemple frame 100 : t = 100 / {fps} = {100.0/fps:.6f}s (DEVRAIT ETRE {100.0/source_fps:.6f}s)")
            print(f"  Exemple frame {total_source_frames-1} : t = {total_source_frames-1} / {fps} = {(total_source_frames-1)/fps:.6f}s (DEVRAIT ETRE {(total_source_frames-1)/source_fps:.6f}s)")
            print(f"\n[PROJECTION]")
            print(f"  Timecode atteint : ~{total_source_frames/fps:.2f}s (LIMITE)")
            print(f"  Timecode attendu : ~{duration:.2f}s")
            print(f"  DUREE VIDEO EXPORT ATTENDUE: {total/fps:.2f} secondes")
            print(f"  DUREE REELLE SI BUG: ~{total_source_frames/fps:.2f} secondes ({100*total_source_frames/(total if total > 0 else 1):.1f}% seulement)")
            print("="*60 + "\n")
            # ==== FIN LOGS ====

            # Pipeline threading avec queues limitées
            read_queue = Queue(maxsize=3)
            rhythmo_queue = Queue(maxsize=3)

            # Threads
            reader = FrameReadThread(self.video, total, fps, read_queue, source_fps)
            rhythmo_gen = RythmoThread(
                read_queue, rhythmo_queue, segments, export_w, export_h, 
                rhythmo_h, fps, self.current_color, self.show_names.get(), source_fps
            )

            reader.start()
            rhythmo_gen.start()

            # Buffers réutilisés
            canvas_buffer = np.zeros((video_h, export_w, 3), dtype=np.uint8)
            final = np.zeros((export_h, export_w, 3), dtype=np.uint8)

            tmp = tempfile.mktemp(suffix=".mp4")
            writer = cv2.VideoWriter(
                tmp,
                cv2.VideoWriter_fourcc(*"mp4v"),
                fps,
                (export_w, export_h)
            )

            start = time.time()
            frames_written = 0
            frame_buffer = {}
            next_frame_idx = 0
            
            while frames_written < total:
                try:
                    # Timeout augmenté + vérification thread vivant
                    item = rhythmo_queue.get(timeout=5)
                    
                    if item is None:
                        # Fin du thread rhythmo - vider frame_buffer avant exit
                        break
                    
                    frame_idx, video_frame, rhythmo = item
                    
                    # Nettoyer les références anciennes
                    if len(frame_buffer) > 50:
                        old_indices = [i for i in frame_buffer.keys() if i < next_frame_idx - 30]
                        for old_idx in old_indices:
                            del frame_buffer[old_idx]
                    
                    frame_buffer[frame_idx] = (video_frame, rhythmo)
                    
                    # Écrire les frames consécutives
                    while next_frame_idx in frame_buffer:
                        video_frame, rhythmo = frame_buffer.pop(next_frame_idx)
                        
                        video_frame = fit_frame(video_frame, export_w, video_h, canvas_buffer)
                        
                        final[:video_h] = video_frame
                        final[video_h:] = rhythmo
                        
                        writer.write(final)
                        frames_written += 1
                        next_frame_idx += 1
                        
                        # Nettoyer references
                        del video_frame
                        del rhythmo
                        
                        prog = frames_written / total
                        self.progress.set(prog)
                        
                        elapsed = time.time() - start
                        fps_actual = frames_written / max(elapsed, 0.01)
                        eta = (total - frames_written) / max(fps_actual, 0.01)
                        
                        # OPT#6: Mettre à jour UI seulement tous les 10 frames pour réduire surcharge
                        if frames_written % 10 == 0:
                            self.status.configure(
                                text=f"{frames_written:,}/{total:,} frames | {fps_actual:.1f} fps | ETA {eta:.0f}s",
                                text_color="#ffffff"
                            )
                            self.root.update()
                
                except Empty:
                    # Timeout - vérifier si threads vivent toujours
                    if not reader.is_alive() and not rhythmo_gen.is_alive():
                        break
                    continue

            # CRUCIAL: Vider frame_buffer résiduel
            while next_frame_idx in frame_buffer and frames_written < total:
                video_frame, rhythmo = frame_buffer.pop(next_frame_idx)
                
                video_frame = fit_frame(video_frame, export_w, video_h, canvas_buffer)
                
                final[:video_h] = video_frame
                final[video_h:] = rhythmo
                
                writer.write(final)
                frames_written += 1
                next_frame_idx += 1
                
                del video_frame
                del rhythmo

            writer.release()
            
            # Attendre fin threads
            reader.join(timeout=2)
            rhythmo_gen.join(timeout=2)

            # ==== LOGS FINAUX ====
            print("\n" + "="*60)
            print("RESULTAT FINAL")
            print("="*60)
            print(f"[ATTENDU]")
            print(f"  total_frames à générer = {total}")
            print(f"  durée attendue = {total/fps:.2f}s (à {fps} FPS)")
            print(f"\n[REEL]")
            print(f"  frames_written = {frames_written}")
            print(f"  durée réelle = {frames_written/fps:.2f}s (à {fps} FPS)")
            print(f"\n[DIFFERENCE]")
            print(f"  frames manquantes = {total - frames_written}")
            print(f"  ratio = {100*frames_written/max(total,1):.1f}% seulement")
            print(f"  durée perdue = {(total-frames_written)/fps:.2f}s")
            print(f"\n[ANALYSE]")
            print(f"  total_source_frames = {total_source_frames}")
            print(f"  source_fps = {source_fps}")
            print(f"  total demandé au FrameReadThread = {total}")
            print(f"  Si FrameReadThread lit seulement {total_source_frames} frames")
            print(f"  Alors RythmoThread génère {total_source_frames} frames")
            print(f"  Et writer écrit {total_source_frames} frames")
            print(f"  Durée = {total_source_frames}/{fps} = {total_source_frames/fps:.2f}s")
            print(f"  Cela correspond à {100*total_source_frames/max(total,1):.1f}% de la durée attendue")
            print("="*60 + "\n")
            # ==== FIN LOGS ====

            self.status.configure(text="Intégration du son...", text_color="#ffffff")
            self.root.update()

            if self.integrate_audio_ffmpeg(tmp, out, self.audio, duration):
                self.status.configure(text="✓ Vidéo créée avec succès!", text_color="#4ade80")
            else:
                pass

        except Exception as e:
            self.status.configure(text=f"❌ Erreur: {str(e)}", text_color="#ff6b6b")
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    def integrate_audio_ffmpeg(self, video_tmp, output, audio_file, duration):
        """Intégrer l'audio avec ffmpeg"""
        try:
            mute_enabled = self.mute_audio.get()

            if mute_enabled:
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
                
                extract_result = subprocess.run(
                    extract_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False
                )
                
                if extract_result.returncode == 0 and os.path.getsize(audio_tmp) > 1000:
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
            try:
                if os.path.exists(video_tmp):
                    os.remove(video_tmp)
            except:
                pass

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    App().run()
