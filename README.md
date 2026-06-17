# SuturaCappella

Générateur de bande rythmo pour Cappella. Combine une vidéo source avec les timecodes DETX pour créer une vidéo avec synchronisation de dialogue.



\---



## Installation

##### Exécutez le fichier setup.bat

## 

## Utilisation

1. **Déposer fichier DETX** → Fichier Cappella
2. **Déposer vidéo** → MP4, MOV, AVI, etc.
3. **Déposer audio (optionnel)** → MP3, WAV, M4A
4. **Sélectionner framerate** → 24/30/60/120 fps
5. **Sélectionner résolution** → 720p/1080p/1440p/4K
6. **Cliquer "Créer la vidéo"** → Choisir où sauvegarder
7. **Attendre** → \~35-40s (1080p60fps, 2 min de vidéo)



\---



## Caractéristiques

* **Playhead rouge** à gauche (position actuelle)
* **Background jaune** pour confort visuel
* **Format 16:9** pour toutes les résolutions
* **Audio optionnel** intégré à la vidéo
* **4 pistes** de dialogue avec couleurs



\---



## Détails Techniques

* **Vidéo source**: Redimensionnée en 16:9 (75% de l'écran)
* **Bande rythmo**: 25% de l'écran en bas, background jaune
* **Playhead**: Fixe à gauche, vidéo défile de droite
* **Codec vidéo**: H.264 (compatible universel)
* **Codec audio**: AAC
* **Fichiers temporaires**: Auto-supprimés



\---



## Troubleshooting

**"ffmpeg not found"**

* Windows: Ajouter ffmpeg au PATH
* Vérifier: `ffmpeg -version`

**Vidéo lente**

* Réduire résolution (4K → 1080p)
* Réduire framerate (120 → 60 fps)



## Licence

SuturaCappella est distribué sous licence MIT.

Copyright © 2026 Gasteryoug

Le logiciel est fourni « en l'état », sans aucune garantie explicite ou implicite. Son utilisation relève de la responsabilité de l'utilisateur.



## Contributions

Les contributions, signalements de bugs et propositions d'amélioration sont les bienvenus via les GitHub Issues et Pull Requests.
