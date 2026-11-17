#!/usr/bin/env python3
"""Build a synthetic "livestream" for end-to-end testing: real video + real synthesized speech with known
highlight moments, a loud-but-boring distractor, and a matching chat log.

    python scripts/make_test_stream.py --video some_gameplay.mp4 --voice voice.onnx --out test_stream.mp4

Needs piper-tts (pip install piper-tts) and a Piper voice. Ground truth is written next to the output as .truth.json.
"""
import argparse, json, subprocess, tempfile, wave, os
import numpy as np

SR = 22050
# (time, text, gain, kind)   kind: calm | moment
SCRIPT = [
    (4,   "Alright chat, welcome back. Let me check the map real quick.", 0.45, "calm"),
    (12,  "I think the mission marker is somewhere north of here.", 0.45, "calm"),
    (21,  "Let me just change my outfit, one second.", 0.4, "calm"),
    (30,  "Okay. Driving to the marker now. Nothing special.", 0.4, "calm"),
    (41,  "Wait, wait, wait. Why is this guy following me?", 0.8, "moment"),
    (46,  "No way! He just jumped on the car! Bro, what is this NPC doing?", 1.0, "moment"),
    (52,  "Ha ha ha ha ha! I'm crying, that is so funny!", 1.0, "moment"),
    (62,  "Anyway. Thanks for the follow, appreciate you.", 0.4, "calm"),
    (71,  "Let me look at the settings for a moment.", 0.4, "calm"),
    (80,  "I am going to turn the music down a little bit.", 0.4, "calm"),
    (92,  "Still driving. This road is pretty long.", 0.4, "calm"),
    (104, "Okay, I see a ramp over there. Watch this jump, chat.", 0.7, "moment"),
    (110, "Oh my god! The car flew over the whole bridge! That physics is insane!", 1.0, "moment"),
    (117, "Did you see that? How did I even land that?", 0.9, "moment"),
    (128, "Right. Back to the mission I guess.", 0.4, "calm"),
    (137, "I need to buy some ammo before we continue.", 0.4, "calm"),
    (170, "Sorry about that noise, that was my fan.", 0.4, "calm"),
    (180, "Let me read some chat messages while I walk.", 0.4, "calm"),
    (190, "Somebody asked what time I am ending today. Probably in an hour.", 0.4, "calm"),
    (202, "Uh oh. The cops are on me. Five stars!", 0.85, "moment"),
    (207, "No no no no! They flipped the truck! I'm dead!", 1.0, "moment"),
    (212, "Ha ha ha ha! That was the worst escape ever.", 0.95, "moment"),
    (222, "Okay, respawned. Let me save the game.", 0.4, "calm"),
    (231, "Alright, that is it for this part. See you after the break.", 0.4, "calm"),
]
MOMENTS = [(41, 57), (104, 121), (202, 217)]
DISTRACTOR = (148, 160)     # loud noise, no speech: must NOT outrank the real moments
TOTAL = 240


def tts(text, voice, path):
    subprocess.run(["piper", "-m", voice, "-f", path], input=text.encode(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with wave.open(path) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if sr != SR:
        a = np.interp(np.arange(0, len(a), sr / SR), np.arange(len(a)), a).astype(np.float32)
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--voice", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rng = np.random.default_rng(7)
    mix = rng.normal(0, 0.012, TOTAL * SR).astype(np.float32)          # constant game-ambience bed
    with tempfile.TemporaryDirectory() as td:
        for i, (t, text, gain, _) in enumerate(SCRIPT):
            s = tts(text, a.voice, os.path.join(td, f"{i}.wav"))
            s = s / (np.abs(s).max() + 1e-6) * gain * 0.8
            i0 = int(t * SR)
            mix[i0:i0 + len(s)] += s[:len(mix) - i0]
        d0, d1 = int(DISTRACTOR[0] * SR), int(DISTRACTOR[1] * SR)
        mix[d0:d1] += rng.normal(0, 0.25, d1 - d0).astype(np.float32)   # loud, sustained, meaningless
        mix = np.clip(mix, -1, 1)
        wav = os.path.join(td, "mix.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes((mix * 32767).astype(np.int16).tobytes())
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-stream_loop", "-1", "-i", a.video,
                        "-i", wav, "-map", "0:v", "-map", "1:a", "-t", str(TOTAL), "-vf", "scale=1280:720,fps=30",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
                        "-pix_fmt", "yuv420p", a.out], check=True)
    # chat log: steady trickle + bursts shortly after each moment
    lines = []
    for t in range(0, TOTAL, 7):
        lines.append((t + rng.uniform(0, 5), "viewer%d" % rng.integers(1, 40), "hello"))
    for s, e in MOMENTS:
        for k in range(22):
            lines.append((s + 6 + rng.uniform(0, 8), "viewer%d" % rng.integers(1, 40),
                          rng.choice(["LUL", "KEKW", "LMAO", "clip it", "no way", "OMEGALUL"])))
    lines.sort()
    base = os.path.splitext(a.out)[0]
    with open(base + ".chat.txt", "w") as f:
        for t, u, m in lines:
            f.write(f"[{int(t)//3600}:{int(t)%3600//60:02d}:{int(t)%60:02d}] {u}: {m}\n")
    json.dump({"moments": MOMENTS, "distractor": DISTRACTOR, "duration": TOTAL,
               "script": [{"t": t, "text": x, "kind": k} for t, x, _, k in SCRIPT]}, open(base + ".truth.json", "w"), indent=1)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
