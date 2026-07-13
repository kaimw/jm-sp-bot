#!/usr/bin/env python3
import argparse
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from faster_whisper import WhisperModel
from scipy.fftpack import dct
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics import silhouette_score
from speechbrain.inference.speaker import EncoderClassifier


def fmt_ts(seconds: float, comma: bool = False) -> str:
    ms = int(round((seconds - math.floor(seconds)) * 1000))
    total = int(math.floor(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def convert_wav(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(dst),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def transcribe(
    audio: Path,
    whole_wav: Path,
    model_name: str,
    out_json: Path,
    threads: int,
    chunk_seconds: int,
    beam_size: int,
) -> list[dict]:
    if out_json.exists():
        return json.loads(out_json.read_text(encoding="utf-8"))["segments"]

    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    wav_data, sr = sf.read(str(whole_wav), dtype="float32")
    duration = len(wav_data) / sr
    chunks_dir = out_json.parent / f"{audio.stem}.chunks"
    chunks_dir.mkdir(exist_ok=True)
    segments = []
    language = "zh"
    language_probability = 1.0

    starts = list(range(0, int(math.ceil(duration)), chunk_seconds))
    for chunk_index, start_sec in enumerate(starts, 1):
        chunk_json = chunks_dir / f"chunk_{chunk_index:03d}.json"
        if chunk_json.exists():
            chunk_segments = json.loads(chunk_json.read_text(encoding="utf-8"))
            print(f"loaded chunk {chunk_index}/{len(starts)}", flush=True)
            segments.extend(chunk_segments)
            continue

        chunk_wav = chunks_dir / f"chunk_{chunk_index:03d}.wav"
        if not chunk_wav.exists():
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(start_sec),
                    "-t",
                    str(chunk_seconds),
                    "-i",
                    str(audio),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(chunk_wav),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        print(f"transcribing chunk {chunk_index}/{len(starts)}: {fmt_ts(start_sec)}", flush=True)
        segments_iter, info = model.transcribe(
            str(chunk_wav),
            language="zh",
            task="transcribe",
            beam_size=beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        language = info.language
        language_probability = info.language_probability
        chunk_segments = []
        for seg in segments_iter:
            text = seg.text.strip()
            if not text:
                continue
            chunk_segments.append(
                {
                    "id": len(segments) + len(chunk_segments) + 1,
                    "start": float(seg.start) + start_sec,
                    "end": float(seg.end) + start_sec,
                    "text": text,
                }
            )
        chunk_json.write_text(json.dumps(chunk_segments, ensure_ascii=False, indent=2), encoding="utf-8")
        segments.extend(chunk_segments)

    for i, seg in enumerate(segments, 1):
        seg["id"] = i
    payload = {
        "language": language,
        "language_probability": language_probability,
        "duration": duration,
        "model": model_name,
        "segments": segments,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return segments


def embed_segments(wav: Path, segments: list[dict], cache: Path) -> np.ndarray:
    if cache.exists():
        return np.load(cache)

    audio, sr = sf.read(str(wav), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    print("loading speaker embedding model", flush=True)
    try:
        classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(cache.parent / "speechbrain-spkrec-ecapa-voxceleb"),
            run_opts={"device": "cpu"},
        )

        embeddings = []
        with torch.no_grad():
            for seg in segments:
                if seg["id"] % 20 == 0:
                    print(f"embedding segment {seg['id']}/{len(segments)}", flush=True)
                start = max(0, int(seg["start"] * sr))
                end = min(len(audio), int(seg["end"] * sr))
                clip = audio[start:end]
                if len(clip) < sr:
                    clip = np.pad(clip, (0, sr - len(clip)))
                signal = torch.from_numpy(clip).unsqueeze(0)
                emb = classifier.encode_batch(signal).squeeze().cpu().numpy()
                emb = emb / max(np.linalg.norm(emb), 1e-9)
                embeddings.append(emb)
    except Exception as exc:
        print(f"speechbrain failed, falling back to acoustic clustering: {exc}", flush=True)
        embeddings = []
        for seg in segments:
            if seg["id"] % 100 == 0:
                print(f"extracting acoustic segment {seg['id']}/{len(segments)}", flush=True)
            start = max(0, int(seg["start"] * sr))
            end = min(len(audio), int(seg["end"] * sr))
            clip = audio[start:end]
            embeddings.append(acoustic_embedding(clip, sr))

    arr = np.vstack(embeddings)
    np.save(cache, arr)
    return arr


def acoustic_embedding(clip: np.ndarray, sr: int) -> np.ndarray:
    if len(clip) < int(0.5 * sr):
        clip = np.pad(clip, (0, int(0.5 * sr) - len(clip)))
    clip = clip.astype(np.float32)
    clip = clip - float(np.mean(clip))
    peak = float(np.max(np.abs(clip)))
    if peak > 0:
        clip = clip / peak

    frame = int(0.025 * sr)
    hop = int(0.010 * sr)
    if len(clip) < frame:
        clip = np.pad(clip, (0, frame - len(clip)))
    starts = range(0, max(1, len(clip) - frame + 1), hop)
    feats = []
    freqs = np.fft.rfftfreq(frame, 1 / sr)
    for st in starts:
        x = clip[st : st + frame]
        if len(x) < frame:
            x = np.pad(x, (0, frame - len(x)))
        x = x * np.hanning(frame)
        mag = np.abs(np.fft.rfft(x)) + 1e-8
        power = mag**2
        total = float(np.sum(power))
        centroid = float(np.sum(freqs * power) / total)
        bandwidth = float(np.sqrt(np.sum(((freqs - centroid) ** 2) * power) / total))
        zcr = float(np.mean(np.abs(np.diff(np.signbit(x)))))
        energy = float(np.log(np.mean(x**2) + 1e-8))
        bins = np.array_split(np.log(mag), 24)
        cep = dct([float(np.mean(b)) for b in bins], norm="ortho")[:12]
        feats.append(np.concatenate(([energy, zcr, centroid / sr, bandwidth / sr], cep)))

    feat = np.vstack(feats)
    emb = np.concatenate([feat.mean(axis=0), feat.std(axis=0)])
    return emb / max(np.linalg.norm(emb), 1e-9)


def choose_speakers(embeddings: np.ndarray, max_speakers: int) -> tuple[np.ndarray, int]:
    n = len(embeddings)
    if n < 3:
        return np.zeros(n, dtype=int), 1

    best_labels = None
    best_score = -1.0
    best_k = 2
    upper = min(max_speakers, n - 1)
    for k in range(2, upper + 1):
        labels = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average").fit_predict(embeddings)
        if len(set(labels)) < 2:
            continue
        score = silhouette_score(embeddings, labels, metric="cosine")
        if score > best_score:
            best_score = score
            best_labels = labels
            best_k = k

    if best_labels is None:
        return np.zeros(n, dtype=int), 1
    return best_labels, best_k


def write_outputs(segments: list[dict], labels: np.ndarray, stem: Path, speaker_count: int) -> None:
    speaker_names = {label: f"说话人{idx + 1}" for idx, label in enumerate(sorted(set(labels)))}
    for seg, label in zip(segments, labels):
        seg["speaker"] = speaker_names[int(label)]

    srt = []
    for idx, seg in enumerate(segments, 1):
        srt.extend(
            [
                str(idx),
                f"{fmt_ts(seg['start'], True)} --> {fmt_ts(seg['end'], True)}",
                f"{seg['speaker']}：{seg['text']}",
                "",
            ]
        )
    stem.with_suffix(".speaker.srt").write_text("\n".join(srt), encoding="utf-8")

    lines = [
        "# 商务智能体调研录音转写",
        "",
        f"- 自动识别说话人数：{speaker_count}",
        "- 说明：说话人标签由本地声纹聚类自动生成，可能需要人工按真实姓名校对。",
        "",
    ]
    current = None
    block_text = []
    block_start = 0.0
    block_end = 0.0

    def flush() -> None:
        if not block_text:
            return
        lines.append(f"**[{fmt_ts(block_start)} - {fmt_ts(block_end)}] {current}：**")
        lines.append("".join(block_text).strip())
        lines.append("")

    for seg in segments:
        current_len = sum(len(text) for text in block_text)
        block_too_long = block_text and (block_end - block_start > 45 or current_len > 300)
        if seg["speaker"] != current or block_too_long:
            flush()
            current = seg["speaker"]
            block_text = [seg["text"]]
            block_start = seg["start"]
            block_end = seg["end"]
        else:
            block_text.append(seg["text"])
            block_end = seg["end"]
    flush()

    stem.with_suffix(".dialogue.md").write_text("\n".join(lines), encoding="utf-8")
    stem.with_suffix(".speaker.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("transcripts"))
    parser.add_argument("--model", default="base")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--max-speakers", type=int, default=4)
    parser.add_argument("--chunk-seconds", type=int, default=600)
    parser.add_argument("--beam-size", type=int, default=1)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.out_dir / args.audio.stem
    wav = stem.with_suffix(".16k.wav")
    raw_json = stem.with_suffix(f".{args.model}.json")
    emb_cache = stem.with_suffix(f".{args.model}.embeddings.npy")

    convert_wav(args.audio, wav)
    segments = transcribe(args.audio, wav, args.model, raw_json, args.threads, args.chunk_seconds, args.beam_size)
    embeddings = embed_segments(wav, segments, emb_cache)
    labels, speaker_count = choose_speakers(embeddings, args.max_speakers)
    write_outputs(segments, labels, stem, speaker_count)


if __name__ == "__main__":
    main()
