"""EasyCM Render Worker — monteur vidéo FFmpeg maison (sans watermark, illimité).

Reçoit un storyboard + les clips trouvés (Pexels) et fabrique un vrai MP4 :
- télécharge chaque clip, le recadre au format voulu (16:9 / 9:16 / 1:1) ;
- incruste le texte à l'écran ;
- ajoute la voix off générée GRATUITEMENT par edge-tts (voix Microsoft, illimité,
  sans clé) — ou aucune voix ;
- (option) musique de fond mixée sous la voix ;
- assemble toutes les scènes → MP4, AUCUN filigrane.

Pensé pour tourner sur un hébergeur gratuit sans carte (Hugging Face Spaces,
Render…). Le frontend d'EasyCM l'appelle directement (CORS ouvert), ce qui évite
tout timeout serverless côté Vercel.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import uuid
from pathlib import Path

import edge_tts
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="EasyCM Render Worker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# 720p : compromis qualité/mémoire adapté aux instances gratuites (512 Mo)
RES = {"landscape": (1280, 720), "portrait": (720, 1280), "square": (720, 720)}
# Options x264 économes en mémoire/CPU (indispensable sur tier gratuit)
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-pix_fmt", "yuv420p"]
OUT = Path(tempfile.gettempdir()) / "easycm_out"
OUT.mkdir(exist_ok=True)


class RenderIn(BaseModel):
    storyboard: dict
    clips: list = []
    voice: str = "ai"                      # "ai" | "none"
    voice_name: str = "fr-FR-DeniseNeural"
    orientation: str = "landscape"
    music_url: str | None = None


async def _run(cmd: list[str]) -> None:
    # ffmpeg dans un thread : n'immobilise pas la boucle async (health checks OK)
    p = await asyncio.to_thread(subprocess.run, cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-1000:])


def _dur(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True)
    try:
        return float(out.stdout.decode().strip())
    except ValueError:
        return 0.0


@app.get("/health")
def health() -> dict:
    return {"ok": True, "engine": "ffmpeg+edge-tts"}


@app.post("/render")
async def render(body: RenderIn):
    scenes = body.storyboard.get("scenes", [])
    if not scenes:
        raise HTTPException(400, "Storyboard sans scènes.")
    W, H = RES.get(body.orientation, RES["landscape"])
    work = Path(tempfile.mkdtemp())
    try:
        scene_files: list[Path] = []
        for i, sc in enumerate(scenes):
            clip = body.clips[i] if i < len(body.clips) else None
            dur = max(2, int(sc.get("duration") or 5))

            # 1) Voix off (edge-tts) — gratuite, illimitée, sans clé
            voice_path = None
            if body.voice == "ai" and (sc.get("voiceover") or "").strip():
                voice_path = work / f"v{i}.mp3"
                await edge_tts.Communicate(sc["voiceover"], body.voice_name).save(str(voice_path))
                dur = max(dur, int(_dur(voice_path) + 0.6))

            # 2) Clip vidéo (ou fond uni si absent)
            clip_path = work / f"c{i}.mp4"
            if clip and clip.get("url"):
                async with httpx.AsyncClient(timeout=90, follow_redirects=True) as cl:
                    r = await cl.get(clip["url"])
                    r.raise_for_status()
                    clip_path.write_bytes(r.content)
            else:
                await _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                            f"color=c=0x141B2D:s={W}x{H}:d={dur}", "-t", str(dur), str(clip_path)])

            # 3) Texte à l'écran (via textfile pour éviter tout échappement)
            txt = (sc.get("on_screen_text") or "").strip()
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},setsar=1,fps=30")
            if txt:
                tf = work / f"t{i}.txt"
                tf.write_text(txt, encoding="utf-8")
                fs = int(H * 0.055)
                vf += (f",drawtext=fontfile={FONT}:textfile={tf.as_posix()}:"
                       f"fontcolor=white:fontsize={fs}:box=1:boxcolor=black@0.45:"
                       f"boxborderw=24:line_spacing=10:x=(w-text_w)/2:y=h-text_h-{int(H * 0.08)}")

            # 4) Encodage de la scène (toujours une piste audio → concat fiable)
            scene_out = work / f"s{i}.mp4"
            cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip_path)]
            if voice_path:
                cmd += ["-i", str(voice_path)]
            else:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
            cmd += ["-t", str(dur), "-vf", vf, "-map", "0:v", "-map", "1:a",
                    *X264, "-r", "30", "-c:a", "aac", "-b:a", "128k",
                    "-ar", "44100", "-shortest", str(scene_out)]
            await _run(cmd)
            scene_files.append(scene_out)

        # 5) Assemblage des scènes (ré-encodage = robuste)
        listf = work / "list.txt"
        listf.write_text("".join(f"file '{p.as_posix()}'\n" for p in scene_files), encoding="utf-8")
        assembled = work / "assembled.mp4"
        await _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                    *X264, "-c:a", "aac", "-b:a", "128k", "-ar", "44100", str(assembled)])

        # 6) Musique de fond (option) mixée sous la voix
        final = OUT / f"{uuid.uuid4().hex}.mp4"
        if body.music_url:
            music = work / "music.mp3"
            async with httpx.AsyncClient(timeout=90, follow_redirects=True) as cl:
                r = await cl.get(body.music_url)
                r.raise_for_status()
                music.write_bytes(r.content)
            await _run(["ffmpeg", "-y", "-i", str(assembled), "-stream_loop", "-1", "-i", str(music),
                        "-filter_complex",
                        "[1:a]volume=0.14[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
                        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", str(final)])
        else:
            assembled.replace(final)

        return FileResponse(str(final), media_type="video/mp4", filename="easycm-pub.mp4")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Rendu échoué : {e}")
    finally:
        for f in work.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
