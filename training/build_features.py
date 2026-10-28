# -*- coding: utf-8 -*-
"""Build mmap spectrogram feature sets per the official notebook flow.

Follows basic_training_notebook.ipynb cells 5-7:
  Clips (seeded train/val/test split, 10%+10%) -> Augmentation (fixed 3.2s,
  EQ/noise/gain/RIR) -> 10ms-step spectrograms -> RaggedMmap per split.

Two feature roots are produced:
  features/pos_all/    wake-word positives (pos_xiaochen + pos_variants)
  features/neg_speech_zh/  Chinese TTS speech negatives (neg_zh_tts)

Augmentation background/RIR sources: official packs include only mmaps, not
raw wav, so instead of notebook's fma/audioset wavs we use AddColorNoise +
official-free chain (EQ/distortion/pitch/bandstop/gain). Once speech.zip is
verified we can regenerate with real background paths if FAP is high.
"""
import os
import sys

from microwakeword.audio.augmentation import Augmentation
from microwakeword.audio.clips import Clips
from microwakeword.audio.spectrograms import SpectrogramGeneration
from mmap_ninja.ragged import RaggedMmap

SEED = 10
AUG_DURATION_S = 3.2  # notebook default; frames trained per sample

AUGMENTER = Augmentation(
    augmentation_duration_s=AUG_DURATION_S,
    augmentation_probabilities={
        "SevenBandParametricEQ": 0.1,
        "TanhDistortion": 0.1,
        "PitchShift": 0.1,
        "BandStopFilter": 0.1,
        "AddColorNoise": 0.25,
        "Gain": 1.0,
        "GainTransition": 0.25,
        # RIR/AddBackgroundNoise stay off until we have raw wav sources
    },
    impulse_paths=[],
    background_paths=[],
    min_jitter_s=0.195,
    max_jitter_s=0.205,
)


def _stage(src_dirs, tag):
    """Clips takes a single directory and does its own seeded split, so
    hardlink every source wav into one staging dir (same volume, no copy)."""
    import re
    stage = os.path.join(r"D:\mww\samples", f"_staged_{tag}")
    os.makedirs(stage, exist_ok=True)
    n = 0
    for src in src_dirs:
        for f in os.listdir(src):
            if not f.endswith(".wav"):
                continue
            dst = os.path.join(stage, f)
            if not os.path.exists(dst):
                os.link(os.path.join(src, f), dst)
            n += 1
    print(f"[stage] {tag}: {n} wavs -> {stage}", flush=True)
    return stage


def build(split_dirs, output_root, tag):
    clips = Clips(
        input_directory=_stage(split_dirs, tag),
        file_pattern="*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=SEED,
        split_count=0.1,
    )

    os.makedirs(output_root, exist_ok=True)
    for split in ("training", "validation", "testing"):
        out_dir = os.path.join(output_root, split)
        os.makedirs(out_dir, exist_ok=True)
        split_name = {"training": "train", "validation": "validation",
                      "testing": "test"}[split]
        repetition = 2 if split == "training" else 1
        slide = 10 if split != "testing" else 1
        spec = SpectrogramGeneration(
            clips=clips, augmenter=AUGMENTER, slide_frames=slide, step_ms=10)
        gen = spec.spectrogram_generator(split=split_name, repeat=repetition)
        RaggedMmap.from_generator(
            out_dir=os.path.join(out_dir, "wakeword_mmap"),
            sample_generator=gen,
            batch_size=100,
            verbose=True,
        )
        print(f"[done] {output_root}/{split}", flush=True)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "pos"):
        # round 8: ALL positive sets — r5 accidentally trained on 2730 clips
        # (dirty staging); the "clean" r6/r7 rebuild starved positives to 353
        # and the model collapsed onto the shared 同学 tail. Never drop a set.
        build([r"D:\mww\samples\pos_xiaochen", r"D:\mww\samples\pos_variants",
               r"D:\mww\samples\pos_xiaochen_v2", r"D:\mww\samples\pos_real"],
              r"D:\mww\features\pos_all", "pos")
    if which in ("all", "negzh"):
        # round 6: TTS negatives + real daily-speech negatives (same speaker
        # as the real positives — the anti-"说话就醒" balance, per r2)
        build([r"D:\mww\samples\neg_zh_tts_v2", r"D:\mww\samples\neg_speech_real"],
              r"D:\mww\features\neg_speech_zh", "negzh")
    if which in ("all", "nearmiss"):
        # round 7: the 小X同学 near-miss region r6 lacked — TTS + real takes
        build([r"D:\mww\samples\neg_nearmiss_tts", r"D:\mww\samples\neg_nearmiss_real"],
              r"D:\mww\features\neg_nearmiss", "nearmiss")
    if which in ("posreal",):
        # round 9a: real human voice only (r9_p takes + r6 originals).
        # TTS-timbre overfit killed real-voice wake; r9a trains pure voice.
        build([r"D:\mww\samples\pos_real"],
              r"D:\mww\features\pos_real_r9", "posr9")
    if which in ("postts",):
        # round 9b: TTS positives on the side, downweighted via sampling_weight
        build([r"D:\mww\samples\pos_xiaochen", r"D:\mww\samples\pos_variants",
               r"D:\mww\samples\pos_xiaochen_v2"],
              r"D:\mww\features\pos_tts_r9", "postts")
